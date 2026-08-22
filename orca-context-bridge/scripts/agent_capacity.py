#!/usr/bin/env python3
"""Report live local-capacity evidence for a multi-agent wave.

The result is advisory and never starts, stops, or signals an Orca agent.

Gate enforcement removed 2026-08-16 per explicit, repeated, live user
instruction ("请把红灯机制去除" -> "强制拆除" -> "请将门禁彻底去除") that
this specific script never block or cap dispatch, superseding this same
session's earlier opt-in-only override flag (which the user judged
insufficient). `capacity_recommendation()` below still computes the true
red/yellow/green signal from the same thresholds as before -- that
computation, and the underlying load/memory/Orca-agent evidence it is based
on, are unchanged and still printed in full for transparency -- but `main()`
no longer lets that signal block, cap, or otherwise gate any caller by
default. See `--i-am-explicitly-overriding-the-capacity-gate-this-run-only`
in `build_parser()` for the (now-superseded, kept only for backward
compatibility with existing call sites) prior opt-in mechanism.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


COMMAND_TIMEOUT_SECONDS = 5.0
SIGNATURE_TIMEOUT_SECONDS = 15.0
CLEANUP_TIMEOUT_SECONDS = 0.25
DEFAULT_OUTPUT_LIMIT = 64 * 1024
WORKTREE_OUTPUT_LIMIT = 1024 * 1024
# 2026-08-22 reconciliation merge: raised 128 -> 256. At 128 this machine's
# real worktree count (143, verified live) exceeded the cap and
# summarize_orca_worktrees() silently discarded all Orca evidence
# (worktree_error "invalid_worktree_schema"), degrading a true green gate to
# an advisory yellow. 256 restores headroom above today's count.
MAX_WORKTREE_ROWS = 256
MAX_AGENTS_PER_WORKTREE = 64
MAX_TOTAL_WORKING_AGENTS = 3
MEMORY_FREE_RE = __import__("re").compile(
    r"memory free percentage:\s*(\d+(?:\.\d+)?)%", __import__("re").IGNORECASE
)

SYSCTL = Path("/usr/sbin/sysctl")
MEMORY_PRESSURE = Path("/usr/bin/memory_pressure")
CODESIGN = Path("/usr/bin/codesign")
# 2026-08-22 reconciliation merge, round 2: the round-1 candidate reused the
# ORCA_CLI_COMMAND env var here to override this *.app bundle path, copying
# the naming convention from orca_readonly_probe.py /
# orca_lifecycle_precondition_probe.py -- but in those two scripts
# ORCA_CLI_COMMAND names a PATH-resolved *command* ("orca"), not a bundle
# directory, so the same variable name meant two incompatible things
# depending which script read it. Two independent dual reviews (Claude
# opus + Codex sol, 2026-08-22) both flagged this as a real defect: setting
# ORCA_CLI_COMMAND=orca (correct usage for the other two scripts) made this
# module fail closed with orca_authority_unavailable, and the override also
# widened this trust anchor's input surface with no demonstrated need (the
# GNOME-screen-reader rationale for the other two scripts is a PATH-lookup
# concern that does not apply to a fixed bundle path here). Reverted to A's
# original hardcoded path; no env override for the bundle location.
ORCA_BUNDLE = Path("/Applications/Orca.app")
ORCA_ELECTRON = ORCA_BUNDLE / "Contents" / "MacOS" / "Orca"
ORCA_CLI = ORCA_BUNDLE / "Contents" / "Resources" / "app.asar.unpacked" / "out" / "cli" / "index.js"
ORCA_IDENTIFIER = "com.stablyai.orca"
ORCA_TEAM_ID = "6CX3WHS9HZ"
ORCA_REQUIREMENT = (
    f'=identifier "{ORCA_IDENTIFIER}" and anchor apple generic '
    'and certificate 1[field.1.2.840.113635.100.6.2.6] exists '
    'and certificate leaf[field.1.2.840.113635.100.6.1.13] exists '
    f'and certificate leaf[subject.OU] = "{ORCA_TEAM_ID}"'
)
SUBPROCESS_ENV = {
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    # The packaged CLI is a JavaScript entrypoint executed by Orca's signed
    # Electron binary.  Without this switch Electron starts the GUI, hits the
    # single-instance guard, and exits 3 instead of running the CLI command.
    "ELECTRON_RUN_AS_NODE": "1",
}
WORKTREE_STATES = frozenset({"active", "inactive", "working"})
AGENT_STATES = frozenset({"working", "waiting", "done", "idle", "error", "interrupted"})


@dataclass(frozen=True)
class CommandResult:
    payload: dict[str, Any] | None
    error: str | None = None


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "FileIdentity":
        return cls(value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid)


@dataclass(frozen=True)
class OrcaAuthority:
    electron: Path
    cli: Path
    snapshot: tuple[FileIdentity, FileIdentity, FileIdentity]


@dataclass(frozen=True)
class RawCommandResult:
    stdout: bytes | None
    stderr: bytes | None
    error: str | None


def _strict_json_loads(value: str) -> Any:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = item
        return result

    def no_nonfinite(token: str) -> None:
        raise ValueError(f"nonfinite:{token}")

    return json.loads(value, object_pairs_hook=no_duplicates, parse_constant=no_nonfinite)


def _trusted_component(identity: FileIdentity, relative: tuple[str, ...]) -> bool:
    mode = stat.S_IMODE(identity.mode)
    if identity.uid not in {0, os.geteuid()} or mode & stat.S_IWOTH:
        return False
    # macOS exposes /Applications as root:admin 0775.  The following signed
    # bundle verification supplies the integrity proof for this one anchor.
    if relative == ("Applications",):
        return identity.uid == 0
    return not bool(mode & stat.S_IWGRP)


def _open_trusted_path(path: Path, *, directory_leaf: bool = False) -> FileIdentity | None:
    try:
        parts = path.relative_to(Path("/")).parts
    except ValueError:
        return None
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    result: FileIdentity | None = None
    failed = False
    primary_control: BaseException | None = None
    try:
        descriptor = os.open("/", directory_flags)
        root = FileIdentity.from_stat(os.fstat(descriptor))
        if root.uid != 0 or stat.S_IMODE(root.mode) & (stat.S_IWGRP | stat.S_IWOTH):
            failed = True
        else:
            for index, component in enumerate(parts):
                leaf = index == len(parts) - 1
                child: int | None = None
                try:
                    named = FileIdentity.from_stat(os.stat(component, dir_fd=descriptor, follow_symlinks=False))
                    child = os.open(component, file_flags if leaf else directory_flags, dir_fd=descriptor)
                    opened = FileIdentity.from_stat(os.fstat(child))
                except OSError:
                    if child is not None:
                        try:
                            os.close(child)
                        except OSError:
                            pass
                    failed = True
                    break
                valid_type = stat.S_ISDIR(opened.mode) if (not leaf or directory_leaf) else stat.S_ISREG(opened.mode)
                if named != opened or not valid_type or not _trusted_component(opened, parts[: index + 1]):
                    try:
                        os.close(child)
                    except OSError:
                        pass
                    failed = True
                    break
                parent = descriptor
                descriptor = child
                os.close(parent)
            if not failed:
                result = FileIdentity.from_stat(os.fstat(descriptor))
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            primary_control = exc
        failed = True
    close_failed = False
    if descriptor is not None:
        try:
            os.close(descriptor)
        except OSError:
            close_failed = True
    if primary_control is not None:
        raise primary_control
    return None if failed or close_failed else result


def _group_state(process: subprocess.Popen[bytes]) -> bool | None:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    return True


def _stop_group(process: subprocess.Popen[bytes]) -> tuple[bool, BaseException | None]:
    control_error: BaseException | None = None

    def remember(exc: BaseException) -> None:
        nonlocal control_error
        if control_error is None and isinstance(exc, (KeyboardInterrupt, SystemExit)):
            control_error = exc

    def state() -> bool | None:
        try:
            return _group_state(process)
        except BaseException as exc:
            remember(exc)
            return None

    def direct(method: str) -> None:
        try:
            getattr(process, method)()
        except (OSError, ProcessLookupError):
            pass
        except BaseException as exc:
            remember(exc)

    def signal_group(value: int, fallback: str) -> bool:
        try:
            os.killpg(process.pid, value)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            direct(fallback)
            return True
        except BaseException as exc:
            remember(exc)
            direct(fallback)
            return True

    def wait_for_exit() -> bool:
        try:
            deadline = time.monotonic() + CLEANUP_TIMEOUT_SECONDS
        except BaseException as exc:
            remember(exc)
            return state() is False
        while True:
            if state() is False:
                return True
            try:
                remaining = deadline - time.monotonic()
            except BaseException as exc:
                remember(exc)
                return False
            if remaining <= 0:
                return False
            try:
                time.sleep(min(0.01, remaining))
            except BaseException as exc:
                remember(exc)
                return False

    if not signal_group(signal.SIGTERM, "terminate"):
        return True, control_error
    if wait_for_exit():
        return True, control_error
    if not signal_group(signal.SIGKILL, "kill"):
        return True, control_error
    if wait_for_exit():
        return True, control_error
    return state() is False, control_error


def _run_bounded(argv: Sequence[str], *, timeout: float, output_limit: int) -> RawCommandResult:
    if not argv or not os.path.isabs(argv[0]) or timeout <= 0 or output_limit <= 0:
        return RawCommandResult(None, None, "unsafe_command")
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    streams: list[tuple[Any, str]] = []
    output = {"stdout": bytearray(), "stderr": bytearray()}
    primary_error: str | None = None
    primary_control: BaseException | None = None
    cleanup_error: str | None = None
    cleanup_control: BaseException | None = None
    cleanup_incomplete = False

    def remember_cleanup(exc: BaseException) -> None:
        nonlocal cleanup_error, cleanup_control
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            cleanup_control = cleanup_control or exc
        else:
            cleanup_error = cleanup_error or type(exc).__name__

    try:
        process = subprocess.Popen(
            list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=False, start_new_session=True, env=SUBPROCESS_ENV,
        )
        if process.stdout is None or process.stderr is None:
            primary_error = "pipe_unavailable"
        else:
            streams = [(process.stdout, "stdout"), (process.stderr, "stderr")]
            selector = selectors.DefaultSelector()
            for stream, name in streams:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            deadline = time.monotonic() + timeout
            while selector.get_map() and primary_error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    primary_error = "timeout"
                    break
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(output[key.data]) + len(chunk) > output_limit:
                        primary_error = "output_limit"
                        break
                    else:
                        output[key.data].extend(chunk)
            if primary_error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    primary_error = "timeout"
                else:
                    try:
                        returncode = process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        primary_error = "timeout"
                    else:
                        if _group_state(process) is not False:
                            primary_error = "live_process_group"
                        elif returncode != 0:
                            primary_error = f"exit_{returncode}"
    except (KeyboardInterrupt, SystemExit) as exc:
        primary_control = exc
    except BaseException as exc:
        primary_error = primary_error or type(exc).__name__
    finally:
        if selector is not None:
            try:
                selector.close()
            except BaseException as exc:
                remember_cleanup(exc)
        for stream, _ in streams:
            try:
                stream.close()
            except BaseException as exc:
                remember_cleanup(exc)
        if process is not None:
            try:
                state = _group_state(process)
            except BaseException as exc:
                remember_cleanup(exc)
                state = None
            if primary_error is not None or primary_control is not None or state is not False:
                terminated, control_error = _stop_group(process)
                if control_error is not None:
                    remember_cleanup(control_error)
                if not terminated:
                    cleanup_incomplete = True
            try:
                process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
            except BaseException as exc:
                remember_cleanup(exc)
    if primary_control is not None:
        raise primary_control
    if cleanup_control is not None:
        raise cleanup_control
    if cleanup_incomplete:
        return RawCommandResult(None, None, "cleanup_incomplete")
    if primary_error is not None:
        return RawCommandResult(None, None, primary_error)
    if cleanup_error is not None:
        return RawCommandResult(None, None, f"cleanup_{cleanup_error}")
    return RawCommandResult(bytes(output["stdout"]), bytes(output["stderr"]), None)


def run_json_command(argv: list[str], timeout: float = COMMAND_TIMEOUT_SECONDS, *, output_limit: int = DEFAULT_OUTPUT_LIMIT) -> CommandResult:
    result = _run_bounded(argv, timeout=timeout, output_limit=output_limit)
    if result.error is not None or result.stdout is None:
        return CommandResult(payload=None, error=result.error or "command_failed")
    try:
        value = _strict_json_loads(result.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError, TypeError):
        return CommandResult(payload=None, error="invalid_json")
    if not isinstance(value, dict) or value.get("ok") is not True or not isinstance(value.get("result"), dict):
        return CommandResult(payload=None, error="invalid_envelope")
    return CommandResult(payload=value, error=None)


def _trusted_system_executable(path: Path) -> FileIdentity | None:
    return _open_trusted_path(path)


def _orca_snapshot() -> tuple[FileIdentity, FileIdentity, FileIdentity] | None:
    bundle = _open_trusted_path(ORCA_BUNDLE, directory_leaf=True)
    electron = _open_trusted_path(ORCA_ELECTRON)
    cli = _open_trusted_path(ORCA_CLI)
    return (bundle, electron, cli) if bundle and electron and cli else None


def _verified_orca_authority() -> tuple[OrcaAuthority | None, str | None]:
    before = _orca_snapshot()
    codesign_identity = _trusted_system_executable(CODESIGN)
    if before is None or codesign_identity is None:
        return None, "orca_authority_unavailable"
    verified = _run_bounded(
        [os.fspath(CODESIGN), "--verify", "--deep", "-R", ORCA_REQUIREMENT, os.fspath(ORCA_BUNDLE)],
        timeout=SIGNATURE_TIMEOUT_SECONDS, output_limit=DEFAULT_OUTPUT_LIMIT,
    )
    details = _run_bounded(
        [os.fspath(CODESIGN), "-dv", "--verbose=4", os.fspath(ORCA_BUNDLE)],
        timeout=SIGNATURE_TIMEOUT_SECONDS, output_limit=DEFAULT_OUTPUT_LIMIT,
    )
    metadata = ((details.stdout or b"") + (details.stderr or b"")).decode("utf-8", errors="replace")
    if verified.error is not None or details.error is not None:
        return None, "orca_signature_invalid"
    lines = set(metadata.splitlines())
    if f"Identifier={ORCA_IDENTIFIER}" not in lines or f"TeamIdentifier={ORCA_TEAM_ID}" not in lines:
        return None, "orca_signature_identity"
    after = _orca_snapshot()
    if after != before or _trusted_system_executable(CODESIGN) != codesign_identity:
        return None, "orca_authority_changed"
    return OrcaAuthority(ORCA_ELECTRON, ORCA_CLI, after), None


def _finite(value: object, *, minimum: float, maximum: float | None = None) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        return False
    return float(value) >= minimum and (maximum is None or float(value) <= maximum)


def logical_cpus() -> int | None:
    if sys.platform == "darwin":
        before = _trusted_system_executable(SYSCTL)
        if before is None:
            return None
        result = _run_bounded([os.fspath(SYSCTL), "-n", "hw.logicalcpu"], timeout=COMMAND_TIMEOUT_SECONDS, output_limit=DEFAULT_OUTPUT_LIMIT)
        if _trusted_system_executable(SYSCTL) != before or result.stdout is None or result.error is not None:
            return None
        try:
            value = int(result.stdout.decode("ascii", errors="strict").strip())
        except ValueError:
            return None
        return value if value > 0 else None
    try:
        affinity = os.sched_getaffinity(0)
        return len(affinity) if affinity else None
    except (AttributeError, OSError):
        return None


def one_minute_load() -> float | None:
    try:
        value = float(os.getloadavg()[0])
    except (AttributeError, OSError, ValueError):
        return None
    return round(value, 3) if _finite(value, minimum=0) else None


def memory_free_percent() -> float | None:
    if sys.platform != "darwin":
        return None
    before = _trusted_system_executable(MEMORY_PRESSURE)
    if before is None:
        return None
    result = _run_bounded([os.fspath(MEMORY_PRESSURE), "-Q"], timeout=COMMAND_TIMEOUT_SECONDS, output_limit=DEFAULT_OUTPUT_LIMIT)
    if _trusted_system_executable(MEMORY_PRESSURE) != before or result.stdout is None or result.error is not None:
        return None
    match = MEMORY_FREE_RE.search(result.stdout.decode("utf-8", errors="replace"))
    value = float(match.group(1)) if match else None
    return value if value is not None and _finite(value, minimum=0, maximum=100) else None


def summarize_orca_worktrees(payload: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    rows, total, truncated = result.get("worktrees"), result.get("totalCount"), result.get("truncated")
    if not isinstance(rows, list) or not isinstance(total, int) or isinstance(total, bool) or not isinstance(truncated, bool):
        return None
    if truncated or total != len(rows) or total < 0 or total > MAX_WORKTREE_ROWS:
        return None
    working_worktrees = 0
    active_agents = 0
    for row in rows:
        if not isinstance(row, dict) or row.get("status") not in WORKTREE_STATES:
            return None
        agents = row.get("agents")
        if not isinstance(agents, list) or len(agents) > MAX_AGENTS_PER_WORKTREE:
            return None
        working_worktrees += int(row["status"] == "working")
        for agent in agents:
            if not isinstance(agent, dict) or agent.get("state") not in AGENT_STATES:
                return None
            active_agents += int(agent["state"] == "working")
    return {"worktrees": total, "working_worktrees": working_worktrees, "reported_agents": active_agents}


def capacity_recommendation(
    cpu_count: int | None,
    load_1m: float | None,
    memory_free_pct: float | None,
    *,
    include_orca: bool,
    orca_available: bool,
    active_agents: int | None,
) -> dict[str, Any]:
    valid_cpu = isinstance(cpu_count, int) and not isinstance(cpu_count, bool) and cpu_count > 0
    valid_load = _finite(load_1m, minimum=0)
    valid_memory = _finite(memory_free_pct, minimum=0, maximum=100)
    valid_agents = isinstance(active_agents, int) and not isinstance(active_agents, bool) and active_agents >= 0
    if not (valid_cpu and valid_load and valid_memory):
        return _limited("host capacity evidence was incomplete or invalid")
    if not include_orca or not orca_available or not valid_agents:
        return _limited("Orca activity evidence was unavailable; host-only mode cannot authorize a green wave")
    ratio = round(float(load_1m) / cpu_count, 3)
    reasons: list[str] = []
    if ratio > 1:
        reasons.append("load exceeds logical CPU capacity")
    if float(memory_free_pct) < 15:
        reasons.append("free memory is below 15%")
    if active_agents >= MAX_TOTAL_WORKING_AGENTS:
        reasons.append("Orca already reports the total working-agent capacity")
    if reasons:
        return {
            "gate": "red", "new_workers_default": 0, "new_workers_max": 0, "coordinator_only": True,
            "reason": reasons,
            "next_action": "Do not start a new heavy agent; wait for an existing worker to settle or be released.",
        }
    if ratio >= 0.75 or float(memory_free_pct) < 25:
        return _limited("host load or free memory is within the caution range", active_agents=active_agents)
    remaining = MAX_TOTAL_WORKING_AGENTS - active_agents
    if remaining < 2:
        return _limited("existing Orca working agents leave room for at most one new worker", active_agents=active_agents)
    return {
        "gate": "green", "new_workers_default": 2, "new_workers_max": min(3, remaining), "coordinator_only": False,
        "reason": ["host load, memory, and Orca agent evidence are within the local planning threshold"],
        "next_action": "Start two independent workers by default; use a third only when write sets are isolated.",
    }


def _limited(reason: str, *, active_agents: int | None = None) -> dict[str, Any]:
    remaining = 1 if active_agents is None else max(0, MAX_TOTAL_WORKING_AGENTS - active_agents)
    return {
        "gate": "yellow", "new_workers_default": min(1, remaining), "new_workers_max": min(1, remaining),
        "coordinator_only": False, "reason": [reason],
        "next_action": "Start at most one isolated worker and wait for its result before opening another wave.",
    }


def _orca_snapshot_data() -> dict[str, Any]:
    authority, authority_error = _verified_orca_authority()
    if authority is None:
        return {"diagnostics_available": False, "diagnostics_error": authority_error, "worktree_summary": None, "worktree_error": authority_error}
    prefix = [os.fspath(authority.electron), os.fspath(authority.cli)]
    diagnostics = run_json_command([*prefix, "diagnostics", "memory", "--json"])
    worktrees = run_json_command([*prefix, "worktree", "ps", "--limit", str(MAX_WORKTREE_ROWS), "--json"], output_limit=WORKTREE_OUTPUT_LIMIT)
    if _orca_snapshot() != authority.snapshot:
        return {"diagnostics_available": False, "diagnostics_error": "orca_authority_changed", "worktree_summary": None, "worktree_error": "orca_authority_changed"}
    summary = summarize_orca_worktrees(worktrees.payload)
    return {
        "diagnostics_available": diagnostics.payload is not None,
        "diagnostics_error": diagnostics.error,
        "worktree_summary": summary,
        "worktree_error": worktrees.error or (None if summary is not None else "invalid_worktree_schema"),
    }


def collect_snapshot(include_orca: bool) -> dict[str, Any]:
    orca = _orca_snapshot_data() if include_orca else {
        "diagnostics_available": False, "diagnostics_error": "skipped_by_no_orca", "worktree_summary": None, "worktree_error": "skipped_by_no_orca",
    }
    # Host evidence is intentionally sampled after potentially slow Orca RPCs.
    cpu_count, load_1m, free_memory = logical_cpus(), one_minute_load(), memory_free_percent()
    summary = orca["worktree_summary"]
    snapshot: dict[str, Any] = {
        "observed_at": datetime.now(timezone.utc).isoformat(), "max_age_seconds": 15,
        "cpu_logical": cpu_count, "load_1m": load_1m,
        "load_per_cpu": round(load_1m / cpu_count, 3) if isinstance(cpu_count, int) and cpu_count > 0 and load_1m is not None else None,
        "memory_free_percent": free_memory, "orca": orca,
    }
    snapshot["recommendation"] = capacity_recommendation(
        cpu_count, load_1m, free_memory, include_orca=include_orca,
        orca_available=orca["diagnostics_available"] and summary is not None,
        active_agents=summary["reported_agents"] if summary is not None else None,
    )
    return snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recommend a safe local multi-agent wave size.")
    parser.add_argument("--no-orca", action="store_true", help="Use explicit host-only degraded mode (never green).")
    # Superseded 2026-08-16: gate enforcement is now unconditionally off by default
    # (see module docstring), so this flag no longer changes anything -- the
    # unflagged default already reports the same non-blocking recommendation this
    # flag used to have to be typed to get. Kept accepted (as a no-op) only so any
    # existing call site that already passes it does not start failing on an
    # unrecognized-argument error.
    parser.add_argument(
        "--i-am-explicitly-overriding-the-capacity-gate-this-run-only",
        action="store_true",
        help=(
            "No-op as of 2026-08-16: gate enforcement was removed from the default "
            "path itself, so every invocation already gets what this flag used to "
            "grant. Kept only for backward compatibility with existing call sites."
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    snapshot = collect_snapshot(include_orca=not args.no_orca)
    true_recommendation = snapshot["recommendation"]
    snapshot["advisory_true_recommendation"] = true_recommendation
    snapshot["recommendation"] = {
        "gate": "gate_removed",
        "new_workers_default": 2,
        "new_workers_max": 3,
        "coordinator_only": False,
        "reason": [
            "Gate enforcement removed per explicit, repeated, live user instruction "
            "(2026-08-16). This field no longer blocks or caps dispatch for any "
            "caller. See advisory_true_recommendation for what the load/memory/"
            f"Orca-agent-count based gate would have said (was {true_recommendation['gate']!r}: "
            f"{'; '.join(true_recommendation['reason'])})."
        ],
        "next_action": (
            "No capacity-based restriction. advisory_true_recommendation above is "
            "informational only."
        ),
    }
    print(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
