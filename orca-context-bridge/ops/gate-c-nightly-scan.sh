#!/usr/bin/env bash
#
# gate-c-nightly-scan.sh -- TEMPLATE ONLY. Not installed, not registered
# with launchd/cron by anything in this repo. This file exists purely so a
# human can review the exact command a scheduled Gate C scan would run
# before deciding to install it themselves.
#
# WHAT THIS RUNS, AND WHAT IT DELIBERATELY NEVER DOES
# ----------------------------------------------------------------------------
# `discover_capability_candidates.py scan --all-projects` -- a read-only
# filesystem walk over every project catalog.json already knows about (see
# that script's own module docstring, SCOPE section). This wrapper NEVER
# passes --authorize-production-write, so every run lands in M8-2's
# staging-default output directory
# (manifests/capability-discovery-pending-authorization/discovery-hits.json),
# never the real production path -- M8-2's own independent authorization
# gate (design 3.3.3) has not been granted (44-72% AI-judged false-positive
# rate on real data), and this script does not grant it. A human still
# reviews every hit via review_capability_candidates.py before anything
# reaches promote_capability.py draft-from-discovery-hit.
#
# This script does not call `launchctl load`, `crontab`, or install a git
# hook anywhere. It is inert until a human takes the install step below
# themselves.
#
# TO INSTALL (a human runs this, not this script):
# ----------------------------------------------------------------------------
#   1. Review this file and the companion launchd template
#      (com.orca.gate-c-nightly-scan.plist) in this same directory.
#   2. Copy the plist into place and point its ProgramArguments at this
#      script's real, deployed path (edit the placeholder path in the plist
#      first -- it is not filled in for you):
#        cp com.orca.gate-c-nightly-scan.plist ~/Library/LaunchAgents/
#   3. Load it:
#        launchctl load ~/Library/LaunchAgents/com.orca.gate-c-nightly-scan.plist
#   4. To remove it later:
#        launchctl unload ~/Library/LaunchAgents/com.orca.gate-c-nightly-scan.plist
#        rm ~/Library/LaunchAgents/com.orca.gate-c-nightly-scan.plist
#
# (A plain crontab entry works too, if launchd is not preferred on this
# machine -- e.g. `0 3 * * * /path/to/gate-c-nightly-scan.sh` for a 3am
# daily run -- but this repo's own convention for macOS-scheduled work
# elsewhere in the tree favors launchd, hence the .plist template.)

set -euo pipefail

# Fixed absolute path, not derived from this script's own location -- same
# rationale as every "fixed absolute path" constant in the Python tools this
# script wraps (see discover_capability_candidates.py's own module
# docstring, WRITE SURFACE section): a script relocated to a different
# checkout, or invoked with an unexpected $PWD by launchd/cron, must still
# scan and write the same real locations every time.
readonly SCRIPT_PATH="/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/scripts/discover_capability_candidates.py"
readonly CATALOG_PATH="/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/catalog.json"
readonly LOG_DIR="/Volumes/Extreme SSD/Orca/manifests/capability-discovery-pending-authorization/runs"

mkdir -p "$LOG_DIR"
log_file="$LOG_DIR/nightly-scan-$(date -u +%Y%m%dT%H%M%SZ).log"

# --all-projects: resolve scan roots from catalog.json's own projects[]
#   (never a fresh `orca repo list`/`worktree list` call -- see that flag's
#   own docstring). Prints its own unsuppressible "this may take seconds to
#   ~5.5 minutes" warning to stderr, captured into the log file below.
# --catalog: explicit, not the script's own --catalog default, so this
#   template's behavior does not silently change if that default ever does.
# --json --quiet: machine-readable summary only, no human-facing prose, into
#   the per-run log file rather than launchd's own stdout/stderr capture.
# NO --authorize-production-write ANYWHERE ON THIS LINE. This is the one
# invariant this whole file exists to hold: a nightly/idle-time run must
# only ever write to the staging default, never the real production path.
#
# `set -e` is suspended around this one call: the whole point of this
# wrapper is to log discover_capability_candidates.py's own exit code (its
# generator-convention exit codes -- 0/2/4, see that script's own module
# docstring -- are meaningful, not just "did it crash"), which requires
# capturing $? rather than letting `set -e` tear the script down on a
# nonzero exit before that capture ever runs.
set +e
python3 "$SCRIPT_PATH" scan \
  --all-projects \
  --catalog "$CATALOG_PATH" \
  --json \
  --quiet \
  >>"$log_file" 2>&1
exit_code=$?
set -e

echo "gate-c-nightly-scan.sh: discover_capability_candidates.py scan exited $exit_code (log: $log_file)" >>"$log_file"
exit "$exit_code"
