#!/bin/bash
# Install the two Ego cleanup launchd jobs so they survive macOS TCC.
#
#   ./install.sh              # copy scripts to internal disk, render plists, (re)load both jobs, verify
#   ./install.sh --doctor     # check only: no copying, no (re)loading
#   ./install.sh --uninstall  # unload and remove both plists (keeps the internal copies)
#
# WHY THIS EXISTS
#
# com.local.ego-taskspace-reaper and com.local.ego-idle-reaper were silently
# dead for ~14.8 days (since 2026-08-13). Their ProgramArguments named
# /usr/bin/python3 -- an Apple stub that re-execs Xcode.app's python3 -- running
# a script under ~/.agents resp. ~/claudecode, both of which are symlinks onto
# /Volumes/Extreme SSD. macOS TCC denies a launchd job "Files on Removable
# Volumes", so the interpreter could not open the script at all: LastExitStatus
# 2 and 21,380 + 21,000 lines of "[Errno 1] Operation not permitted".
#
# Measured under a real launchd job on this machine (2026-08-28):
#
#   /bin/cat            reading  /Volumes/... source   -> DENIED   (Errno 1)
#   /usr/bin/python3    running  /Volumes/... source   -> DENIED   (exit 2)
#   /opt/homebrew/...   running  /Volumes/... source   -> OK
#   /usr/bin/python3    running  internal-disk copy    -> OK
#   /opt/homebrew/...   running  internal-disk copy    -> OK
#
# So the denial is not specific to Xcode's python3: it applies to Apple platform
# binaries in the launchd session generally. Homebrew's ad-hoc-signed python3
# happens to escape it today, but that is an attribution quirk Apple can tighten
# in any OS update, it needs Homebrew to stay installed, and it still breaks
# whenever the SSD is unmounted (the old logs contain Errno 2 lines from exactly
# that). Copying to internal disk is therefore the primary fix -- it is what
# ~/.local/bin/orca-terminal-dispatch already does successfully on this machine.
# Preferring Homebrew's python3 is kept as an independent second guard.
#
# TRADE-OFF: the installed copies are snapshots. Edit a source script and you
# must re-run this installer. `--doctor` reports that drift by sha256 so it
# cannot rot unnoticed the way the original breakage did.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Canonical sources (override for testing). These legitimately live on the SSD;
# only the launchd-invoked copies must be internal.
ROUTER_SRC="${EGO_ROUTER_SRC:-$HOME/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py}"
IDLE_SRC="${EGO_IDLE_SRC:-$HOME/claudecode/本机/tools/ego_idle_reaper.py}"

LIBEXEC="$HOME/.local/libexec/ego-reaper"
ROUTER_DEST="$LIBEXEC/ego_profile_router.py"
IDLE_DEST="$LIBEXEC/ego_idle_reaper.py"
MANIFEST="$LIBEXEC/MANIFEST.json"

ROUTER_LABEL="com.local.ego-taskspace-reaper"
IDLE_LABEL="com.local.ego-idle-reaper"
ROUTER_STATE="$HOME/.local/state/ego-orca-profile"
IDLE_STATE="$HOME/.local/state"
LA="$HOME/Library/LaunchAgents"

MODE=install
for arg in "$@"; do
  case "$arg" in
    --doctor) MODE=doctor ;;
    --uninstall) MODE=uninstall ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# --- pick an interpreter -----------------------------------------------------
# Order matches ~/.local/bin/orca-terminal-dispatch's installer. Every candidate
# here works against an internal-disk copy; the first two additionally survive
# the removable-volume denial, so they are preferred.
PYTHON=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$candidate" ]; then PYTHON="$candidate"; break; fi
done
[ -n "$PYTHON" ] || { echo "no python3 interpreter found" >&2; exit 1; }

realpath_of() { "$PYTHON" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"; }
sha_of() { "$PYTHON" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; }

# ============================== uninstall ====================================
if [ "$MODE" = uninstall ]; then
  for label in "$ROUTER_LABEL" "$IDLE_LABEL"; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$LA/$label.plist"
    echo "unloaded and removed $label"
  done
  exit 0
fi

# =============================== doctor ======================================
if [ "$MODE" = doctor ]; then
  rc=0
  check_job() {
    local label="$1" src="$2" dest="$3" logdir="$4" logname="$5"
    local plist="$LA/$label.plist"
    echo "--- $label"
    if [ ! -f "$plist" ]; then echo "  FAIL plist missing: $plist"; rc=1; return; fi

    # 1. every ProgramArguments path must resolve to internal storage
    local bad
    bad="$("$PYTHON" - "$plist" <<'PY'
import plistlib, os, sys
args = plistlib.load(open(sys.argv[1], "rb")).get("ProgramArguments", [])
# Only absolute paths are filesystem references; "reap"/"--quiet" are literal
# subcommand arguments and must not be realpath'd against the caller's cwd.
print("\n".join(f"{a} -> {os.path.realpath(a)}"
                for a in args
                if a.startswith("/") and os.path.realpath(a).startswith("/Volumes/")))
PY
)"
    if [ -n "$bad" ]; then
      echo "  FAIL ProgramArguments resolves onto a removable volume (the original bug):"
      echo "$bad" | sed 's/^/    /'
      rc=1
    else
      echo "  ok   ProgramArguments all resolve to internal storage"
    fi

    # 2. installed copy present and in sync with the source of truth
    if [ ! -f "$dest" ]; then
      echo "  FAIL installed copy missing: $dest"; rc=1
    elif [ ! -r "$src" ]; then
      echo "  warn source unreadable (SSD unmounted?): $src -- cannot check drift;"
      echo "       the job keeps running against the last installed copy, by design"
    elif [ "$(sha_of "$src")" != "$(sha_of "$dest")" ]; then
      echo "  FAIL installed copy has drifted from $src -- re-run install.sh"; rc=1
    else
      echo "  ok   installed copy matches source (sha256)"
    fi

    # 3. the job's own verdict
    local status
    status="$(launchctl list | awk -v l="$label" '$3==l {print $2}')"
    if [ -z "$status" ]; then
      echo "  FAIL not loaded"; rc=1
    elif [ "$status" != "0" ]; then
      echo "  FAIL LastExitStatus=$status (2 == could not open the script)"; rc=1
    else
      echo "  ok   LastExitStatus=0"
    fi

    # 4. no TCC denial in recent stderr
    local log="$logdir/$logname"
    if [ -f "$log" ] && tail -50 "$log" 2>/dev/null | grep -q "Operation not permitted"; then
      echo "  FAIL recent 'Operation not permitted' in $log"; rc=1
    else
      echo "  ok   no recent TCC denial in $logname"
    fi
  }
  echo "interpreter: $PYTHON"
  check_job "$ROUTER_LABEL" "$ROUTER_SRC" "$ROUTER_DEST" "$ROUTER_STATE" "launchd.stderr.log"
  check_job "$IDLE_LABEL"   "$IDLE_SRC"   "$IDLE_DEST"   "$IDLE_STATE"   "ego-idle-reaper.stderr.log"
  [ "$rc" = 0 ] && echo "doctor: all checks passed" || echo "doctor: FAILURES above"
  exit "$rc"
