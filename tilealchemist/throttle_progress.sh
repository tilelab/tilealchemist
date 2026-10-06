#!/usr/bin/env bash
# Throttles \r-redrawn output to at most one line per INTERVAL seconds, most
# recent wins. A \r-redrawing tool repaints one line in place: `pmtiles
# merge`'s progress bar, tile-join's "z/x/y", tilemaker's per-tile counters. \n-terminated lines always print
# immediately. The split is on which byte ended a chunk, never on message
# text, so it works for any such tool.
#
# Particularly worth using ahead of a GitHub Actions log. Its viewer does not
# support \r as an in-place redraw, so a \r-heavy tool piped in unthrottled
# either floods the log with one line per redraw or renders as one unreadable
# blob, depending on the step.
#
# A pending redraw MUST be dropped, not shown, once a real \n line arrives.
# That line proves the tool moved on, and flushing the stale redraw first
# would leak one progress line per phase transition on any tool interleaving
# \n status lines with \r redraws. It is still shown if nothing supersedes
# it: once INTERVAL seconds pass since the last print, or at EOF.
#
# Usage: some_noisy_command 2>&1 | throttle_progress.sh <interval_seconds>
#
# No `set -e`. `read`'s non-zero exit on timeout and on EOF is expected here,
# not an error to abort on.
set -u

interval=$1

# partial: text since the last \r or \n, its terminator not yet seen. `read`
# stores what it got before a timeout or EOF, so a line spanning several
# reads accumulates here.
# pending/pending_set: the latest \r-terminated redraw, waiting to print or
# to be dropped. The flag MUST stay separate: "" is a valid redraw, two \r's
# in a row with nothing between them.
partial=""
pending=""
pending_set=0
printf -v last_shown_at '%(%s)T' -1  # builtin; `date +%s` would fork

# Prints the pending redraw, but only once `interval` seconds have passed
# since whatever this script printed last.
show_pending_if_due() {
  (( pending_set )) || return 0
  local now
  printf -v now '%(%s)T' -1
  (( now - last_shown_at >= interval )) || return 0
  printf '%s\n' "$pending"
  last_shown_at=$now
  pending_set=0
}

# Takes one \n-free chunk and keeps only the redraw ending at its last \r.
# Earlier ones in the same chunk are already superseded, and only one could
# print per interval anyway. What follows that \r has no terminator yet, so
# it goes back to waiting in partial.
consume() {
  local chunk=$1 head
  if [[ $chunk != *$'\r'* ]]; then
    partial+=$chunk
    return 0
  fi
  head=${chunk%$'\r'*}
  if [[ $head == *$'\r'* ]]; then
    pending=${head##*$'\r'}
  else
    pending=$partial$head
  fi
  pending_set=1
  partial=${chunk##*$'\r'}
  show_pending_if_due
}

while :; do
  IFS= read -r -t "$interval" chunk
  status=$?
  consume "$chunk"

  if (( status == 0 )); then
    # \n: a complete line, printed as is; any stale redraw dies with it.
    pending_set=0
    printf '%s\n' "$partial"
    partial=""
  elif (( status > 128 )); then
    # Timed out waiting for a \n. Either the tool stalled, or it is in a
    # \r-only phase and this timeout paces the redraws. `read` hands back
    # what it got so far, so either way what is left in partial is the
    # tool's most recent state and belongs in the redraw slot.
    if [[ -n $partial ]]; then
      pending=$partial
      pending_set=1
      partial=""
    fi
    show_pending_if_due
  else
    break  # EOF, or a read failure
  fi
done

# Both can still hold real, never-shown content. pending is a redraw that
# never came due, partial is output after it. pending is the older one.
(( pending_set )) && printf '%s\n' "$pending"
[[ -n $partial ]] && printf '%s\n' "$partial"

# Under pipefail, which a step gets from `shell: bash` or `set -o pipefail`,
# a false test above MUST NOT become the whole step's exit status.
exit 0
