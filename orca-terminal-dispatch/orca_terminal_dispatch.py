#!/usr/bin/env python3
"""Registry + idle-reaper backstop for one-shot `orca terminal create` dispatches.

CLAUDE.md rule 1 requires every non-Claude AI dispatch (Grok, Gemini, Codex, ...)
to go through `orca terminal create` and to be torn down afterwards with
`orca terminal close --terminal <handle> --tab`.  Until now that teardown existed
only as prose: every dispatching agent hand-built its own two-step invocation, so
a forgotten second step silently leaked a terminal forever.

This module is the missing shared code path.  `create` records a private,
0600 registry entry describing exactly which terminal this wrapper made; `close`
tears it down and drops the entry; `reap` is an autonomous backstop for the case
where an agent died, timed out, or simply forgot to call `close`.

Safety model (mirrors ego_profile_router.py's task-space reaper):

  * POSITIVE REGISTRATION.  The reaper iterates *our registry*, never
    `orca terminal list`.  A terminal that this wrapper did not create is
    invisible to it, no matter how idle it looks.  Orca exposes no createdAt /
    createdBy / ownership metadata on terminals, so registry membership plus a
    per-dispatch title marker is the only ownership evidence that exists.
  * IDENTITY RE-VERIFICATION.  Terminal handles are runtime-issued and can be
    reused across Orca restarts, so a registry hit alone is not enough: the live
    record must still match on handle + ptyId + incarnationId + tabId + leafId,
    and its title must still carry this dispatch's unique marker token.  This is
    the analogue of Ego's `live.name !== expected.name` / createdBy / ownership
    triple check.
  * FAIL SAFE, NEVER FAIL DESTRUCTIVE.  Every ambiguity (missing agent state,
    a null lastOutputAt, a renamed title, a stale-handle answer contradicted by
    `terminal list`) defers or drops the registry entry.  Nothing is ever closed
    on a signal we could not positively confirm.

Deployment note (learned from the dead Ego reaper): the launchd copy of this
script must live on internal storage.  `~/.agents` and `~/.claude` resolve onto
`/Volumes/Extreme SSD`, and macOS TCC denies a launchd job "Files on Removable
Volumes", which silently broke `com.local.ego-taskspace-reaper` for ~2 weeks.
`install.sh` copies this file to `~/.local/bin/` (real internal disk) for that
reason; this tracked copy stays the source of truth.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any


REGISTRY_VERSION = 1
MARKER_PREFIX = "[[orca-dispatch:"
MARKER_SUFFIX = "]]"
IDENTITY_FIELDS = (
    ("handle", "handle"),
    ("pty_id", "ptyId"),
    ("incarnation_id", "incarnationId"),
    ("tab_id", "tabId"),
    ("leaf_id", "leafId"),
)
# Orca's own embedded agent-type vocabulary, used to tell "a pane Orca does not
# recognise" (safe-ish) from "a pane running a different agent than we launched"
# (never touch).
KNOWN_AGENT_TYPES = frozenset(
    [
        "claude", "codex", "gemini", "antigravity", "amp", "opencode",
        "mimo-code", "cursor", "pi", "omp", "prime-agent", "droid",
        "command-code", "grok", "copilot", "hermes", "devin", "kimi",
    ]
)
BUSY_AGENT_STATES = frozenset(["working", "waiting"])
COMMAND_SUMMARY_MAX = 200
LOG_MAX_BYTES = 128 * 1024
LAUNCHD_LOG_MAX_BYTES = 256 * 1024


def env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default
    return value if lo <= value <= hi else default


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


STATE_DIR = Path(
    os.environ.get("ORCA_TERMINAL_DISPATCH_STATE_DIR", "~/.local/state/orca-terminal-dispatch")
).expanduser()
REGISTRY_PATH = STATE_DIR / "terminals.json"
LOCK_PATH = STATE_DIR / "dispatch.lock"
LOG_PATH = STATE_DIR / "reaper.log"
LAUNCHD_LOG_PATHS = (STATE_DIR / "launchd.stdout.log", STATE_DIR / "launchd.stderr.log")

# Same env-var convention as Ego's EGO_ORCA_TASK_IDLE_SECONDS.
IDLE_SECONDS = env_int("ORCA_TERMINAL_DISPATCH_IDLE_SECONDS", 600, 60, 86400)
ORCA_TIMEOUT = env_int("ORCA_TERMINAL_DISPATCH_ORCA_TIMEOUT", 90, 10, 600)
REQUIRE_MARKER = env_flag("ORCA_TERMINAL_DISPATCH_REQUIRE_MARKER", True)
REQUIRE_AGENT_PS = env_flag("ORCA_TERMINAL_DISPATCH_REQUIRE_AGENT_PS", True)
DEFAULT_ORCA_BIN = "/Applications/Orca.app/Contents/Resources/bin/orca"

LAUNCHD_LABEL = "com.local.orca-terminal-dispatch-reaper"


class DispatchError(RuntimeError):
    pass


# ---------------------------------------------------------------- private state


def private_json_write(path: Path, value: Any) -> None:
    """Byte-for-byte the atomic 0600 write convention from ego_profile_router.py."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchError(f"cannot read private state {path}: {exc}") from exc