fi

# =============================== install =====================================
for src in "$ROUTER_SRC" "$IDLE_SRC"; do
  [ -r "$src" ] || { echo "REFUSING: source unreadable: $src (SSD unmounted?)" >&2; exit 1; }
done

mkdir -p "$LIBEXEC" "$ROUTER_STATE" "$IDLE_STATE" "$LA"
chmod 700 "$LIBEXEC"
install -m 0644 "$ROUTER_SRC" "$ROUTER_DEST"
install -m 0644 "$IDLE_SRC" "$IDLE_DEST"

# --- refuse to install onto a volume launchd cannot read ---------------------
for path in "$ROUTER_DEST" "$IDLE_DEST" "$ROUTER_STATE" "$IDLE_STATE" "$PYTHON"; do
  real="$(realpath_of "$path")"
  case "$real" in
    /Volumes/*)
      echo "REFUSING: $path resolves to $real (removable volume; launchd cannot read it under TCC)" >&2
      exit 1 ;;
  esac
done

# --- prove the chosen interpreter can actually run the installed copies ------
"$PYTHON" "$ROUTER_DEST" --help >/dev/null \
  || { echo "installed router is not runnable by $PYTHON" >&2; exit 1; }
"$PYTHON" -c 'import ast,sys; ast.parse(open(sys.argv[1],encoding="utf-8").read())' "$IDLE_DEST" \
  || { echo "installed idle reaper does not parse under $PYTHON" >&2; exit 1; }

ROUTER_SRC="$ROUTER_SRC" IDLE_SRC="$IDLE_SRC" ROUTER_DEST="$ROUTER_DEST" \
IDLE_DEST="$IDLE_DEST" PYTHON="$PYTHON" "$PYTHON" - "$MANIFEST" <<'PY'
import hashlib, json, os, sys, time
def sha(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()
json.dump({
    "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "interpreter": os.environ["PYTHON"],
    "copies": [
        {"source": os.environ[s], "installed": os.environ[d],
         "sha256": sha(os.environ[d])}
        for s, d in (("ROUTER_SRC", "ROUTER_DEST"), ("IDLE_SRC", "IDLE_DEST"))
    ],
}, open(sys.argv[1], "w"), indent=2, ensure_ascii=False)
PY
echo "installed $ROUTER_DEST and $IDLE_DEST (interpreter: $PYTHON)"

# --- render + load both plists ----------------------------------------------
render_and_load() {
  local label="$1" dest="$2" state="$3"
  local plist="$LA/$label.plist"
  PYTHON="$PYTHON" DEST="$dest" STATE_DIR="$state" \
  "$PYTHON" - "$SRC_DIR/$label.plist.template" "$plist" <<'PY'
import os, sys
template, out = sys.argv[1], sys.argv[2]
text = open(template, encoding="utf-8").read()
text = (text.replace("__PYTHON__", os.environ["PYTHON"])
            .replace("__SCRIPT__", os.environ["DEST"])
            .replace("__STATE_DIR__", os.environ["STATE_DIR"]))
os.makedirs(os.path.dirname(out), exist_ok=True)
open(out, "w", encoding="utf-8").write(text)
PY
  # plutil -lint is more permissive than a strict XML parser (it accepts a
  # double hyphen inside an XML comment, which expat rejects), so validate with
  # both before loading anything.
  plutil -lint "$plist" >/dev/null
  "$PYTHON" -c 'import plistlib,sys; plistlib.load(open(sys.argv[1],"rb"))' "$plist" \
    || { echo "rendered plist is not well-formed XML: $plist" >&2; exit 1; }
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "loaded $label"
}
render_and_load "$ROUTER_LABEL" "$ROUTER_DEST" "$ROUTER_STATE"
render_and_load "$IDLE_LABEL" "$IDLE_DEST" "$IDLE_STATE"

# --- verify the jobs actually ran ------------------------------------------
# RunAtLoad fires immediately; give it a moment, then insist on LastExitStatus 0.
# Skipping this check is how the original breakage went unnoticed for 14.8 days.
sleep 5
"$0" --doctor
