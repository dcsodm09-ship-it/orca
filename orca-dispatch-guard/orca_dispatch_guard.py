#!/usr/bin/env python3
"""orca_dispatch_guard.py -- userspace workaround for a confirmed Orca bug:
dispatching to a terminal that is ALREADY in the "working" PTY state at the
exact moment of submission triggers a guaranteed false-positive
`agent_prompt_stalled` error, which permanently revokes that dispatch's
capability token. The genuinely-completing worker's real eventual
`worker_done` then gets rejected ("capability is revoked" /
`dispatch_capability_invalid`), silently losing a real, correct result.

This is a userspace workaround only. The real fix
(`waitForBusyAgentPromptSettlement()`, committed as `ee982fc0a8` in a
different worktree of the same Orca app source) requires recompiling Orca's
own app bundle, which is explicitly out of scope here. This module instead
becomes the path future dispatches go through INSTEAD OF raw
`orca orchestration worker-start`, detecting the false-positive signature and
running a safe recovery flow when it happens.

SCOPE / KNOWN LIMITATION -- READ BEFORE RELYING ON THIS TOOL
------------------------------------------------------------------
This wrapper only covers dispatches that go through its own `start`
subcommand. It provides NO protection for:
  * direct human-UI-driven dispatches through the Orca app itself,
  * any other process invoking `orca orchestration worker-start` directly.
Those paths remain exposed to the exact race this module exists to route
around. This is a deliberate, documented scope boundary (per the design
panel's own explicit "out of scope" list), not an oversight: no background
daemon, no worker-side `orca` PATH shim, no coverage for human-UI dispatches
are built here.

DETECTION -- TEXT-SUBSTRING, BUT NEVER OVER CALLER-ECHOED FIELDS
------------------------------------------------------------------
We have no captured ground-truth JSON from a real occurrence of this bug on
this machine's current Orca version. Rather than guess at an exact field
path (which could easily be wrong and silently fail to detect the very bug
this tool exists to catch), detection searches the stdout/stderr text for the
literal substrings `"agent_prompt_stalled"` (the false-positive stall
signature) and `"dispatch_capability_invalid"` / `"capability is revoked"`
(the downstream capability-revocation symptom). `stage`/`failedStage` ARE
named in the real `orca` CLI's own `--help` text, so when present in a parsed
JSON body they are captured and surfaced as diagnostics (see
`extract_stage_diagnostics()`) -- but detection itself never requires them to
hold any particular value, since that value has not been confirmed against a
real occurrence.

Structure IS used for one thing: deciding what NOT to search. When a stream
parses as JSON, every caller-supplied echoed-text field in it (`spec`,
`taskSpec`, `title`, `objective`, ... -- see `_ECHOED_CALLER_TEXT_FIELDS`) is
stripped before the substring search runs. Dispatch specs written in this
project routinely quote these very phrases (a task whose whole job is
"investigate the agent_prompt_stalled false positive"), so a worker-start
failing for an unrelated reason, with an error body echoing the submitted
spec, used to be misclassified as the stall -- and `_do_start()` would then
"recover" it into a real duplicate task + dispatch. See
`is_stalled_false_positive()` for why the reverse (gating on `failedStage`'s
VALUE) is deliberately not done.

Similarly, this module has no confirmed field name for "the dispatch id"
inside `worker-start`'s own JSON response for the SPECIFIC agent_prompt_stalled
occurrence (no captured ground truth for that exact bug on this machine).
`extract_dispatch_id()` tries a short list of plausible field names
(documented there) and, if none match, recovery refuses to proceed rather
than guessing an id and journaling or retrying against the wrong thing --
fail-closed, per this project's own established convention.

CONFIRMED REAL ENVELOPE SHAPE (2026-08-26, live `orca` binary in this
worktree, unmocked, commands and raw output recorded in this round's review
notes)
------------------------------------------------------------------
Every real `orca ... --json` call observed -- `orchestration task-list`,
`orchestration run-list`, `orchestration run-create` (missing arg),
`orchestration worker-start` (missing arg; unknown task), `terminal list`,
`orchestration task-create`, `orchestration check --wait`, `orchestration
worker-list` -- wraps its payload one level down from the bare top level:
    success:  {"id": <rpc-tracking-id>, "ok": true,  "result": {...}, "_meta": {...}}
    failure:  {"id": <rpc-tracking-id>, "ok": false, "error":  {"code":..., "message":...}, "_meta": {...}}
The top-level "id" key is the RPC call's OWN tracking id (a fresh uuid or
the literal string "local"), never a task/dispatch id -- code below must
never treat it as one. Confirmed nested field names actually observed:
`result.task.id` (task-create), `result.tasks[].id` (task-list),
`result.dispatchId` / `result.taskId` (worker-list, camelCase), `result.
messages[]` + `result.deliveryId` (orchestration check). Every extraction
helper below checks this confirmed nested shape FIRST; only when the parsed
body has neither a "result" nor an "error" key at all (i.e. does not look
like this wrapped envelope in the first place) does it fall back to
treating the body as an older/unwrapped flat shape -- it must never fall
back to scanning the bare top level of a body that DOES have "result"/
"error", since that top level is exactly where the misleading RPC tracking
id lives (the original bug this round fixes).

STATE ON DISK
------------------------------------------------------------------
STATE_ROOT (default `~/.orca/dispatch-guard/`, a rebindable module constant
so tests never touch the real path -- see test_orca_dispatch_guard.py's own
convention, matching promote_capability.py's PROMOTION_ROOT) holds two kinds
of state:

  locks/<sha256(terminal_handle)[:32]>.lock
    A POSIX advisory lock (fcntl.flock) held across the ENTIRE
    submit-or-recover flow for one terminal handle -- not just the recovery
    half. This was the synthesis's explicit tie-break over a narrower
    recovery-only lock: holding the lock only during recovery would still
    let two concurrent `start` calls against the same terminal race each
    other's INITIAL `worker-start` submissions, which is the exact
    busy-terminal condition that triggers the underlying bug in the first
    place.

  journal/<sha256(original_dispatch_id)[:32]>.json
    One JSON record per original (first-attempt) dispatch id, capped at
    MAX_REMOUNT_COUNT remount attempts. Filename is hashed rather than the
    raw dispatch id -- same treatment as locks/ above -- since the id
    ultimately comes from parsing another process's JSON output and must
    never be trusted as a filename component; the raw original_dispatch_id
    is still recorded inside the entry's own JSON content. See
    `_journal_path()`, `write_journal_atomic()` and
    `_recover_from_stalled_false_positive()`.

Run with:
    python3 orca_dispatch_guard.py start --task <id> --terminal <handle> [--run <id>]
    python3 orca_dispatch_guard.py wait --run <id> --dispatch <id> [--dispatch <id> ...]
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

# ---------------------------------------------------------------------------
# Rebindable module constants -- tests monkeypatch these directly (never a
# CLI override), matching this codebase's established convention (see
# promote_capability.py's PROMOTION_ROOT / detect_capability_changes.py).
# ---------------------------------------------------------------------------

STATE_ROOT = Path.home() / ".orca" / "dispatch-guard"
LOCKS_DIRNAME = "locks"
JOURNAL_DIRNAME = "journal"

ORCA_BIN = "orca"

# Cap on remount attempts per original dispatch id (matches the synthesis).
MAX_REMOUNT_COUNT = 3

# How long `orca terminal wait --for tui-idle` is given before we conclude
# "possible genuine stall OR task still genuinely running" -- generous on
# purpose, since a real task may still be legitimately running.
TUI_IDLE_WAIT_TIMEOUT_MS = 300_000  # 5 minutes

# Default bound on how long `start` will wait to ACQUIRE the per-terminal
# lock before giving up (non-blocking acquire + bounded backoff, never a
# blocking-forever wait). Generous enough that a concurrent `start` call
# already in the middle of a legitimate 5-minute tui-idle wait does not get
# spuriously starved out.
DEFAULT_LOCK_ACQUIRE_TIMEOUT_SECONDS = 400.0
LOCK_POLL_INTERVAL_SECONDS = 0.25

# `wait` subcommand: overall timeout budget, and the per-call chunk size fed
# to `orca orchestration check --wait --timeout-ms`.
DEFAULT_WAIT_TIMEOUT_MS = 600_000  # 10 minutes
WAIT_CHECK_CHUNK_MS = 30_000

# Bound on how long `wait`'s own "mark journal recovered" step will wait to
# acquire the SAME per-terminal TerminalLock `start`'s recovery path holds,
# before giving up on just that bookkeeping write. The real worker_done
# match has already been established (and acked) independently of any
# journal by the time this lock is taken, so a timeout here is reported on
# stderr but still lets `wait` report its already-genuine EXIT_OK match --
# it only means this ONE journal record may still read "recovering" instead
# of "recovered" afterward. Deliberately much shorter than `start`'s own
# DEFAULT_LOCK_ACQUIRE_TIMEOUT_SECONDS: recovery only holds the lock for the
# bounded duration of one remount attempt, not this command's entire
# multi-minute wait budget.
WAIT_JOURNAL_LOCK_TIMEOUT_SECONDS = 30.0

# Exit codes -- documented explicitly so a caller can tell these apart.
EXIT_OK = 0
EXIT_FAILURE = 1  # genuine failure passthrough (same code worker-start itself
# would have reported), OR a guard-internal fatal condition (lock timeout,
# unresolvable dispatch id, task-create/remount plumbing failure).
EXIT_USAGE = 2
EXIT_RETRY_LATER = 3  # tui-idle wait did not report idle within the window:
# possible genuine stall OR task still genuinely running past 5 minutes --
# not safe to remount yet, caller should retry `start` again later.
EXIT_GAVE_UP = 4  # remount_count already at MAX_REMOUNT_COUNT: refused to
# remount again, journal marked "gave_up", needs human attention.


# ---------------------------------------------------------------------------
# Detection -- pure functions, zero subprocess calls, unit-testable in
# isolation.
# ---------------------------------------------------------------------------


STALL_SIGNATURES = ("agent_prompt_stalled",)
CAPABILITY_REVOKED_SIGNATURES = ("dispatch_capability_invalid", "capability is revoked")

# Field names whose values are CALLER-SUPPLIED text that the `orca` CLI
# echoes back verbatim in its own JSON bodies -- a task's spec, its title,
# a run objective. Names are matched normalized (lowercased, "_"/"-"
# stripped), so `taskSpec`, `task_spec` and `TASK-SPEC` all match one entry.
#
# Detection must never search these: dispatch specs written by this very
# project routinely quote the literal strings below (e.g. a task whose whole
# job is "investigate the agent_prompt_stalled false positive"), so a
# worker-start that fails for a COMPLETELY unrelated reason -- with an error
# body that happens to echo the submitted spec back -- would otherwise be
# misread as the false-positive stall this module exists to detect, and
# `_do_start()` would "recover" it by creating a task and dispatching for
# real. That is a duplicate real dispatch caused purely by a caller's own
# choice of words, which is why this exclusion matters more here than in the
# read-side matching helpers.
#
# Deliberately conservative membership: only fields confirmed to carry
# caller-authored text in the real task shape (`orchestration task-list
# --json` -> `result.tasks[]` has `spec`, `task_title`, `display_name`) plus
# their obvious spelling variants. CLI-authored fields (`code`, `message`,
# `reason`, `stage`, `failedStage`, ...) are all still searched.
_ECHOED_CALLER_TEXT_FIELDS = frozenset(
    {
        "spec",
        "taskspec",
        "specification",
        "prompt",
        "prompttext",
        "preamble",
        "instruction",
        "instructions",
        "title",
        "tasktitle",
        "displayname",
        "objective",
    }
)


def _normalize_field_name(name: str) -> str:
    return name.replace("_", "").replace("-", "").lower()


def _strip_echoed_caller_text(value: Any) -> Any:
    """Recursively drop every `_ECHOED_CALLER_TEXT_FIELDS` key from a parsed
    JSON value. String values that are themselves JSON-encoded objects/arrays
    are decoded, stripped and re-encoded -- the `payload` field of a real
    message arrives in exactly that shape (see
    `_extract_dispatch_id_from_worker_done`), so a spec echoed one level
    deeper inside it must be excluded too."""
    if isinstance(value, dict):
        return {
            key: _strip_echoed_caller_text(item)
            for key, item in value.items()
            if not (isinstance(key, str) and _normalize_field_name(key) in _ECHOED_CALLER_TEXT_FIELDS)
        }
    if isinstance(value, list):
        return [_strip_echoed_caller_text(item) for item in value]
    if isinstance(value, str):
        nested = _parse_json_value_loose(value)
        if isinstance(nested, (dict, list)):
            return json.dumps(_strip_echoed_caller_text(nested), ensure_ascii=False)
    return value


def _cli_authored_text(raw: str | None) -> str:
    """`raw` reduced to the parts the CLI itself authored: when `raw` parses
    as a JSON object/array, its caller-echoed text fields are stripped and it
    is re-serialized; otherwise `raw` is returned unchanged.

    KNOWN RESIDUAL LIMITATION: a non-JSON stderr line that interpolates a
    caller's spec into free text ("failed to start task with spec: ...")
    offers no structure to strip, so it is still searched whole. That is
    narrower than the previous behaviour (which searched caller text even
    when it WAS cleanly separated into its own JSON field) but not zero."""
    if not raw:
        return ""
    parsed = _parse_json_value_loose(raw)
    if not isinstance(parsed, (dict, list)):
        return raw
    return json.dumps(_strip_echoed_caller_text(parsed), ensure_ascii=False)


def _signature_present(raw_stdout: str | None, raw_stderr: str | None, signatures: tuple[str, ...]) -> bool:
    """True iff any signature appears in the CLI-authored part of either
    stream. Each stream is examined separately (rather than concatenated as
    before) because each is independently either a JSON body to strip or
    free text -- no signature spans the boundary between them."""
    for raw in (raw_stdout, raw_stderr):
        text = _cli_authored_text(raw)
        if any(signature in text for signature in signatures):
            return True
    return False


def is_stalled_false_positive(raw_stdout: str, raw_stderr: str, exit_code: int) -> bool:
    """True iff the CLI-authored part of stdout/stderr contains the literal
    substring "agent_prompt_stalled" -- i.e. the same substring search as
    before, but with caller-echoed text fields (`spec`/`taskSpec`/`title`/
    ..., see `_ECHOED_CALLER_TEXT_FIELDS`) excluded from what is searched, so
    a task whose OWN description merely mentions the phrase can no longer
    trigger a recovery (and therefore a duplicate real dispatch).

    We deliberately still do NOT require `stage`/`failedStage` to EQUAL any
    particular string. Those fields are captured for diagnostics
    (`extract_stage_diagnostics()`), but using their value as the gate --
    "stalled iff failedStage == 'agent_prompt_stalled'" -- would be a guess:
    no ground-truth body from a real occurrence of this bug has been captured
    on this machine, and a real occurrence reporting the signature in
    `message` while `failedStage` says something coarser (e.g. "settle")
    would then go undetected. Missing the bug is the worse failure of the
    two, so structure is used to decide WHERE to search, never to veto a
    signature the CLI did emit.

    `exit_code` gates the match to a non-zero exit: a successful
    `worker-start` (exit 0) reporting this substring somewhere in its own
    output (e.g. echoing task spec text that happens to mention it) is not a
    failure at all and must not be treated as one. This is an additional
    safety condition this function adds beyond the bare substring search,
    not a narrowing of the substring itself -- if a genuine occurrence of
    this bug ever exits 0, this deliberately conservative choice would miss
    it, but a stall that revokes the capability token is understood to be a
    failure mode of the CLI's own exit code as documented ("Failed ...
    exits 1").
    """
    if exit_code == 0:
        return False
    return _signature_present(raw_stdout, raw_stderr, STALL_SIGNATURES)


def is_capability_revoked_failure(raw_stdout: str, raw_stderr: str) -> bool:
    """True iff the CLI-authored part of stdout/stderr contains the literal
    substring "dispatch_capability_invalid" or "capability is revoked" -- the
    downstream symptom of a worker's real, eventual `worker_done` being
    rejected because this exact race already burned its capability token.

    Same caller-echo exclusion as `is_stalled_false_positive()` above, for
    the same reason: a message that merely quotes a spec naming these
    phrases is not itself a revocation."""
    return _signature_present(raw_stdout, raw_stderr, CAPABILITY_REVOKED_SIGNATURES)


def _is_wrapped_envelope(parsed: dict[str, Any]) -> bool:
    """True iff `parsed` looks like the confirmed real `orca ... --json`
    envelope -- i.e. it has a "result" and/or "error" key (see the
    CONFIRMED REAL ENVELOPE SHAPE note in the module docstring). This gates
    every extraction helper below: when true, they read ONLY the nested
    `result`/`error` bodies and never the bare top level (whose own "id" key
    is the outer RPC tracking id, not a task/dispatch id -- falling back to
    it was the original bug). Only when this is false (a body that does not
    even look like the wrapped shape) do the helpers fall back to treating
    `parsed` itself as an older/unwrapped flat body."""
    return "result" in parsed or "error" in parsed


def extract_stage_diagnostics(parsed: dict[str, Any] | None) -> dict[str, Any]:
    """Best-effort capture of `stage`/`failedStage` for diagnostics only --
    never used to gate detection (see module docstring). Per `orca
    orchestration worker-start --help` these are documented as living in the
    call's own JSON body on a failed/outcome_unknown result; on the
    confirmed real envelope that body is `result` (success) or `error`
    (input-validation failures) -- checked in that order, first key found
    wins over a later duplicate."""
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, Any] = {}
    if _is_wrapped_envelope(parsed):
        for container_key in ("result", "error"):
            container = parsed.get(container_key)
            if not isinstance(container, dict):
                continue
            for key in ("stage", "failedStage"):
                if key in container and key not in out:
                    out[key] = container[key]
        return out
    for key in ("stage", "failedStage"):
        if key in parsed:
            out[key] = parsed[key]
    return out


def extract_dispatch_id(parsed: dict[str, Any] | None) -> str | None:
    """Best-effort extraction of a dispatch id from a parsed `worker-start`
    JSON body. On the confirmed real envelope, live `orchestration
    worker-list` output shows the canonical field name is camelCase
    `dispatchId`, sitting directly on `result` (not nested under a further
    "dispatch" sub-object) -- this is checked first. A `result.dispatch.*`
    nesting is still checked as a documented-but-unconfirmed fallback for
    this exact `worker-start` response shape (no captured ground truth for
    the specific agent_prompt_stalled occurrence -- see module docstring).
    Returns None (never a guess) if nothing matches; callers must treat None
    as "cannot safely identify this dispatch" and refuse to journal/retry
    rather than fabricate an id."""
    if not isinstance(parsed, dict):
        return None
    if _is_wrapped_envelope(parsed):
        result = parsed.get("result")
        result = result if isinstance(result, dict) else {}
        for key in ("dispatchId", "dispatch_id", "dispatchID"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        nested = result.get("dispatch")
        if isinstance(nested, dict):
            for key in ("id", "dispatchId", "dispatch_id"):
                value = nested.get(key)
                if isinstance(value, str) and value:
                    return value
        # Deliberately does NOT fall further to the bare top level here --
        # that is the outer RPC tracking id, not a dispatch id.
        return None
    for key in ("dispatch_id", "dispatchId", "dispatchID"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            return value
    nested = parsed.get("dispatch")
    if isinstance(nested, dict):
        for key in ("id", "dispatch_id"):
            value = nested.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def extract_task_id(parsed: dict[str, Any] | None) -> str | None:
    """Extraction of a task id from a parsed `task-create` JSON body. On the
    confirmed real envelope (live `orchestration task-create --json`
    output), the task id lives at `result.task.id` -- checked first. A flat
    `result.taskId`/`result.id` is also checked as a fallback in case some
    other subcommand's task-bearing response is flatter. Deliberately never
    falls back to the bare top-level `id` when a wrapped envelope is
    present -- that is the outer RPC tracking id (a fresh uuid on every
    call), and returning it here reproduces the exact P0 this round fixes:
    a plausible-looking but wrong id that a caller then dispatches against."""
    if not isinstance(parsed, dict):
        return None
    if _is_wrapped_envelope(parsed):
        result = parsed.get("result")
        result = result if isinstance(result, dict) else {}
        task_obj = result.get("task")
        if isinstance(task_obj, dict):
            for key in ("id", "task_id", "taskId"):
                value = task_obj.get(key)
                if isinstance(value, str) and value:
                    return value
        for key in ("taskId", "task_id", "id"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        return None
    for key in ("task_id", "taskId", "id"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            return value
    nested = parsed.get("task")
    if isinstance(nested, dict):
        for key in ("id", "task_id"):
            value = nested.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def extract_delivery_id(parsed: dict[str, Any] | None) -> str | None:
    """Extraction of the BATCH delivery id from a parsed `orchestration
    check` JSON body, for use with `orchestration check --ack`. On the
    confirmed real envelope (live `orchestration check --wait --json`
    output) this is `result.deliveryId` -- one id per check call/batch, NOT
    a per-message field (individual messages carry their own unrelated "id",
    e.g. "msg_...", which must never be used as the ack id)."""
    if not isinstance(parsed, dict):
        return None
    if _is_wrapped_envelope(parsed):
        result = parsed.get("result")
        result = result if isinstance(result, dict) else {}
        for key in ("deliveryId", "delivery_id"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        return None
    for key in ("deliveryId", "delivery_id"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_json_value_loose(text: str | None) -> Any:
    """Parse `text` as JSON, returning None instead of raising on anything
    that is not valid JSON. Unlike `_parse_json_loose()` below, ANY JSON
    value is returned (a list, a bare string, a number), not just an object
    -- the caller decides what shapes it accepts."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _parse_json_loose(text: str | None) -> dict[str, Any] | None:
    obj = _parse_json_value_loose(text)
    return obj if isinstance(obj, dict) else None


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _print_json_stdout(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _print_json_stderr(obj: Any) -> None:
    sys.stderr.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Per-terminal locking (fcntl.flock, non-blocking acquire + bounded backoff)
# ---------------------------------------------------------------------------


class TerminalLockTimeout(Exception):
    """Could not acquire the per-terminal lock within the bounded wait."""


def _lock_path_for_terminal(terminal_handle: str) -> Path:
    digest = hashlib.sha256(terminal_handle.encode("utf-8")).hexdigest()[:32]
    return STATE_ROOT / LOCKS_DIRNAME / f"{digest}.lock"


class TerminalLock:
    """Advisory, per-terminal-handle lock via fcntl.flock on a dedicated
    lock file. Deliberately `fcntl.flock` (associated with the OPEN FILE
    DESCRIPTION, not the process) rather than `fcntl.lockf`/F_SETLK
    (associated with the process): this makes the lock genuinely serialize
    two concurrent acquisitions from within the SAME process (e.g. two
    threads, or two independent `TerminalLock` context managers), which
    matters for tests exercising real concurrency, not just two separate
    processes.

    Non-blocking acquire attempts in a bounded poll loop -- never a single
    blocking `flock()` call -- so a caller can never hang forever if the
    lock is somehow never released (e.g. a crashed prior holder that never
    exited, hence never had its fd closed by the OS)."""

    def __init__(
        self,
        terminal_handle: str,
        *,
        timeout_seconds: float = DEFAULT_LOCK_ACQUIRE_TIMEOUT_SECONDS,
        poll_interval: float = LOCK_POLL_INTERVAL_SECONDS,
    ) -> None:
        self.terminal_handle = terminal_handle
        self.path = _lock_path_for_terminal(terminal_handle)
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval
        self._fd: int | None = None

    def __enter__(self) -> "TerminalLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise TerminalLockTimeout(
                        f"could not acquire dispatch lock for terminal {self.terminal_handle!r} "
                        f"within {self.timeout_seconds}s: {self.path}"
                    )
                time.sleep(self.poll_interval)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        return False