def load_registry() -> dict:
    value = read_json(REGISTRY_PATH, {"version": REGISTRY_VERSION, "terminals": {}})
    if not isinstance(value, dict) or value.get("version") != REGISTRY_VERSION:
        raise DispatchError("unsupported terminal dispatch registry")
    if not isinstance(value.get("terminals"), dict):
        raise DispatchError("malformed terminal dispatch registry")
    return value


@contextlib.contextmanager
def registry_lock(blocking: bool = True):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(fd, "r+")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(stream.fileno(), flags)
    except BlockingIOError:
        stream.close()
        yield None
        return
    try:
        yield stream
    finally:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def log_event(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            with LOG_PATH.open("rb") as stream:
                stream.seek(-LOG_MAX_BYTES // 2, os.SEEK_END)
                tail = stream.read().partition(b"\n")[2]
            with LOG_PATH.open("wb") as stream:
                stream.write(tail)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} {message}\n")
        os.chmod(LOG_PATH, 0o600)
    except Exception:
        pass


def trim_launchd_logs() -> None:
    """Keep launchd's own stdout/stderr bounded.

    The Ego reaper's stderr log reached 4.1 MB / 21,382 identical lines before
    anyone noticed it had been failing for two weeks.  launchd opens these with
    O_APPEND, so truncating in place is safe.
    """
    for path in LAUNCHD_LOG_PATHS:
        try:
            if path.exists() and path.stat().st_size > LAUNCHD_LOG_MAX_BYTES:
                with path.open("r+b") as stream:
                    stream.truncate(0)
        except Exception:
            pass


# ------------------------------------------------------------------- orca shell


def orca_bin() -> str:
    override = os.environ.get("ORCA_TERMINAL_DISPATCH_ORCA_BIN")
    if override:
        return override
    if os.path.exists(DEFAULT_ORCA_BIN):
        return DEFAULT_ORCA_BIN
    found = shutil.which("orca")
    if not found:
        raise DispatchError("cannot locate the `orca` CLI")
    return found


def run_orca(args: list, timeout: int | None = None) -> dict:
    """Run `orca ... --json` and return its envelope.

    Always executed through subprocess with a captured pipe; `orca --json`
    output captured through a shell command substitution is known to truncate
    silently on this machine.
    """
    argv = [orca_bin()] + list(args)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout or ORCA_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DispatchError(f"orca {' '.join(args[:2])} timed out") from exc
    except OSError as exc:
        raise DispatchError(f"cannot run the orca CLI: {exc}") from exc
    raw = (proc.stdout or "").strip()
    if not raw:
        raise DispatchError(
            f"orca {' '.join(args[:2])} produced no JSON (rc={proc.returncode}): "
            f"{(proc.stderr or '').strip()[:200]}"
        )
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DispatchError(f"orca {' '.join(args[:2])} returned non-JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise DispatchError(f"orca {' '.join(args[:2])} returned an unexpected payload")
    return envelope


def envelope_error_code(envelope: dict) -> str | None:
    error = envelope.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        return str(code) if code else "unknown"
    if error:
        return str(error)
    return None


def terminal_show(handle: str) -> tuple[str, dict | None]:
    """Return ("found"|"stale"|"stale-but-listed", live_terminal_or_None).

    `orca terminal show` is known to answer `terminal_handle_stale` for a
    terminal that is in fact alive and connected (observed 2026-08-25 on
    `terminal wait`).  A stale answer is therefore cross-checked against
    `terminal list` before it is believed, exactly as that failure note
    prescribes -- and a contradiction resolves toward "still alive", never
    toward closing something.
    """
    envelope = run_orca(["terminal", "show", "--terminal", handle, "--json"])
    if envelope.get("ok"):
        result = envelope.get("result")
        terminal = result.get("terminal") if isinstance(result, dict) else None
        if isinstance(terminal, dict):
            return "found", terminal
        return "stale", None
    try:
        listed_terminals = terminal_list()
    except DispatchError:
        # The cross-check is corroboration only.  When it cannot be run, the
        # stale answer stands -- which drops a registry entry and closes
        # nothing, so an unanswerable list still resolves non-destructively.
        listed_terminals = []
    for listed in listed_terminals:
        if listed.get("handle") == handle:
            return "stale-but-listed", listed
    return "stale", None


def terminal_list() -> list:
    """Every live terminal Orca reports, or DispatchError when it will not say.

    The failure is deliberately not swallowed here.  An empty list has to mean
    "Orca positively answered: there is nothing else out there", and a timeout,
    a truncated payload or an `ok: false` envelope turned into `[]` reads as
    exactly that confirmation -- which is how `tab_is_shared` would clear a
    whole-tab close on a tab it never actually managed to inspect.  Each caller
    decides what "unknown" means for it; both resolve it away from closing.
    """
    envelope = run_orca(["terminal", "list", "--json"])
    if not envelope.get("ok"):
        raise DispatchError(f"orca terminal list failed: {envelope_error_code(envelope)}")
    result = envelope.get("result")
    terminals = result.get("terminals") if isinstance(result, dict) else None
    if not isinstance(terminals, list):
        raise DispatchError("orca terminal list returned no terminal array")
    return [t for t in terminals if isinstance(t, dict)]


def tab_is_shared(handle: str, tab_id: str | None) -> bool:
    """True when another live terminal sits in the same tab as this one.

    `--tab` closes the *whole tab*.  A dispatched terminal normally gets its own
    tab, but if Orca ever placed one as a split pane beside somebody else's
    session, closing the tab would take that session down too.  When the tab is
    not provably ours alone the close degrades to pane-only.

    "Not provably ours alone" includes not being able to ask.  A missing tabId
    already resolves that way, and a `terminal list` that timed out or answered
    with an error is the same kind of ambiguity: it is the absence of an answer,
    never the answer "no sibling".  Treating that silence as a confirmed solo
    tab is what would let one transient CLI hiccup close somebody else's
    session, so an unreadable list degrades the close to pane-only too.
    """
    if not tab_id:
        return True
    try:
        listed_terminals = terminal_list()
    except DispatchError as exc:
        log_event(f"tab check for {handle} failed ({exc}); closing pane-only, not the tab")
        return True
    for listed in listed_terminals:
        if listed.get("handle") != handle and listed.get("tabId") == tab_id:
            return True
    return False


def close_args_for(handle: str, tab_id: str | None) -> list:
    args = ["terminal", "close", "--terminal", handle]
    if not tab_is_shared(handle, tab_id):
        args.append("--tab")
    args.append("--json")
    return args


def agent_states(limit: int = 60) -> dict | None:
    """Map "<tabId>:<leafId>" -> agent record from `orca worktree ps`.

    Returns None when the corroborating signal is unavailable, which the reaper
    treats as "defer", never as "safe to close".
    """
    try:
        envelope = run_orca(["worktree", "ps", "--limit", str(limit), "--json"])
    except DispatchError:
        return None
    if not envelope.get("ok"):
        return None
    result = envelope.get("result")
    worktrees = result.get("worktrees") if isinstance(result, dict) else None
    if not isinstance(worktrees, list):
        return None
    panes: dict = {}
    for worktree in worktrees:
        if not isinstance(worktree, dict):
            continue
        for agent in worktree.get("agents") or []:
            if isinstance(agent, dict) and isinstance(agent.get("paneKey"), str):
                panes[agent["paneKey"]] = agent
    return panes


# ------------------------------------------------------------------- identities


def owner_identity() -> tuple:
    pane_key = os.environ.get("ORCA_PANE_KEY", "")
    cwd = str(Path.cwd().resolve(strict=False))
    owner_key = f"pane:{pane_key}" if pane_key else f"cwd:{cwd}"
    return owner_key, pane_key, cwd


def make_marker(agent_kind: str) -> str:
    safe = "".join(ch for ch in agent_kind.lower() if ch.isalnum() or ch in "-_") or "agent"
    return f"{MARKER_PREFIX}{safe}:{secrets.token_hex(4)}{MARKER_SUFFIX}"


def identity_mismatches(entry: dict, live: dict) -> list:
    """Registry fields that no longer agree with the live terminal record."""
    bad = []
    for local_key, live_key in IDENTITY_FIELDS:
        expected = entry.get(local_key)
        if expected in (None, ""):
            # Never treat "we recorded nothing" as "it matches".
            bad.append(live_key)
            continue
        if live.get(live_key) != expected:
            bad.append(live_key)
    return bad


def marker_present(entry: dict, live: dict) -> bool:
    marker = entry.get("marker")
    if not isinstance(marker, str) or not marker:
        return False
    title = live.get("title")
    return isinstance(title, str) and marker in title


def pane_key_of(entry: dict) -> str | None:
    tab_id, leaf_id = entry.get("tab_id"), entry.get("leaf_id")
    if isinstance(tab_id, str) and isinstance(leaf_id, str) and tab_id and leaf_id:
        return f"{tab_id}:{leaf_id}"
    return None


def newest_activity(entry: dict, live: dict, agent: dict | None) -> float | None:
    """Newest credible activity timestamp, in epoch seconds.

    Returns None when the terminal must be considered active regardless of the
    clock.  `lastOutputAt: null` is exactly that case: a live Claude pane in
    state "done" was observed with a null lastOutputAt and an empty preview, so
    "null" must never be read as "infinitely idle".
    """
    stamps = []
    recorded = entry.get("last_activity")
    if isinstance(recorded, (int, float)):
        stamps.append(float(recorded))

    if "lastOutputAt" in live:
        last_output = live.get("lastOutputAt")
        if last_output is None:
            return None
        if isinstance(last_output, (int, float)):
            stamps.append(float(last_output) / 1000.0)
        else:
            return None

    if isinstance(agent, dict):
        for key in ("updatedAt", "stateStartedAt"):
            value = agent.get(key)
            if isinstance(value, (int, float)):
                stamps.append(float(value) / 1000.0)

    return max(stamps) if stamps else None


def entry_idle_limit(entry: dict, fallback: int) -> int:
    value = entry.get("idle_seconds")
    if isinstance(value, (int, float)) and 60 <= float(value) <= 86400:
        return int(value)
    return fallback


def evaluate_candidate(
    entry: dict,
    live_state: str,
    live: dict | None,
    agent: dict | None,
    agent_ps_available: bool,
    now: float,
    idle: int,
    require_marker: bool = True,
    require_agent_ps: bool = True,
) -> tuple:
    """Pure decision function for one registered terminal.

    Returns (action, reason).  Only the literal action "close" authorises
    `orca terminal close`; "drop" removes a registry entry without touching
    Orca; "defer" leaves everything alone and retries later.
    """
    if entry.get("created_by_this_wrapper") is not True:
        return "drop", "entry is not marked as created by this wrapper"

    if live_state == "stale-but-listed":
        return "defer", "terminal show reported a stale handle that terminal list contradicts"
    if live_state != "found" or not isinstance(live, dict):
        return "drop", "terminal no longer exists"

    bad_fields = identity_mismatches(entry, live)
    if bad_fields:
        return "drop", f"handle now belongs to a different terminal ({', '.join(bad_fields)})"

    if require_marker and not marker_present(entry, live):
        return "defer", "dispatch marker is no longer in the terminal title"

    if require_agent_ps and not agent_ps_available:
        return "defer", "agent state from worktree ps is unavailable"

    if isinstance(agent, dict):
        state = agent.get("state")
        if state in BUSY_AGENT_STATES:
            return "defer", f"agent pane is {state}"
        agent_type = agent.get("agentType")
        expected = (entry.get("agent_kind") or "").lower()
        if isinstance(agent_type, str) and agent_type.lower() != expected:
            if agent_type.lower() in KNOWN_AGENT_TYPES:
                return "defer", f"pane now runs a different agent ({agent_type})"

    activity = newest_activity(entry, live, agent)
    if activity is None:
        return "defer", "terminal reports no usable last-output timestamp"
    if now - activity < idle:
        return "defer", f"idle for {int(now - activity)}s, threshold {idle}s"

    if now < float(entry.get("defer_until", 0) or 0):
        return "defer", "entry is deferred"

    return "close", f"idle for {int(now - activity)}s past the {idle}s threshold"


def due_entries(registry: dict, now: float, idle: int) -> list:
    """Registered entries whose recorded clock alone already says they are due.

    A cheap pre-filter so an ordinary tick makes zero `orca` calls.  Every entry
    it returns is still re-checked in full against live state before anything is
    closed.
    """
    due = []
    for entry in registry["terminals"].values():
        if not isinstance(entry, dict):
            continue
        if entry.get("created_by_this_wrapper") is not True:
            continue
        recorded = entry.get("last_activity")
        if not isinstance(recorded, (int, float)):
            continue
        if now - float(recorded) < entry_idle_limit(entry, idle):
            continue
        if now < float(entry.get("defer_until", 0) or 0):
            continue
        due.append(entry)
    return due


# --------------------------------------------------------------------- commands


def capture_identity(handle: str, attempts: int = 3) -> dict:
    for index in range(attempts):
        state, live = terminal_show(handle)
        if state in ("found", "stale-but-listed") and isinstance(live, dict):
            return live
        if index + 1 < attempts:
            time.sleep(0.4)
    raise DispatchError(f"created terminal {handle} but could not read its record back")


def extract_handle(envelope: dict) -> str:
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise DispatchError("orca terminal create returned no result")
    for candidate in (result.get("terminal"), result):
        if isinstance(candidate, dict):
            handle = candidate.get("handle") or candidate.get("terminal")
            if isinstance(handle, str) and handle:
                return handle
    raise DispatchError("orca terminal create returned no terminal handle")


def command_create(args: argparse.Namespace) -> dict:
    if args.idle_seconds is not None and not 60 <= args.idle_seconds <= 86400:
        raise DispatchError("--idle-seconds must be between 60 and 86400")
    marker = make_marker(args.agent_kind)
    label = (args.title or args.purpose or args.agent_kind).strip()
    title = f"{marker} {label}".strip()

    create_args = ["terminal", "create", "--worktree", args.worktree, "--title", title]
    if args.command:
        create_args += ["--command", args.command]
    if args.focus:
        create_args.append("--focus")
    create_args.append("--json")

    envelope = run_orca(create_args, timeout=args.timeout)
    if not envelope.get("ok"):
        raise DispatchError(f"orca terminal create failed: {envelope_error_code(envelope)}")
    handle = extract_handle(envelope)

    try:
        live = capture_identity(handle)
        owner_key, pane_key, cwd = owner_identity()
        now = time.time()
        summary = (args.command or "")[:COMMAND_SUMMARY_MAX]
        entry = {
            "handle": handle,
            "created_by_this_wrapper": True,
            "marker": marker,
            "title": title,
            "agent_kind": args.agent_kind.lower(),
            "purpose": args.purpose,
            "command_summary": summary,
            "command_truncated": bool(args.command and len(args.command) > COMMAND_SUMMARY_MAX),
            "pty_id": live.get("ptyId"),
            "incarnation_id": live.get("incarnationId"),
            "tab_id": live.get("tabId"),
            "leaf_id": live.get("leafId"),
            "worktree_id": live.get("worktreeId"),
            "worktree_path": live.get("worktreePath"),
            "owner_key": owner_key,
            "orca_pane_key": pane_key,
            "cwd": cwd,
            "created_at": now,
            "last_activity": now,
            "defer_until": 0,
            "idle_seconds": args.idle_seconds if args.idle_seconds is not None else IDLE_SECONDS,
            "last_reaper_result": None,
        }
        with registry_lock() as lock:
            if lock is None:
                raise DispatchError("terminal dispatch registry is busy")
            registry = load_registry()
            registry["terminals"][handle] = entry
            private_json_write(REGISTRY_PATH, registry)
    except Exception:
        # Never leave a terminal we made outside the registry: an unregistered
        # terminal is invisible to the reaper and would leak forever.
        with contextlib.suppress(Exception):
            run_orca(["terminal", "close", "--terminal", handle, "--tab", "--json"])
        raise

    log_event(f"created {handle} kind={entry['agent_kind']} purpose={args.purpose!r}")
    return {
        "ok": True,
        "action": "created",
        "handle": handle,
        "marker": marker,
        "title": title,
        "worktree_path": entry["worktree_path"],
        "idle_seconds": entry["idle_seconds"],
        "registry": str(REGISTRY_PATH),
    }


def registered_entry(registry: dict, handle: str, any_owner: bool) -> dict:
    entry = registry["terminals"].get(handle)
    if not isinstance(entry, dict):
        raise DispatchError(f"terminal {handle} was not created by this wrapper")
    if not any_owner:
        owner_key, _pane, _cwd = owner_identity()
        if entry.get("owner_key") != owner_key:
            raise DispatchError(
                f"terminal {handle} is registered to another Orca pane (pass --any-owner to override)"
            )
    return entry


def command_close(args: argparse.Namespace) -> dict:
    with registry_lock() as lock:
        if lock is None:
            raise DispatchError("terminal dispatch registry is busy")
        registry = load_registry()
        entry = registered_entry(registry, args.terminal, args.any_owner)

        state, live = terminal_show(args.terminal)
        # "stale-but-listed" means `show` lied and the terminal is in fact alive;
        # dropping the entry there would make a live terminal invisible to the
        # reaper and leak it forever, so it is treated as found.
        if state == "stale" or not isinstance(live, dict):
            registry["terminals"].pop(args.terminal, None)
            private_json_write(REGISTRY_PATH, registry)
            log_event(f"close {args.terminal}: already gone")
            return {"ok": True, "action": "missing", "handle": args.terminal}

        bad_fields = identity_mismatches(entry, live)
        if bad_fields:
            registry["terminals"].pop(args.terminal, None)
            private_json_write(REGISTRY_PATH, registry)
            log_event(f"close {args.terminal}: identity mismatch on {bad_fields}, dropped")
            return {
                "ok": True,
                "action": "identity-mismatch",
                "handle": args.terminal,
                "mismatched": bad_fields,
                "detail": "handle now belongs to a different terminal; registry entry dropped, nothing closed",
            }

        if args.keep_tab:
            close_args = ["terminal", "close", "--terminal", args.terminal, "--json"]
        else:
            close_args = close_args_for(args.terminal, entry.get("tab_id"))
        envelope = run_orca(close_args, timeout=args.timeout)
        if not envelope.get("ok"):
            entry["last_activity"] = time.time()
            entry["defer_until"] = 0
            private_json_write(REGISTRY_PATH, registry)
            raise DispatchError(f"orca terminal close failed: {envelope_error_code(envelope)}")

        registry["terminals"].pop(args.terminal, None)
        private_json_write(REGISTRY_PATH, registry)
        log_event(f"closed {args.terminal} on request")
        return {"ok": True, "action": "closed", "handle": args.terminal}


def command_touch(args: argparse.Namespace) -> dict:
    with registry_lock() as lock:
        if lock is None:
            raise DispatchError("terminal dispatch registry is busy")
        registry = load_registry()
        entry = registered_entry(registry, args.terminal, args.any_owner)
        entry["last_activity"] = time.time()
        entry["defer_until"] = 0
        private_json_write(REGISTRY_PATH, registry)
        return {"ok": True, "action": "touched", "handle": args.terminal}


def command_status(_args: argparse.Namespace) -> dict:
    registry = load_registry()
    now = time.time()
    terminals = []
    for entry in registry["terminals"].values():
        if not isinstance(entry, dict):
            continue
        last = entry.get("last_activity")
        terminals.append(
            {
                "handle": entry.get("handle"),
                "agent_kind": entry.get("agent_kind"),
                "purpose": entry.get("purpose"),
                "idle_seconds": round(now - last) if isinstance(last, (int, float)) else None,
                "idle_limit": entry_idle_limit(entry, IDLE_SECONDS),
                "last_reaper_result": entry.get("last_reaper_result"),
            }
        )
    return {
        "ok": True,
        "idle_limit": IDLE_SECONDS,
        "registry": str(REGISTRY_PATH),
        "terminals": terminals,
    }


def command_reap(args: argparse.Namespace) -> dict:
    trim_launchd_logs()
    idle = args.idle_seconds or IDLE_SECONDS
    now = time.time()
    # Non-blocking: a tick that collides with an in-flight create/close simply
    # steps aside, mirroring Ego's "router lock is busy" deferral.
    with registry_lock(blocking=False) as lock:
        if lock is None:
            return {"ok": True, "action": "deferred", "reason": "dispatch lock is busy"}
        registry = load_registry()
        due = due_entries(registry, now, idle)
        if not due:
            return {
                "ok": True,
                "action": "noop",
                "idle_seconds": idle,
                "registered": len(registry["terminals"]),
            }

        panes = agent_states()
        agent_ps_available = panes is not None
        results = []
        changed = False

        for entry in list(due):
            handle = entry.get("handle")
            if not isinstance(handle, str) or not handle:
                continue
            # Back off on this entry's own scale: an entry that asked for a 60s
            # threshold should not be parked for the global 600s after one defer.
            limit = entry_idle_limit(entry, idle)
            try:
                state, live = terminal_show(handle)
            except DispatchError as exc:
                # One unreachable handle must not abort the whole pass, and an
                # unreadable terminal is never evidence that it can be closed.
                entry["defer_until"] = now + limit
                entry["last_reaper_result"] = f"show-failed:{exc}"
                changed = True
                results.append({"handle": handle, "action": "deferred", "reason": f"terminal show failed: {exc}"})
                continue
            pane_key = pane_key_of(entry)
            agent = (panes or {}).get(pane_key) if pane_key else None
            action, reason = evaluate_candidate(
                entry,
                state,
                live,
                agent,
                agent_ps_available,
                now,
                limit,
                require_marker=REQUIRE_MARKER,
                require_agent_ps=REQUIRE_AGENT_PS,
            )

            if action == "close" and args.dry_run:
                results.append({"handle": handle, "action": "would-close", "reason": reason})
                log_event(f"dry-run would close {handle}: {reason}")
                continue

            if action == "close":
                try:
                    envelope = run_orca(close_args_for(handle, entry.get("tab_id")))
                except DispatchError as exc:
                    entry["defer_until"] = now + limit
                    entry["last_reaper_result"] = f"close-errored:{exc}"
                    changed = True
                    results.append({"handle": handle, "action": "close-failed", "reason": str(exc)})
                    log_event(f"close errored for {handle}: {exc}")
                    continue
                if envelope.get("ok"):
                    registry["terminals"].pop(handle, None)
                    changed = True
                    results.append({"handle": handle, "action": "closed", "reason": reason})
                    log_event(f"reaped registered terminal {handle}: {reason}")
                else:
                    code = envelope_error_code(envelope)
                    entry["defer_until"] = now + limit
                    entry["last_reaper_result"] = f"close-failed:{code}"
                    changed = True
                    results.append({"handle": handle, "action": "close-failed", "reason": code})
                    log_event(f"close failed for {handle}: {code}")
            elif action == "drop":
                registry["terminals"].pop(handle, None)
                changed = True
                results.append({"handle": handle, "action": "dropped", "reason": reason})
                log_event(f"dropped registry entry {handle}: {reason}")
            else:
                entry["defer_until"] = now + limit
                entry["last_reaper_result"] = reason
                changed = True
                results.append({"handle": handle, "action": "deferred", "reason": reason})

        if changed and not args.dry_run:
            private_json_write(REGISTRY_PATH, registry)

        return {
            "ok": True,
            "action": "dry-run" if args.dry_run else "reaped",
            "idle_seconds": idle,
            "considered": len(due),
            "agent_state_available": agent_ps_available,
            "results": results,
        }


def command_doctor(_args: argparse.Namespace) -> dict:
    """Health check aimed squarely at how the Ego reaper died unnoticed."""
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    installed = Path.home() / ".local" / "bin" / "orca-terminal-dispatch"
    checks = {
        "state_dir": str(STATE_DIR),
        "state_dir_internal_volume": not str(STATE_DIR.resolve()).startswith("/Volumes/"),
        "registry_exists": REGISTRY_PATH.exists(),
        "registry_mode": oct(REGISTRY_PATH.stat().st_mode & 0o777) if REGISTRY_PATH.exists() else None,
        "installed_script": str(installed),
        "installed_script_exists": installed.exists(),
        "installed_script_internal_volume": (
            not str(installed.resolve()).startswith("/Volumes/") if installed.exists() else None
        ),
        "plist": str(plist),
        "plist_installed": plist.exists(),
        "orca_bin": None,
        "orca_reachable": False,
    }
    try:
        checks["orca_bin"] = orca_bin()
        checks["orca_reachable"] = bool(run_orca(["terminal", "list", "--json"]).get("ok"))
    except DispatchError as exc:
        checks["orca_error"] = str(exc)
    try:
        listed = subprocess.run(
            ["launchctl", "list", LAUNCHD_LABEL],
            capture_output=True, text=True, timeout=20, check=False,
        )
        checks["launchd_loaded"] = listed.returncode == 0
        if listed.returncode == 0:
            for line in listed.stdout.splitlines():
                if '"LastExitStatus"' in line:
                    checks["launchd_last_exit_status"] = line.strip().rstrip(";").split("=")[-1].strip()
    except Exception as exc:
        checks["launchd_error"] = str(exc)
    problems = []
    if not checks["state_dir_internal_volume"]:
        problems.append("state dir is on an external volume; launchd cannot read it under TCC")
    if checks["installed_script_exists"] and checks["installed_script_internal_volume"] is False:
        problems.append("installed script resolves onto an external volume (the Ego reaper failure)")
    if not checks["orca_reachable"]:
        problems.append("orca CLI is not reachable")
    if checks.get("launchd_loaded") and str(checks.get("launchd_last_exit_status", "0")) != "0":
        problems.append(f"launchd job last exited {checks.get('launchd_last_exit_status')}")
    checks["ok"] = not problems
    checks["problems"] = problems
    return checks


# ----------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orca-terminal-dispatch",
        description="Create, register, close and reap one-shot orca terminal dispatches.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create a terminal and register it")
    create.add_argument("--worktree", default="active")
    create.add_argument("--command", help="command to run in the terminal on startup")
    create.add_argument("--purpose", required=True, help="short human-readable reason")
    create.add_argument("--agent-kind", default="agent", help="grok, gemini, codex, ...")
    create.add_argument("--title", help="title text after the marker (defaults to --purpose)")
    create.add_argument("--idle-seconds", type=int, help="per-dispatch reap threshold")
    create.add_argument("--timeout", type=int, default=ORCA_TIMEOUT)
    create.add_argument("--focus", action="store_true")
    create.set_defaults(func=command_create)

    close = sub.add_parser("close", help="close a registered terminal and drop its entry")
    close.add_argument("--terminal", required=True)
    close.add_argument("--keep-tab", action="store_true", help="close the pane but keep the tab")
    close.add_argument("--any-owner", action="store_true")
    close.add_argument("--timeout", type=int, default=ORCA_TIMEOUT)
    close.set_defaults(func=command_close)

    touch = sub.add_parser("touch", help="mark a registered terminal as still in use")
    touch.add_argument("--terminal", required=True)
    touch.add_argument("--any-owner", action="store_true")
    touch.set_defaults(func=command_touch)

    reap = sub.add_parser("reap", help="close leaked registered terminals")
    reap.add_argument("--dry-run", action="store_true")
    reap.add_argument("--idle-seconds", type=int)
    reap.add_argument("--quiet", action="store_true")
    reap.set_defaults(func=command_reap)

    status = sub.add_parser("status", help="list registered terminals")
    status.set_defaults(func=command_status)

    doctor = sub.add_parser("doctor", help="check the reaper's own health")
    doctor.set_defaults(func=command_doctor)

    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except DispatchError as exc:
        result = {"ok": False, "error": str(exc)}
    quiet = bool(getattr(args, "quiet", False))
    if not quiet or result.get("action") not in ("noop", "deferred"):
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
