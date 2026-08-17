#!/usr/bin/env python3
"""Replay the managed install lifecycle inside an owned Extreme SSD temp root."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))

import install_prime_agent as installer

SANDBOX_PARENT = Path(__file__).resolve().parents[2] / ".orca"


def require(condition: object, message: str) -> None:
    """Non-optimizable stand-in for a bare `assert` on the lifecycle-acceptance
    path. A bare `assert` is compiled out entirely under `python -O` /
    `PYTHONOPTIMIZE=1` (`__debug__` becomes False and the whole statement,
    condition included, is never evaluated) -- so a real lifecycle failure
    (a mocked/broken install, verify, enable, uninstall, or recover result)
    would silently report a pass instead of failing loudly under those
    common environment settings (independent Codex sol/xhigh review,
    2026-08-16, P1-4). `if not condition: raise AssertionError(message)` has
    no such optimized-mode exemption -- it always evaluates and always
    raises. See main() for the accompanying refusal to run at all under
    `__debug__ is False`, which is belt-and-suspenders alongside this."""
    if not condition:
        raise AssertionError(message)


def paths(root: Path) -> dict[str, Path]:
    local_homes = root / "local-homes"
    tool_root = local_homes / ".shared-tools/prime-agent"
    release = tool_root / "releases" / f"v{installer.VERSION}"
    user_home = root / "user"
    return {
        "ssd": root,
        "local_homes": local_homes,
        "tool_root": tool_root,
        "release": release,
        "state": tool_root / "state",
        "probe": tool_root / "probe-home",
        "sessions": tool_root / "sessions",
        "user_home": user_home,
        "state_link": user_home / ".prime",
        "bin_link": user_home / ".local/bin/prime-agent",
        "receipt": tool_root / "receipts" / f"v{installer.VERSION}.json",
        "pending": tool_root / "pending-install.json",
    }


def run(expected_lock_sha256: str | None) -> int:
    real_targets = (
        installer.STATE_LINK,
        installer.BIN_LINK,
        installer.TOOL_ROOT,
        installer.RELEASE_DIR,
        installer.STATE_DIR,
        installer.PROBE_HOME,
        installer.managed_session_dir(),
        installer.RECEIPT_PATH,
        installer.PENDING_PATH,
        installer.lifecycle_lock_path(),
    )
    present_before = [
        os.fspath(path) for path in real_targets if os.path.lexists(path)
    ]
    if present_before:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "real managed paths are present; sandbox refuses an ambiguous baseline",
                    "present_real_paths": present_before,
                },
                sort_keys=True,
            )
        )
        return 3
    result_payload: dict[str, object] | None = None
    exit_code = 1
    primary_error: BaseException | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="prime-agent-e2e-", dir=SANDBOX_PARENT
        ) as directory:
            root = Path(directory).resolve()
            p = paths(root)
            p["local_homes"].mkdir(mode=0o700)
            p["user_home"].mkdir(mode=0o700)
            compile_cache_observer = root / "compile-cache-observer"
            compile_cache_observer.mkdir(mode=0o700)
            path_env = os.pathsep.join(
                (os.fspath(p["bin_link"].parent), os.environ.get("PATH", ""))
            )
            patches = (
                mock.patch.object(installer, "SSD_ROOT", p["ssd"]),
                mock.patch.object(installer, "LOCAL_HOMES", p["local_homes"]),
                mock.patch.object(installer, "TOOL_ROOT", p["tool_root"]),
                mock.patch.object(installer, "RELEASE_DIR", p["release"]),
                mock.patch.object(installer, "STATE_DIR", p["state"]),
                mock.patch.object(installer, "PROBE_HOME", p["probe"]),
                mock.patch.object(installer, "USER_HOME", p["user_home"]),
                mock.patch.object(installer, "STATE_LINK", p["state_link"]),
                mock.patch.object(installer, "BIN_LINK", p["bin_link"]),
                mock.patch.object(installer, "RECEIPT_PATH", p["receipt"]),
                mock.patch.object(installer, "PENDING_PATH", p["pending"]),
                mock.patch.object(
                    installer, "volume_uuid", return_value="TEST-VOLUME-UUID"
                ),
                mock.patch.dict(os.environ, {"PATH": path_env}),
            )
            if expected_lock_sha256:
                patches += (
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    ),
                )
            with ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                try:
                    receipt = installer.install()
                except installer.PrimeInstallError as exc:
                    lock_path = p["release"] / "package-lock.json"
                    observed = None
                    if lock_path.is_file():
                        parsed_lock = installer.strict_json(lock_path.read_bytes())
                        if isinstance(parsed_lock, dict):
                            observed = installer.sha256_bytes(
                                installer.normalized_production_lock(parsed_lock)
                            )
                    result_payload = {
                        "ok": False,
                        "error": str(exc),
                        "observed_production_lock_sha256": observed,
                    }
                    exit_code = 2
                else:
                    require(
                        receipt["command_default_enabled"] is False,
                        "receipt command_default_enabled was not False",
                    )
                    require(
                        receipt["session_dir"] == os.fspath(p["sessions"]),
                        "receipt session_dir did not match the sandboxed session directory",
                    )
                    require(p["sessions"].is_dir(), "sandboxed session directory is missing")
                    license_path = p["release"] / "LICENSE"
                    require(
                        license_path.is_file() and not license_path.is_symlink(),
                        "installed LICENSE is missing or is a symlink",
                    )
                    require(
                        installer.sha256_file(license_path) == installer.LICENSE_SHA256,
                        "installed LICENSE digest did not match the pinned digest",
                    )
                    require(
                        receipt["license_sha256"] == installer.LICENSE_SHA256,
                        "receipt license_sha256 did not match the pinned digest",
                    )
                    require(
                        not p["bin_link"].exists() and not p["bin_link"].is_symlink(),
                        "command link exists immediately after install, before enable",
                    )
                    wrapper_version = subprocess.run(
                        [os.fspath(Path(receipt["bin_target"])), "--version"],
                        cwd=root,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                        env={
                            "HOME": os.fspath(p["user_home"]),
                            "PATH": path_env,
                            "TMPDIR": os.fspath(compile_cache_observer),
                        },
                    )
                    wrapper_channels = (
                        wrapper_version.stdout.strip(), wrapper_version.stderr.strip()
                    )
                    require(
                        wrapper_version.returncode == 0,
                        f"wrapper --version exited {wrapper_version.returncode}",
                    )
                    require(
                        wrapper_channels
                        in ((installer.VERSION, ""), ("", installer.VERSION)),
                        f"wrapper --version output did not match the pinned version: {wrapper_channels!r}",
                    )
                    require(
                        list(compile_cache_observer.iterdir()) == [],
                        "Node compile cache wrote to the observed default temp directory",
                    )
                    disabled = installer.verify()
                    require(
                        disabled["command_enabled"] is False,
                        "verify() reported command_enabled True before enable",
                    )
                    require(
                        disabled["version_probe"] == installer.VERSION,
                        "verify() version_probe did not match the pinned version",
                    )
                    require(
                        disabled["sessions_on_ssd"] is True,
                        "verify() reported sessions_on_ssd False",
                    )
                    require(
                        disabled["session_dir"] == os.fspath(p["sessions"]),
                        "verify() session_dir did not match the sandboxed session directory",
                    )

                    update = subprocess.run(
                        [os.fspath(Path(receipt["bin_target"])), "update"],
                        cwd=root,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                        env={"HOME": os.fspath(p["user_home"]), "PATH": path_env},
                    )
                    require(
                        update.returncode == 64,
                        f"wrapper update exited {update.returncode}, expected 64",
                    )
                    require(
                        "self-update is disabled" in update.stderr,
                        "wrapper update did not report self-update as disabled",
                    )

                    enabled = installer.enable()
                    require(
                        enabled["command_enabled"] is True,
                        "enable() did not report command_enabled True",
                    )
                    require(
                        installer.verify()["command_enabled"] is True,
                        "verify() did not report command_enabled True after enable",
                    )
                    stopped = installer.uninstall()
                    require(
                        stopped["command_disabled"] is True,
                        "uninstall() did not report command_disabled True",
                    )
                    require(
                        installer.verify()["command_enabled"] is False,
                        "verify() reported command_enabled True after uninstall",
                    )
                    recovered = installer.recover()
                    require(
                        recovered["state"] == "already_committed",
                        f"recover() state was {recovered.get('state')!r}, expected already_committed",
                    )
                    require(
                        recovered["command_enabled"] is False,
                        "recover() reported command_enabled True after uninstall",
                    )
                    result_payload = {
                        "ok": True,
                        "version": installer.VERSION,
                        "production_lock_sha256": receipt[
                            "production_lock_sha256"
                        ],
                        "license_sha256": receipt["license_sha256"],
                        "release_tree_sha256": receipt["release_tree_sha256"],
                        "release_tree_entries": receipt["release_tree_entries"],
                        "command_default_enabled": False,
                        "enable_verify_disable_recover": "passed",
                        "node_compile_cache_disabled": True,
                        "sessions_on_ssd": True,
                        "real_user_state_changed": False,
                    }
                    exit_code = 0
    except BaseException as exc:
        primary_error = exc
    finally:
        present_after = [
            os.fspath(path) for path in real_targets if os.path.lexists(path)
        ]
        if present_after != present_before:
            contamination = AssertionError(
                "sandbox changed real managed paths: "
                f"before={present_before!r} after={present_after!r}"
            )
            if primary_error is not None:
                raise contamination from primary_error
            raise contamination
    if primary_error is not None:
        raise primary_error
    require(result_payload is not None, "no result payload was produced")
    print(json.dumps(result_payload, sort_keys=True))
    return exit_code


def main() -> int:
    if not __debug__:
        # Belt-and-suspenders alongside require(): every lifecycle-acceptance
        # check in run() already uses require() instead of a bare `assert`
        # (see that function's own P1-4 comment), so this guard is not
        # strictly needed for correctness anymore -- but refusing outright
        # under `python -O` / `PYTHONOPTIMIZE=1` means a future bare `assert`
        # added anywhere in this module (accidentally, or in a smaller
        # utility script that copies this pattern) fails loud and immediate
        # instead of silently compiling out and reporting a false pass
        # (independent Codex sol/xhigh review, 2026-08-16, P1-4).
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        "refusing to run sandbox_e2e.py with __debug__=False "
                        "(python -O / PYTHONOPTIMIZE=1 strips assert statements "
                        "and can silently turn a real lifecycle failure into a "
                        "reported pass); re-run without -O and without "
                        "PYTHONOPTIMIZE set"
                    ),
                },
                sort_keys=True,
            )
        )
        return 4
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-lock-sha256")
    args = parser.parse_args()
    return run(args.expected_lock_sha256)


if __name__ == "__main__":
    raise SystemExit(main())