# ---------------------------------------------------------------------------
# Idempotency journal -- one JSON file per ORIGINAL dispatch id.
# ---------------------------------------------------------------------------


def _journal_path(original_dispatch_id: str) -> Path:
    """Same treatment as `_lock_path_for_terminal()`: hash the untrusted
    identifier before using it as a filename component, rather than
    blacklisting dangerous characters. `original_dispatch_id` ultimately
    comes from `extract_dispatch_id()` on a real `worker-start` JSON body --
    an attacker-influenced or malformed CLI response containing something
    like "../../evil/pwned" must never let a filename component escape
    STATE_ROOT. The full, unhashed original_dispatch_id is still recorded
    inside the journal entry's own JSON content, so nothing is lost for
    debugging -- only the ON-DISK FILENAME is hashed."""
    digest = hashlib.sha256(original_dispatch_id.encode("utf-8")).hexdigest()[:32]
    return STATE_ROOT / JOURNAL_DIRNAME / f"{digest}.json"


def read_journal(original_dispatch_id: str) -> dict[str, Any] | None:
    path = _journal_path(original_dispatch_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def write_journal_atomic(entry: dict[str, Any]) -> None:
    path = _journal_path(entry["original_dispatch_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    tmp_path.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp_path), str(path))


def find_journal_by_any_dispatch_id(dispatch_id: str) -> dict[str, Any] | None:
    """Look up a journal entry either by its own original_dispatch_id, or by
    scanning for one whose remount_dispatch_ids[] contains this id -- used by
    `wait` to update the right journal record to "recovered" regardless of
    whether the caller is watching the original id or one of its remounts."""
    direct = read_journal(dispatch_id)
    if direct is not None:
        return direct
    journal_dir = STATE_ROOT / JOURNAL_DIRNAME
    if not journal_dir.is_dir():
        return None
    for entry_path in sorted(journal_dir.glob("*.json")):
        try:
            raw = entry_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and dispatch_id in (data.get("remount_dispatch_ids") or []):
            return data
    return None


# ---------------------------------------------------------------------------
# Thin subprocess wrappers around the real `orca` CLI. Every call site in
# this module goes through `_run_orca()` so tests can mock `subprocess.run`
# once and control every interaction.
# ---------------------------------------------------------------------------


def _run_orca(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([ORCA_BIN, *args], capture_output=True, text=True, timeout=timeout)


def _worker_start(*, task_id: str, terminal: str, run_id: str | None, retry_of: str | None) -> subprocess.CompletedProcess:
    args = ["orchestration", "worker-start", "--task", task_id, "--terminal", terminal, "--json"]
    if run_id:
        args += ["--run", run_id]
    if retry_of:
        args += ["--retry-of", retry_of]
    return _run_orca(args)


def _task_create(*, spec: str, run_id: str | None, title: str | None) -> subprocess.CompletedProcess:
    args = ["orchestration", "task-create", "--spec", spec, "--json"]
    if title:
        args += ["--task-title", title]
    if run_id:
        args += ["--run", run_id]
    return _run_orca(args)


def _terminal_wait_tui_idle(terminal: str, *, timeout_ms: int) -> subprocess.CompletedProcess:
    return _run_orca(
        ["terminal", "wait", "--terminal", terminal, "--for", "tui-idle", "--timeout-ms", str(timeout_ms), "--json"],
        timeout=(timeout_ms / 1000.0) + 30.0,
    )


def _terminal_read_tail(terminal: str, *, limit: int) -> subprocess.CompletedProcess:
    return _run_orca(["terminal", "read", "--terminal", terminal, "--limit", str(limit), "--json"])


def _orchestration_check(*, run_id: str, timeout_ms: int) -> subprocess.CompletedProcess:
    return _run_orca(
        ["orchestration", "check", "--run", run_id, "--wait", "--types", "worker_done", "--timeout-ms", str(timeout_ms), "--json"],
        timeout=(timeout_ms / 1000.0) + 30.0,
    )


def _orchestration_ack(*, run_id: str, delivery_id: str) -> subprocess.CompletedProcess:
    return _run_orca(["orchestration", "check", "--run", run_id, "--ack", delivery_id, "--json"])


def _terminal_wait_succeeded(proc: subprocess.CompletedProcess) -> bool:
    """`orca terminal wait` is documented to block until the condition is
    met or the timeout expires. We treat exit 0 as "condition reached" and
    any non-zero exit as "did not reach idle in time" -- the conservative
    reading, since we have no confirmed ground truth for how this CLI
    distinguishes the two outcomes in its own JSON body."""
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# `start` subcommand
# ---------------------------------------------------------------------------


def _build_harvest_spec(*, original_task_id: str, original_spec_text: str | None) -> str:
    context_line = f"Original task id (for context only, do not blindly re-execute): {original_task_id}"
    if original_spec_text:
        context_line += f"\nOriginal task spec text (verbatim, for context only):\n{original_spec_text}"
    return (
        "HARVEST ONLY. Do not redo any work. A previous attempt on this exact terminal "
        "already ran and may have already completed the following task; if it has, report "
        "the outcome that was already achieved via worker_done (including any file paths, "
        "results, or conclusions already produced) rather than re-attempting it. If, on "
        "inspection, the previous attempt is clearly still genuinely in progress or was not "
        "actually completed, say so honestly rather than fabricating a result.\n\n" + context_line
    )


def _recover_from_stalled_false_positive(
    *,
    terminal: str,
    original_task_id: str,
    original_dispatch_id: str,
    run_id: str | None,
    original_spec_text: str | None,
    stage_diagnostics: dict[str, Any],
) -> int:
    """Steps 4a-4d of the design, run while the caller's TerminalLock is
    still held."""
    wait_proc = _terminal_wait_tui_idle(terminal, timeout_ms=TUI_IDLE_WAIT_TIMEOUT_MS)
    if not _terminal_wait_succeeded(wait_proc):
        _print_json_stdout(
            {
                "ok": False,
                "recovery": "retry_later",
                "original_dispatch_id": original_dispatch_id,
                "diagnostics": stage_diagnostics,
                "message": (
                    "terminal did not report tui-idle within the wait window; this may be a "
                    "genuine stall or a task still genuinely running past 5 minutes -- not safe "
                    "to remount yet. Retry `orca-dispatch-guard start` again later against the "
                    "same terminal/task."
                ),
            }
        )
        return EXIT_RETRY_LATER

    read_proc = _terminal_read_tail(terminal, limit=400)
    captured_tail = read_proc.stdout if read_proc.returncode == 0 else None

    journal = read_journal(original_dispatch_id)
    now = now_iso()
    if journal is None:
        journal = {
            "original_dispatch_id": original_dispatch_id,
            "terminal_handle": terminal,
            "run_id": run_id,
            "remount_count": 0,
            "remount_dispatch_ids": [],
            "created_at": now,
            "last_attempt_at": now,
            "captured_terminal_tail": captured_tail,
            "status": "recovering",
        }
    else:
        # Never overwrite a tail already captured on a prior remount attempt.
        journal["last_attempt_at"] = now

    if journal.get("remount_count", 0) >= MAX_REMOUNT_COUNT:
        journal["status"] = "gave_up"
        write_journal_atomic(journal)
        _print_json_stdout(
            {
                "ok": False,
                "recovery": "gave_up",
                "original_dispatch_id": original_dispatch_id,
                "remount_count": journal["remount_count"],
                "diagnostics": stage_diagnostics,
                "message": (
                    f"already attempted {journal['remount_count']} remounts for this dispatch; "
                    "refusing to remount again. Needs human attention."
                ),
            }
        )
        return EXIT_GAVE_UP

    # Persist the "recovering" state -- AND count this as a remount attempt
    # -- before attempting the remount itself, so a crash mid-remount still
    # leaves the captured tail on disk, and so a persistently-failing
    # task-create (which never reaches worker-start at all) still counts
    # toward MAX_REMOUNT_COUNT instead of retrying forever uncapped.
    journal["remount_count"] = journal.get("remount_count", 0) + 1
    journal["status"] = "recovering"
    write_journal_atomic(journal)

    spec_text = _build_harvest_spec(original_task_id=original_task_id, original_spec_text=original_spec_text)
    task_create_proc = _task_create(spec=spec_text, run_id=run_id, title="orca-dispatch-guard recovery harvest")
    if task_create_proc.returncode != 0:
        _print_json_stderr(
            {
                "ok": False,
                "reason": "recovery_task_create_failed",
                "original_dispatch_id": original_dispatch_id,
                "remount_count": journal["remount_count"],
                "raw_stdout": task_create_proc.stdout,
                "raw_stderr": task_create_proc.stderr,
            }
        )
        return EXIT_FAILURE

    new_task_id = extract_task_id(_parse_json_loose(task_create_proc.stdout))
    if not new_task_id:
        _print_json_stderr(
            {
                "ok": False,
                "reason": "recovery_task_id_unresolvable",
                "original_dispatch_id": original_dispatch_id,
                "remount_count": journal["remount_count"],
                "raw_stdout": task_create_proc.stdout,
            }
        )
        return EXIT_FAILURE

    remount_proc = _worker_start(task_id=new_task_id, terminal=terminal, run_id=run_id, retry_of=original_dispatch_id)
    new_dispatch_id = extract_dispatch_id(_parse_json_loose(remount_proc.stdout))

    if new_dispatch_id:
        journal.setdefault("remount_dispatch_ids", []).append(new_dispatch_id)
    journal["last_attempt_at"] = now_iso()
    journal["status"] = "recovering"
    write_journal_atomic(journal)

    # Fail closed, symmetric with how the INITIAL dispatch id is handled in
    # `_do_start()`: a remount that exits 0 but whose own dispatch id we
    # cannot extract from its JSON body is not a safe "ok" -- without the
    # id, `wait` can never later match this remount's real worker_done, and
    # `remount_dispatch_ids[]` would silently omit it forever. Report exit
    # code `remount_proc.returncode` only when the CLI itself reported
    # non-zero; an unresolvable id on an otherwise-0 exit is instead
    # surfaced as EXIT_FAILURE, never masqueraded as EXIT_OK.
    if remount_proc.returncode != 0:
        exit_code = remount_proc.returncode
    elif new_dispatch_id is None:
        exit_code = EXIT_FAILURE
    else:
        exit_code = EXIT_OK
    ok = exit_code == EXIT_OK
    _print_json_stdout(
        {
            "ok": ok,
            "recovery": "remount",
            "original_dispatch_id": original_dispatch_id,
            "new_task_id": new_task_id,
            "new_dispatch_id": new_dispatch_id,
            "remount_count": journal["remount_count"],
            "diagnostics": stage_diagnostics,
            "worker_start_result": _parse_json_loose(remount_proc.stdout) or remount_proc.stdout,
        }
    )
    return exit_code


def _do_start(*, terminal: str, task_id: str, run_id: str | None) -> int:
    proc = _worker_start(task_id=task_id, terminal=terminal, run_id=run_id, retry_of=None)
    if proc.returncode == 0:
        sys.stdout.write(proc.stdout)
        return EXIT_OK

    if not is_stalled_false_positive(proc.stdout, proc.stderr, proc.returncode):
        # Genuine failure -- unchanged passthrough, no swallowing, no
        # reinterpretation, same exit code worker-start itself reported.
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode

    parsed = _parse_json_loose(proc.stdout) or _parse_json_loose(proc.stderr)
    diagnostics = extract_stage_diagnostics(parsed)
    original_dispatch_id = extract_dispatch_id(parsed)
    if original_dispatch_id is None:
        _print_json_stderr(
            {
                "ok": False,
                "reason": "stalled_false_positive_but_dispatch_id_unresolvable",
                "message": (
                    "agent_prompt_stalled signature detected but no dispatch id could be "
                    "extracted from worker-start's JSON response; refusing to guess an id "
                    "rather than journal/retry against the wrong dispatch."
                ),
                "diagnostics": diagnostics,
                "raw_stdout": proc.stdout,
                "raw_stderr": proc.stderr,
            }
        )
        return EXIT_FAILURE

    return _recover_from_stalled_false_positive(
        terminal=terminal,
        original_task_id=task_id,
        original_dispatch_id=original_dispatch_id,
        run_id=run_id,
        original_spec_text=None,
        stage_diagnostics=diagnostics,
    )


def cmd_start(args: argparse.Namespace) -> int:
    lock_timeout = (args.timeout_ms / 1000.0) if args.timeout_ms else DEFAULT_LOCK_ACQUIRE_TIMEOUT_SECONDS
    try:
        with TerminalLock(args.terminal, timeout_seconds=lock_timeout):
            return _do_start(terminal=args.terminal, task_id=args.task, run_id=args.run)
    except TerminalLockTimeout as exc:
        _print_json_stderr({"ok": False, "reason": "lock_timeout", "message": str(exc)})
        return EXIT_FAILURE


# ---------------------------------------------------------------------------
# `wait` subcommand
# ---------------------------------------------------------------------------


def _message_list(parsed: dict[str, Any] | None) -> list[Any]:
    """Extraction of the message list from a parsed `orchestration check`
    JSON body. On the confirmed real envelope (live `orchestration check
    --wait --json` output) messages live at `result.messages` -- checked
    first; falls back to a bare top-level list only when `parsed` does not
    even look like the wrapped envelope (see `_is_wrapped_envelope`)."""
    if not isinstance(parsed, dict):
        return []
    if _is_wrapped_envelope(parsed):
        result = parsed.get("result")
        result = result if isinstance(result, dict) else {}
        for key in ("messages", "events", "items"):
            value = result.get(key)
            if isinstance(value, list):
                return value
        return []
    for key in ("messages", "events", "items"):
        value = parsed.get(key)
        if isinstance(value, list):
            return value
    return []


def _extract_dispatch_id_from_worker_done(msg: dict[str, Any]) -> str | None:
    """Structured-field extraction of the dispatch id a single `worker_done`
    message actually reports -- exact field lookup, never a substring scan
    of the whole serialized message. A raw `did in json.dumps(msg)` search
    (the previous approach) false-positives whenever a watched id is a
    PREFIX of another id present anywhere in the same message (e.g. watching
    "dispatch-123" while the message mentions "dispatch-1234" in an
    unrelated field). Checked shapes, in order: `dispatchId`/`dispatch_id`
    directly on the message; the same keys inside a `payload` field, which
    may arrive as a JSON-encoded string (the observed shape -- see
    WaitCommandTests) or already as a nested dict; and a `dispatch` nested
    object, mirroring `extract_dispatch_id()`'s own fallback shapes above."""
    for key in ("dispatchId", "dispatch_id"):
        value = msg.get(key)
        if isinstance(value, str) and value:
            return value
    payload = msg.get("payload")
    payload_obj: dict[str, Any] | None = None
    if isinstance(payload, str):
        payload_obj = _parse_json_loose(payload)
    elif isinstance(payload, dict):
        payload_obj = payload
    if isinstance(payload_obj, dict):
        for key in ("dispatchId", "dispatch_id"):
            value = payload_obj.get(key)
            if isinstance(value, str) and value:
                return value
    nested = msg.get("dispatch")
    if isinstance(nested, dict):
        for key in ("id", "dispatchId", "dispatch_id"):
            value = nested.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _find_matching_worker_done(parsed: dict[str, Any] | None, watched_ids: set[str]) -> dict[str, Any] | None:
    for msg in _message_list(parsed):
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type") or msg.get("message_type")
        if msg_type != "worker_done":
            continue
        found_id = _extract_dispatch_id_from_worker_done(msg)
        if found_id and found_id in watched_ids:
            result = dict(msg)
            result["_matched_dispatch_id"] = found_id
            return result
    return None


def _find_capability_revoked_hit(parsed: dict[str, Any] | None, watched_ids: set[str]) -> str | None:
    """Scan every message (not just worker_done) for the capability-revoked
    signature referencing one of our watched dispatch ids -- if this real,
    previously-documented downstream symptom shows up for a dispatch we are
    watching, report it explicitly instead of just timing out silently.

    The signature itself (`is_capability_revoked_failure`) is still a text
    search -- there is no confirmed field name for "this message reports a
    revoked capability" to key off instead -- though it no longer searches
    caller-echoed text fields, so a message merely quoting a spec that names
    the phrase is not a hit. But WHICH dispatch id it names is
    resolved via the same structured-field extraction as the worker_done
    sibling below (`_extract_dispatch_id_from_worker_done`), not a substring
    scan of the serialized message: this symptom is documented as the
    rejection of the dispatch's own real, eventual `worker_done`, so the
    message carries that same confirmed id shape (`dispatchId`/`dispatch_id`
    directly, or nested under `payload`/`dispatch`). A raw
    `did in msg_text` search (the previous approach) false-positives
    whenever a watched id is a PREFIX of another id present anywhere in the
    same message (e.g. watching "dispatch-123" while the message mentions
    "dispatch-1234" in an unrelated field) -- the exact bug already fixed for
    `_find_matching_worker_done` above. Non-dict messages have no structured
    field to extract from and are skipped rather than text-scanned, per this
    project's fail-closed convention (no guessing an id match)."""
    for msg in _message_list(parsed):
        if not isinstance(msg, dict):
            continue
        msg_text = json.dumps(msg, ensure_ascii=False)
        if not is_capability_revoked_failure(msg_text, ""):
            continue
        found_id = _extract_dispatch_id_from_worker_done(msg)
        if found_id and found_id in watched_ids:
            return found_id
    return None


def _mark_journal_recovered_locked(matched_dispatch_id: str) -> None:
    """Mark the journal entry for `matched_dispatch_id` "recovered",
    serialized against `cmd_start()`'s recovery path via the SAME
    per-terminal TerminalLock -- closes the race where this read-then-write
    could otherwise land between `_recover_from_stalled_false_positive()`'s
    own two writes (before task-create, and after the remount) and get
    silently clobbered back to "recovering". The journal is re-read AFTER
    the lock is held (not reused from any earlier peek), so this always
    acts on the latest on-disk state rather than a stale in-memory copy.

    A quick unlocked peek first finds which terminal handle to lock (the
    journal entry itself records it) without holding the lock for a lookup
    that might turn up nothing to update at all; if that peek finds nothing,
    there is nothing to mark and we return without ever touching the lock."""
    peek = find_journal_by_any_dispatch_id(matched_dispatch_id)
    if peek is None:
        return
    terminal_handle = peek.get("terminal_handle")
    if not isinstance(terminal_handle, str) or not terminal_handle:
        # No terminal handle on record (should not happen for a journal
        # `_recover_from_stalled_false_positive()` itself wrote) -- fall
        # back to an unlocked write rather than silently dropping the
        # "recovered" status entirely.
        journal = find_journal_by_any_dispatch_id(matched_dispatch_id)
        if journal is not None:
            journal["status"] = "recovered"
            write_journal_atomic(journal)
        return
    try:
        with TerminalLock(terminal_handle, timeout_seconds=WAIT_JOURNAL_LOCK_TIMEOUT_SECONDS):
            journal = find_journal_by_any_dispatch_id(matched_dispatch_id)
            if journal is not None:
                journal["status"] = "recovered"
                write_journal_atomic(journal)
    except TerminalLockTimeout as exc:
        _print_json_stderr(
            {
                "ok": True,
                "warning": "journal_recovered_write_lock_timeout",
                "matched_dispatch_id": matched_dispatch_id,
                "message": (
                    "the real worker_done match was found and acked, but marking the journal "
                    f"'recovered' timed out waiting for the per-terminal lock: {exc}"
                ),
            }
        )


def _expand_watched_ids_with_remounts(watched_ids: set[str]) -> set[str]:
    """Fold in any `remount_dispatch_ids` recorded against each watched id's
    journal entry. A caller of `wait` only ever knows the ORIGINAL dispatch
    id it passed to `start` -- but if that original submission hit the
    false-positive stall, `_recover_from_stalled_false_positive()` mints a
    brand-new dispatch id via a remounted worker-start, and the real,
    eventual `worker_done` for the work carries THAT new id, never the
    original. Without this expansion, `_find_matching_worker_done()` would
    never match it and `wait` would time out even though the work genuinely
    completed. Looked up fresh on every poll iteration (not once up front)
    because a concurrent `start` call may still be mid-recovery -- and
    therefore may mint a new remount id -- while this `wait` call is already
    polling. Missing/unreadable journals are treated as "nothing to add",
    never an error: a watched id with no journal at all is the common case
    (its `start` never hit the stall in the first place)."""
    expanded = set(watched_ids)
    for did in list(watched_ids):
        journal = find_journal_by_any_dispatch_id(did)
        if journal is None:
            continue
        original = journal.get("original_dispatch_id")
        if isinstance(original, str) and original:
            expanded.add(original)
        for remount_id in journal.get("remount_dispatch_ids") or []:
            if isinstance(remount_id, str) and remount_id:
                expanded.add(remount_id)
    return expanded


def cmd_wait(args: argparse.Namespace) -> int:
    watched_ids: set[str] = set(args.dispatch or [])
    timeout_ms = args.timeout_ms if args.timeout_ms is not None else DEFAULT_WAIT_TIMEOUT_MS
    deadline = time.monotonic() + (timeout_ms / 1000.0)

    while True:
        for did in list(watched_ids):
            journal = find_journal_by_any_dispatch_id(did)
            if journal is not None and journal.get("status") == "recovered":
                _print_json_stdout(
                    {"ok": True, "matched_dispatch_id": did, "via": "journal_status_recovered"}
                )
                return EXIT_OK

        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            _print_json_stdout({"ok": False, "reason": "timeout", "watched_dispatch_ids": sorted(watched_ids)})
            return EXIT_FAILURE

        chunk_ms = int(min(WAIT_CHECK_CHUNK_MS, max(remaining_seconds * 1000.0, 1.0)))
        proc = _orchestration_check(run_id=args.run, timeout_ms=chunk_ms)
        parsed = _parse_json_loose(proc.stdout)

        # Re-derived every iteration, not cached -- see
        # _expand_watched_ids_with_remounts()'s own docstring for why a
        # concurrent recovery mid-poll must still be picked up.
        active_watched_ids = _expand_watched_ids_with_remounts(watched_ids)

        revoked_hit = _find_capability_revoked_hit(parsed, active_watched_ids)
        if revoked_hit is not None:
            _print_json_stdout(
                {
                    "ok": False,
                    "reason": "capability_revoked_detected",
                    "matched_dispatch_id": revoked_hit,
                    "message": (
                        "a capability-revoked signature was observed for a watched dispatch id -- "
                        "its real worker_done may have been silently rejected by the underlying bug."
                    ),
                }
            )
            return EXIT_FAILURE

        match = _find_matching_worker_done(parsed, active_watched_ids)
        if match is not None:
            matched_id = match.get("_matched_dispatch_id")
            # The ack id is the BATCH delivery id from this `check` call's
            # own envelope (`result.deliveryId`), never a per-message field
            # -- a message's own "id" (e.g. "msg_...") is a different,
            # unrelated identifier and must not be used to ack.
            delivery_id = extract_delivery_id(parsed)
            batch_messages = _message_list(parsed)
            # `orca orchestration check --help` is explicit that --ack
            # consumes the PRIOR WHOLE BATCH ("process every message before
            # acknowledging") -- there is no per-message ack in the real
            # CLI. Finding a match for OUR watched id(s) does not mean this
            # call has processed every OTHER message the batch may also
            # contain (a worker_done for a dispatch we are not watching, a
            # question, an escalation); acking anyway would silently and
            # permanently discard those. So we only ack when our matched
            # message is the batch's ONLY message. Tradeoff, accepted
            # deliberately: when other messages are present we leave the
            # WHOLE batch (including our own already-found match) unacked,
            # so a later `check` call -- from this process or another
            # watcher -- will see it again; this may cost extra
            # throughput/latency but never loses a sibling message.
            ack_info: dict[str, Any]
            if len(batch_messages) == 1:
                if isinstance(delivery_id, str) and delivery_id:
                    ack_proc = _orchestration_ack(run_id=args.run, delivery_id=delivery_id)
                    ack_info = {"attempted": True, "ok": ack_proc.returncode == 0}
                    if ack_proc.returncode != 0:
                        ack_info["raw_stdout"] = ack_proc.stdout
                        ack_info["raw_stderr"] = ack_proc.stderr
                else:
                    ack_info = {"attempted": False, "reason": "no_delivery_id_extracted"}
            else:
                ack_info = {
                    "attempted": False,
                    "reason": "batch_contains_other_messages",
                    "batch_message_count": len(batch_messages),
                }
            if matched_id:
                _mark_journal_recovered_locked(matched_id)
            ack_failed = ack_info.get("attempted") and not ack_info.get("ok", True)
            result_payload = {
                "ok": not ack_failed,
                "matched_dispatch_id": matched_id,
                "message": match,
                "ack": ack_info,
            }
            if ack_failed:
                result_payload["warning"] = "ack_failed"
            _print_json_stdout(result_payload)
            return EXIT_OK if not ack_failed else EXIT_FAILURE
        # No match this chunk -- loop again until the overall deadline.


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orca-dispatch-guard")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="Submit a dispatch via worker-start, recovering from the busy-terminal false-positive stall.")
    p_start.add_argument("--task", required=True)
    p_start.add_argument("--terminal", required=True)
    p_start.add_argument("--run", default=None)
    p_start.add_argument(
        "--timeout-ms",
        type=int,
        default=None,
        help="Bounds this command's own lock-acquire wait (not passed through to worker-start, which has no such flag).",
    )

    p_wait = sub.add_parser("wait", help="Block until a worker_done references one of the watched dispatch ids.")
    p_wait.add_argument("--run", required=True)
    p_wait.add_argument("--timeout-ms", type=int, default=None)
    p_wait.add_argument("--dispatch", action="append", default=[])

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "start":
        return cmd_start(args)
    if args.command == "wait":
        return cmd_wait(args)
    parser.error(f"unknown command {args.command!r}")
    return EXIT_USAGE  # pragma: no cover - parser.error() already exits


if __name__ == "__main__":
    sys.exit(main())
