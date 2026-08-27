#!/bin/bash
# Install orca-terminal-dispatch and its launchd backstop.
#
#   ./install.sh                 # install the CLI only
#   ./install.sh --load          # install the CLI and load the once-a-minute reaper
#   ./install.sh --load --dry-run-mode   # same, but the reaper only logs what it would close
#   ./install.sh --uninstall     # unload and remove the launchd job (keeps the CLI + registry)
#
# The CLI is copied to ~/.local/bin because that is real internal storage.  This
# script's own source tree lives on /Volumes/Extreme SSD, and a launchd job
# cannot read removable volumes under macOS TCC -- that is precisely how
# com.local.ego-taskspace-reaper died silently for two weeks.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SRC_DIR/orca_terminal_dispatch.py"
DEST="$HOME/.local/bin/orca-terminal-dispatch"
STATE_DIR="$HOME/.local/state/orca-terminal-dispatch"
LABEL="com.local.orca-terminal-dispatch-reaper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$SRC_DIR/$LABEL.plist.template"

LOAD=0
DRY_RUN_MODE=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --load) LOAD=1 ;;
    --dry-run-mode) DRY_RUN_MODE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ "$UNINSTALL" = 1 ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "unloaded and removed $LABEL"
  exit 0
fi

# --- pick an interpreter that can actually read the installed script ----------
PYTHON=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$candidate" ]; then PYTHON="$candidate"; break; fi
done
[ -n "$PYTHON" ] || { echo "no python3 interpreter found" >&2; exit 1; }

mkdir -p "$(dirname "$DEST")" "$STATE_DIR"
chmod 700 "$STATE_DIR"
install -m 0755 "$SRC" "$DEST"

# --- refuse to install onto a volume launchd cannot read ---------------------
for path in "$DEST" "$STATE_DIR"; do
  real="$($PYTHON -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$path")"
  case "$real" in
    /Volumes/*)
      echo "REFUSING: $path resolves to $real (external volume; launchd cannot read it under TCC)" >&2
      exit 1 ;;
  esac
done

# --- prove the chosen interpreter can run the installed copy -----------------
"$PYTHON" "$DEST" --help >/dev/null || { echo "installed script is not runnable by $PYTHON" >&2; exit 1; }
echo "installed $DEST (interpreter: $PYTHON)"

if [ "$LOAD" = 1 ]; then
  EXTRA_ARGS=""
  if [ "$DRY_RUN_MODE" = 1 ]; then
    EXTRA_ARGS=$'\t\t<string>--dry-run</string>\n'
  fi
  PYTHON="$PYTHON" DEST="$DEST" STATE_DIR="$STATE_DIR" EXTRA_ARGS="$EXTRA_ARGS" \
  "$PYTHON" - "$TEMPLATE" "$PLIST" <<'PY'
import os, sys
template, out = sys.argv[1], sys.argv[2]
text = open(template, encoding="utf-8").read()
text = (text.replace("__PYTHON__", os.environ["PYTHON"])
            .replace("__SCRIPT__", os.environ["DEST"])
            .replace("__STATE_DIR__", os.environ["STATE_DIR"])
            .replace("__EXTRA_ARGS__", os.environ["EXTRA_ARGS"]))
os.makedirs(os.path.dirname(out), exist_ok=True)
open(out, "w", encoding="utf-8").write(text)
PY
  plutil -lint "$PLIST" >/dev/null
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "loaded $LABEL (dry-run mode: $DRY_RUN_MODE)"
fi

"$PYTHON" "$DEST" doctor
