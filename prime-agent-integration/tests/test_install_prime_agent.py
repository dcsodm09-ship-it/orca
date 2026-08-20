from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))

import install_prime_agent as installer

# Round 38, 2026-08-20: the SHA-256 of the generic stub upstream lock every
# `fake_safe_download()` closure across the full-`install()` regression
# tests below writes for "upstream-package-lock.json"
# (`{"lockfileVersion": 3, "packages": {}}`) -- every one of those closures
# is byte-for-byte identical in what it writes for this file, so this is
# computed once, here, rather than 19 separate times. Round 38 added a
# digest re-check in `_install_locked_within_release_dir()` (mirroring the
# existing round-13/14 pattern already used for the Node.js toolchain
# tarball and the four locally patched packages' own source tarballs)
# comparing the freshly re-read on-disk bytes against the real
# `installer.LOCK_SHA256` module constant -- which a synthetic test lock
# can never match. Every one of those 19 tests patches `installer.
# LOCK_SHA256` to this value, alongside its existing `safe_download` patch,
# so that new re-check compares against the SAME stub content the test's
# own `fake_safe_download` actually wrote, exactly the way
# `GENERATED_LOCK_SHA256` is already independently patched to match each
# test's own synthetic generated lock.
STUB_UPSTREAM_LOCK_SHA256 = installer.sha256_bytes(
    installer.canonical_json({"lockfileVersion": 3, "packages": {}})
)


class PrimeAgentInstallerTests(unittest.TestCase):
    def fake_extract_node_toolchain(
        self, asset: Path, destination: Path, expected_sha256: str
    ) -> tuple[Path, Path, str, str, dict[Path, str]]:
        """Shared `extract_node_toolchain` fake for the full-`_install_locked()`
        integration tests below (round 24, 2026-08-19): unlike the real
        function, this never touches the network or a real Node.js tarball,
        but it MUST still leave real, private, regular files on disk at
        `destination` and return their real content digests -- round 24
        made _install_locked() re-verify node/npm_cli's content digest
        (verify_unchanged_private_ssd_asset_digest()) immediately before
        every subsequent point either is used, so a mock that only returns
        bare, non-existent Paths (this fixture's pre-round-24 shape) now
        fails those checks before ever reaching whatever each individual
        test actually means to exercise.

        Round 34, 2026-08-20 (P1-2): also writes a THIRD toolchain file,
        lib/node_modules/npm/lib/cli.js -- modeling real npm's own layout,
        where lib/node_modules/npm/bin/npm-cli.js is a two-line forwarding
        stub and lib/node_modules/npm/lib/cli.js is the actual library code
        that runs -- and returns a `toolchain_content_digests` map (the
        function's now 5th return value) covering all three files, exactly
        as the real extract_node_toolchain() returns a digest for every
        regular file it extracts, not merely node/npm-cli.js. Every caller
        that unpacks this fake's return value already expects a 5-tuple to
        match the real function's current signature.
        """
        node_path = destination / "bin/node"
        npm_cli_path = destination / "lib/node_modules/npm/bin/npm-cli.js"
        npm_lib_cli_path = destination / "lib/node_modules/npm/lib/cli.js"
        node_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        node_path.write_bytes(b"#!/bin/sh\necho fake-node\n")
        os.chmod(node_path, 0o700)
        npm_cli_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        npm_cli_path.write_bytes(b"#!/usr/bin/env node\nrequire('../lib/cli.js')\n")
        os.chmod(npm_cli_path, 0o600)
        npm_lib_cli_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        npm_lib_cli_path.write_bytes(b"// fake npm lib/cli.js (the real npm library code)\n")
        os.chmod(npm_lib_cli_path, 0o600)
        toolchain_content_digests = {
            Path("bin/node"): installer.sha256_file(node_path),
            Path("lib/node_modules/npm/bin/npm-cli.js"): installer.sha256_file(
                npm_cli_path
            ),
            Path("lib/node_modules/npm/lib/cli.js"): installer.sha256_file(
                npm_lib_cli_path
            ),
        }
        return (
            node_path,
            npm_cli_path,
            toolchain_content_digests[Path("bin/node")],
            toolchain_content_digests[Path("lib/node_modules/npm/bin/npm-cli.js")],
            toolchain_content_digests,
        )

    def create_managed_home(
        self, path: Path, *, session_dir: Path | None = None
    ) -> Path:
        path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700)
        agent = path / "agent"
        agent.mkdir(mode=0o700)
        settings_raw = installer.canonical_json(
            {
                "sessionDir": os.fspath(
                    session_dir
                    if session_dir is not None
                    else installer.managed_session_dir()
                ),
                "telemetry": {"enabled": False, "noticeShown": True},
            }
        )
        installer.atomic_write(
            agent / "settings.json", settings_raw, 0o600
        )
        return agent / "settings.json"

    def validate_test_lock(
        self, generated: dict[str, object], root: Path
    ) -> dict[str, object]:
        release = root / "release"
        assets = release / "assets"
        assets.mkdir(parents=True, mode=0o700)
        local_assets = {
            "prime-agent": installer.MAIN_PATCHED_ASSET,
            **installer.WORKSPACE_ASSETS,
        }
        packages = generated.setdefault("packages", {})
        assert isinstance(packages, dict)
        packages.setdefault(
            "",
            {
                "name": "orca-managed-prime-agent",
                "version": installer.VERSION,
                "dependencies": {
                    "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                },
            },
        )
        patched_asset_sha256: dict[str, str] = {}
        for index, (name, asset_name) in enumerate(local_assets.items()):
            content = name.encode("utf-8")
            asset_path = assets / asset_name
            asset_path.write_bytes(content)
            os.chmod(asset_path, 0o600)
            patched_asset_sha256[asset_name] = installer.sha256_bytes(content)
            packages.setdefault(
                f"node_modules/local-{index}",
                {
                    "name": name,
                    "version": installer.VERSION,
                    "resolved": f"file:assets/{asset_name}",
                    "integrity": "sha512-dGVzdA==",
                },
            )
        generated.setdefault("lockfileVersion", 3)
        raw = installer.canonical_json(generated)
        count = len([path for path in packages if path])
        with (
            mock.patch.object(installer, "SSD_ROOT", root),
            mock.patch.object(installer, "RELEASE_DIR", release),
            mock.patch.object(installer, "GENERATED_LOCK_PACKAGE_COUNT", count),
        ):
            expected = installer.sha256_bytes(installer.normalized_production_lock(generated))
            with mock.patch.object(installer, "GENERATED_LOCK_SHA256", expected):
                return installer.validate_generated_lock(raw, generated, patched_asset_sha256)

    def test_package_name_from_nested_lock_path(self) -> None:
        self.assertEqual(
            installer.package_name_from_lock_path("node_modules/a/node_modules/@scope/pkg", {}),
            "@scope/pkg",
        )
        self.assertEqual(
            installer.package_name_from_lock_path("node_modules/a/node_modules/plain", {}),
            "plain",
        )

    def test_exact_dependency_versions_uses_top_level_release_choice(self) -> None:
        manifest = {"dependencies": {"chalk": "^5", "@earendil-works/pi-ai": "remote"}}
        lock = {
            "packages": {
                "node_modules/chalk": {"version": "5.6.2"},
                "node_modules/a/node_modules/chalk": {"version": "4.1.2"},
                "packages/ai": {"name": "@earendil-works/pi-ai", "version": installer.VERSION},
            }
        }
        installer.exact_dependency_versions(manifest, lock)
        self.assertEqual(manifest["dependencies"]["chalk"], "5.6.2")
        self.assertTrue(manifest["dependencies"]["@earendil-works/pi-ai"].startswith("file:"))

    def test_generated_lock_hash_is_exact(self) -> None:
        generated = {"lockfileVersion": 3, "packages": {}}
        raw = installer.canonical_json(generated)
        with mock.patch.object(installer, "GENERATED_LOCK_SHA256", "0" * 64):
            with self.assertRaisesRegex(installer.PrimeInstallError, "lock hash mismatch"):
                installer.validate_generated_lock(raw, generated, {})

    def test_receipt_identity_pins_upstream_license(self) -> None:
        identity = installer.expected_receipt_identity()
        self.assertEqual(identity["license_sha256"], installer.LICENSE_SHA256)
        self.assertIn(installer.TAG_COMMIT, installer.LICENSE_URL)
        self.assertRegex(installer.LICENSE_SHA256, r"^[0-9a-f]{64}$")

    def test_generated_lock_rejects_unpinned_https(self) -> None:
        generated = {
            "packages": {
                "node_modules/chalk": {
                    "version": "5.6.2",
                    "resolved": "https://example.invalid/chalk.tgz",
                    "integrity": "sha512-dGVzdA==",
                },
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(installer.PrimeInstallError, "unsafe generated"):
                self.validate_test_lock(generated, Path(directory))

    def test_generated_lock_rejects_remote_workspace_asset(self) -> None:
        generated: dict[str, object] = {"packages": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "release"
            assets = release / "assets"
            assets.mkdir(parents=True)
            asset_content = b"asset"
            patched_asset_sha256: dict[str, str] = {}
            for asset_name in (installer.MAIN_PATCHED_ASSET, *installer.WORKSPACE_ASSETS.values()):
                asset_path = assets / asset_name
                asset_path.write_bytes(asset_content)
                os.chmod(asset_path, 0o600)
                patched_asset_sha256[asset_name] = installer.sha256_bytes(asset_content)
            packages = {
                "": {
                    "name": "orca-managed-prime-agent",
                    "version": installer.VERSION,
                    "dependencies": {
                        "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                    },
                },
                "node_modules/prime-agent": {
                    "name": "prime-agent",
                    "version": installer.VERSION,
                    "resolved": f"file:assets/{installer.MAIN_PATCHED_ASSET}",
                    "integrity": "sha512-dGVzdA==",
                },
            }
            for index, (name, asset_name) in enumerate(installer.WORKSPACE_ASSETS.items()):
                packages[f"node_modules/workspace-{index}"] = {
                    "name": name,
                    "version": installer.VERSION,
                    "resolved": (
                        "https://pub.example.invalid/" + asset_name
                        if index == 0
                        else f"file:assets/{asset_name}"
                    ),
                    "integrity": "sha512-dGVzdA==",
                }
            generated = {"lockfileVersion": 3, "packages": packages}
            raw = installer.canonical_json(generated)
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "GENERATED_LOCK_PACKAGE_COUNT", len(packages) - 1),
            ):
                expected = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
                with mock.patch.object(installer, "GENERATED_LOCK_SHA256", expected):
                    with self.assertRaisesRegex(installer.PrimeInstallError, "local file"):
                        installer.validate_generated_lock(
                            raw, generated, patched_asset_sha256
                        )

    def test_validate_generated_lock_local_asset_content_drift_is_detected(self) -> None:
        # Regression for independent review round 16, 2026-08-18, P1: the
        # reviewer demonstrated that two DIFFERENT patched-tarball contents
        # produce a BYTE-IDENTICAL normalized lock and closure hash, because
        # normalized_production_lock() replaces "integrity" with a fixed
        # placeholder for local assets before hashing, and (pre-fix)
        # validate_generated_lock()'s local-asset branch never looked at
        # "integrity" -- or at the asset's real content -- at all. A
        # same-UID actor swapping a patched asset's on-disk content
        # therefore got it silently pinned into the lock past this check.
        #
        # This test freezes every OTHER input -- the generated lock JSON,
        # its raw bytes, and the pinned closure hash -- and varies ONLY the
        # real on-disk content of one local asset, proving the two contents
        # no longer both pass validate_generated_lock() even though the
        # closure hash itself (deliberately left unchanged, see
        # normalized_production_lock()'s own docstring) cannot tell them
        # apart on its own.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "release"
            assets = release / "assets"
            assets.mkdir(parents=True, mode=0o700)
            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            legitimate_content = b"legitimate patched tarball bytes"
            packages: dict[str, object] = {
                "": {
                    "name": "orca-managed-prime-agent",
                    "version": installer.VERSION,
                    "dependencies": {
                        "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                    },
                },
            }
            patched_asset_sha256: dict[str, str] = {}
            for index, (name, asset_name) in enumerate(local_assets.items()):
                asset_path = assets / asset_name
                asset_path.write_bytes(legitimate_content)
                os.chmod(asset_path, 0o600)
                patched_asset_sha256[asset_name] = installer.sha256_bytes(legitimate_content)
                packages[f"node_modules/local-{index}"] = {
                    "name": name,
                    "version": installer.VERSION,
                    "resolved": f"file:assets/{asset_name}",
                    "integrity": "sha512-dGVzdA==",
                }
            generated = {"lockfileVersion": 3, "packages": packages}
            raw = installer.canonical_json(generated)
            count = len([path for path in packages if path])
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "GENERATED_LOCK_PACKAGE_COUNT", count),
            ):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
                with mock.patch.object(
                    installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                ):
                    # Sanity: with the legitimate content on disk and a
                    # matching digest map, the check passes.
                    result = installer.validate_generated_lock(
                        raw, generated, patched_asset_sha256
                    )
                    self.assertEqual(result["lock_sha256"], expected_lock_sha256)

                    # Swap ONE local asset's on-disk content for something
                    # different, in place (same inode) so an identity-only
                    # check would miss it. The `generated` dict, `raw`
                    # bytes, and GENERATED_LOCK_SHA256 pin above are all
                    # completely untouched.
                    swapped_content = b"attacker-controlled substituted bytes"
                    self.assertNotEqual(swapped_content, legitimate_content)
                    main_asset_path = assets / installer.MAIN_PATCHED_ASSET
                    with open(main_asset_path, "r+b") as handle:
                        handle.seek(0)
                        handle.write(swapped_content)
                        handle.truncate()

                    # The closure hash itself is provably unaffected:
                    # recomputing it from the SAME `generated` dict (which
                    # never encoded the swapped asset's real content to
                    # begin with) gives the SAME pinned value either way --
                    # this is the literal blind spot the reviewer found.
                    self.assertEqual(
                        installer.sha256_bytes(
                            installer.normalized_production_lock(generated)
                        ),
                        expected_lock_sha256,
                    )
                    # But validate_generated_lock() AS A WHOLE must now
                    # refuse, because its own independent content-digest
                    # check catches what the closure hash cannot.
                    with self.assertRaisesRegex(
                        installer.PrimeInstallError, "content drift"
                    ):
                        installer.validate_generated_lock(
                            raw, generated, patched_asset_sha256
                        )

    def test_safe_extract_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            archive_path = root / "bad.tgz"
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("package/../../escape")
                payload = b"bad"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            os.chmod(archive_path, 0o600)
            destination = root / "out"
            destination.mkdir()
            with self.assertRaises(installer.PrimeInstallError):
                installer.safe_extract_main_asset(
                    archive_path, destination, installer.sha256_file(archive_path)
                )
            self.assertFalse((root / "escape").exists())

    def test_tree_digest_changes_with_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "file.txt"
            path.write_text("one", encoding="utf-8")
            with mock.patch.object(installer, "SSD_ROOT", root):
                first, first_count = installer.tree_digest(root)
                path.write_text("two", encoding="utf-8")
                second, second_count = installer.tree_digest(root)
            self.assertNotEqual(first, second)
            self.assertEqual(first_count, second_count)

    def test_tree_digest_rejects_escaped_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            outside = root.parent / f"{root.name}-outside"
            outside.write_text("outside", encoding="utf-8")
            try:
                (root / "escape").symlink_to(outside)
                with mock.patch.object(installer, "SSD_ROOT", root):
                    with self.assertRaisesRegex(installer.PrimeInstallError, "escaped release"):
                        installer.tree_digest(root)
            finally:
                outside.unlink()

    def test_tree_digest_rejects_unsafe_root_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(installer, "SSD_ROOT", root):
                installer.tree_digest(root)
                os.chmod(root, 0o777)
                try:
                    with self.assertRaisesRegex(installer.PrimeInstallError, "private directory"):
                        installer.tree_digest(root)
                finally:
                    os.chmod(root, 0o700)

    def test_managed_entrypoint_is_safe_by_default(self) -> None:
        script = installer.managed_entrypoint_script(
            Path("/managed/node"), Path("/managed/node_modules/prime-agent/dist/bundle/cli.js")
        ).decode("utf-8")
        self.assertIn("self-update is disabled", script)
        self.assertIn("ORCA_PRIME_AGENT_RESOURCE_GUARD=1", script)
        self.assertIn("--session-dir is fixed", script)
        self.assertIn("export PRIME_AGENT_SESSION_DIR=", script)
        self.assertIn("export PRIME_AGENT_CODING_AGENT_SESSION_DIR=", script)
        self.assertIn("ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS", script)
        self.assertIn("--cwd|--cwd=*", script)
        self.assertIn("--resume|--resume=*|-r|-r?*", script)
        self.assertIn("export PRIME_AGENT_LAUNCHER_PATH=", script)
        self.assertIn("export NODE_DISABLE_COMPILE_CACHE=1", script)
        self.assertIn("export DO_NOT_TRACK=1", script)
        self.assertIn("export PRIME_AGENT_TELEMETRY=0", script)
        self.assertIn("export PYTHONDONTWRITEBYTECODE=1", script)
        self.assertIn("exec /usr/bin/python3 -B", script)
        self.assertIn("prime-agent-launch-guard.py", script)
        guard = installer.managed_launch_guard_script(
            Path("/managed/node"),
            Path("/managed/node_modules/prime-agent/dist/bundle/cli.js"),
            "0" * 64,
            "0" * 64,
        ).decode("utf-8")
        self.assertIn("fcntl.LOCK_SH", guard)
        self.assertIn(
            'RESOURCE_GUARDS = ("--no-extensions", "--no-skills", "--no-prompt-templates")',
            guard,
        )
        self.assertIn("def guarded_arguments", guard)
        self.assertIn("[NODE, CLI, *guarded_arguments", guard)
        self.assertIn(f"NODE_SHA256 = {'0' * 64!r}", guard)
        self.assertIn(f"CLI_SHA256 = {'0' * 64!r}", guard)
        self.assertIn("validate_exec_target(NODE, root, NODE_SHA256)", guard)
        self.assertIn("validate_exec_target(CLI, root, CLI_SHA256)", guard)
        self.assertIn("import hashlib", guard)

    def test_version_probe_accepts_one_exact_stderr_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            probe = root / "probe"
            self.create_managed_home(probe)
            entrypoint = (
                root / "release/lib/node_modules/prime-agent/dist/bundle/cli.js"
            )
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("// test", encoding="utf-8")
            receipt = {
                "bin_target": os.fspath(root / "bin/prime-agent"),
                "node_target": os.fspath(root / "toolchain/bin/node"),
                "probe_home": os.fspath(probe),
                "release_dir": os.fspath(root / "release"),
            }
            completed = subprocess.CompletedProcess(
                ["prime-agent", "--version"], 0, "", installer.VERSION + "\n"
            )
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(
                    installer.subprocess, "run", return_value=completed
                ) as run,
            ):
                self.assertEqual(installer.run_version_probe(receipt), installer.VERSION)
            self.assertEqual(
                run.call_args.kwargs["env"]["NODE_DISABLE_COMPILE_CACHE"], "1"
            )

    def test_version_probe_rejects_extra_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            probe = root / "probe"
            self.create_managed_home(probe)
            entrypoint = (
                root / "release/lib/node_modules/prime-agent/dist/bundle/cli.js"
            )
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("// test", encoding="utf-8")
            receipt = {
                "bin_target": os.fspath(root / "bin/prime-agent"),
                "node_target": os.fspath(root / "toolchain/bin/node"),
                "probe_home": os.fspath(probe),
                "release_dir": os.fspath(root / "release"),
            }
            completed = subprocess.CompletedProcess(
                ["prime-agent", "--version"], 0, installer.VERSION + "\n", "warning\n"
            )
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer.subprocess, "run", return_value=completed),
            ):
                with self.assertRaisesRegex(installer.PrimeInstallError, "exactly match"):
                    installer.run_version_probe(receipt)

    def test_runtime_state_requires_telemetry_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            state = tool_root / "state"
            session_dir = tool_root / "sessions"
            session_dir.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root, 0o700)
            settings = self.create_managed_home(state, session_dir=session_dir)
            installer.atomic_write(
                settings,
                installer.canonical_json(
                    {
                        "sessionDir": os.fspath(session_dir),
                        "telemetry": {"enabled": False},
                        "runtime": {"allowed": True},
                    }
                ),
                0o600,
            )
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
            ):
                self.assertEqual(installer.validate_runtime_state(state), state)
                installer.atomic_write(
                    settings,
                    installer.canonical_json(
                        {
                            "sessionDir": os.fspath(session_dir),
                            "telemetry": {"enabled": True},
                        }
                    ),
                    0o600,
                )
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "telemetry is not disabled"
                ):
                    installer.validate_runtime_state(state)
                installer.atomic_write(
                    settings,
                    installer.canonical_json(
                        {
                            "sessionDir": "/tmp/escaped-prime-sessions",
                            "telemetry": {"enabled": False},
                        }
                    ),
                    0o600,
                )
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "session directory drifted"
                ):
                    installer.validate_runtime_state(state)

    def test_run_npm_uses_private_home_and_minimal_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cache = root / "cache"
            install_home = root / "home"
            install_tmp = root / "tmp"
            for path in (cache, install_home, install_tmp):
                path.mkdir(mode=0o700)
            completed = subprocess.CompletedProcess(["npm"], 0, "", "")
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(
                    installer.subprocess, "run", return_value=completed
                ) as run,
            ):
                installer.run_npm(
                    "/safe/npm",
                    "/safe/node",
                    ["ci"],
                    root,
                    cache,
                    install_home,
                    install_tmp,
                )
            environment = run.call_args.kwargs["env"]
            self.assertEqual(run.call_args.args[0][:3], ["/safe/node", "/safe/npm", "ci"])
            self.assertEqual(environment["HOME"], os.fspath(install_home))
            self.assertEqual(environment["npm_config_registry"], "https://registry.npmjs.org/")
            self.assertNotIn("SSH_AUTH_SOCK", environment)
            self.assertNotIn("NODE_AUTH_TOKEN", environment)
            self.assertNotEqual(
                environment["npm_config_userconfig"], environment["npm_config_globalconfig"]
            )

    def test_run_npm_rejects_managed_config_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cache = root / "cache"
            install_home = root / "home"
            install_tmp = root / "tmp"
            for path in (cache, install_home, install_tmp):
                path.mkdir(mode=0o700)
            npmrc = install_home / "npmrc"
            npmrc.write_text("//registry.npmjs.org/:_authToken=unexpected\n", encoding="utf-8")
            os.chmod(npmrc, 0o600)
            with mock.patch.object(installer, "SSD_ROOT", root):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "configuration drifted"
                ):
                    installer.run_npm(
                        "/safe/npm",
                        "/safe/node",
                        ["ci"],
                        root,
                        cache,
                        install_home,
                        install_tmp,
                    )

    def test_exact_tool_version_uses_private_npm_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cache = root / "cache"
            install_home = root / "home"
            install_tmp = root / "tmp"
            for path in (cache, install_home, install_tmp):
                path.mkdir(mode=0o700)
            completed = subprocess.CompletedProcess(
                ["/safe/node", "--version"], 0, "v" + installer.NODE_VERSION + "\n", ""
            )
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(
                    installer.subprocess, "run", return_value=completed
                ) as run,
            ):
                observed = installer.exact_tool_version(
                    ["/safe/node", "--version"],
                    installer.NODE_VERSION,
                    "Node.js",
                    cache=cache,
                    install_home=install_home,
                    install_tmp=install_tmp,
                )
            self.assertEqual(observed, installer.NODE_VERSION)
            self.assertEqual(run.call_args.kwargs["cwd"], install_home)
            environment = run.call_args.kwargs["env"]
            self.assertEqual(environment["HOME"], os.fspath(install_home))
            self.assertEqual(environment["TMPDIR"], os.fspath(install_tmp))
            self.assertEqual(environment["npm_config_cache"], os.fspath(cache))
            self.assertEqual(
                environment["npm_config_userconfig"],
                os.fspath(install_home / "npmrc"),
            )
            self.assertEqual(
                environment["npm_config_globalconfig"],
                os.fspath(install_home / "global-npmrc"),
            )
            self.assertEqual(environment["npm_config_update_notifier"], "false")
            self.assertEqual(environment["NODE_DISABLE_COMPILE_CACHE"], "1")

    def test_exact_tool_version_rejects_project_npmrc_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cache = root / "cache"
            install_home = root / "home"
            install_tmp = root / "tmp"
            for path in (cache, install_home, install_tmp):
                path.mkdir(mode=0o700)
            (install_home / ".npmrc").write_text(
                "//registry.npmjs.org/:_authToken=sentinel\n", encoding="utf-8"
            )
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer.subprocess, "run") as run,
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "project configuration"
                ):
                    installer.exact_tool_version(
                        ["/safe/node", "--version"],
                        installer.NODE_VERSION,
                        "Node.js",
                        cache=cache,
                        install_home=install_home,
                        install_tmp=install_tmp,
                    )
            run.assert_not_called()

    def test_command_candidates_include_orca_fallback_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            user_home = Path(directory) / "user"
            fallback = user_home / ".volta/bin"
            fallback.mkdir(parents=True)
            command = fallback / "prime-agent"
            command.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(command, 0o700)
            with (
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
            ):
                self.assertIn(command.absolute(), installer.prime_agent_command_candidates())

    def test_command_search_rejects_cwd_dependent_path_components(self) -> None:
        for path_value in (":/usr/bin", ".:/usr/bin", "relative:/usr/bin", "/usr/bin:"):
            with self.subTest(path_value=path_value):
                with mock.patch.dict(os.environ, {"PATH": path_value}):
                    with self.assertRaisesRegex(
                        installer.PrimeInstallError, "cwd-dependent"
                    ):
                        installer.prime_agent_search_directories()

    def test_wrapper_blocks_cwd_override_without_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "prime-agent"
            wrapper.write_bytes(
                installer.managed_entrypoint_script(Path("/bin/sh"), root / "unused-cli")
            )
            os.chmod(wrapper, 0o700)
            target = root / "target"
            (target / ".prime/agent").mkdir(parents=True)
            (target / ".prime/agent/settings.json").write_text("{}", encoding="utf-8")
            result = subprocess.run(
                [os.fspath(wrapper), "--cwd", os.fspath(target)],
                cwd=root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                env={"HOME": os.fspath(root), "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("--cwd requires explicit", result.stderr)

    def test_wrapper_blocks_all_resume_forms_without_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "prime-agent"
            wrapper.write_bytes(
                installer.managed_entrypoint_script(
                    Path("/managed/node"), root / "unused-cli", root / "unused-guard"
                )
            )
            os.chmod(wrapper, 0o700)
            forms = (
                ("--resume", "session"),
                ("--resume=session",),
                ("-r", "session"),
                ("-rsession",),
                ("session", "--resume", "saved"),
            )
            for arguments in forms:
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        [os.fspath(wrapper), *arguments],
                        cwd=root,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        check=False,
                        env={"HOME": os.fspath(root), "PATH": "/usr/bin:/bin"},
                    )
                    self.assertEqual(result.returncode, 78)
                    self.assertIn("resume requires explicit", result.stderr)

    def test_wrapper_guards_runtime_commands_and_effective_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "release"
            bin_dir = release / "bin"
            bin_dir.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root, 0o700)
            os.chmod(release, 0o700)
            node = bin_dir / "node"
            cli = release / "cli.js"
            node.write_text(
                "#!/usr/bin/python3\n"
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "  'argv': sys.argv[1:],\n"
                "  'session': os.environ.get('PRIME_AGENT_SESSION_DIR'),\n"
                "  'legacySession': os.environ.get('PRIME_AGENT_CODING_AGENT_SESSION_DIR'),\n"
                "  'resourceGuard': os.environ.get('ORCA_PRIME_AGENT_RESOURCE_GUARD'),\n"
                "}))\n",
                encoding="utf-8",
            )
            os.chmod(node, 0o700)
            cli.write_text("// argument sentinel\n", encoding="utf-8")
            os.chmod(cli, 0o600)
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            guard = bin_dir / "prime-agent-launch-guard.py"
            wrapper = bin_dir / "prime-agent"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
            ):
                guard.write_bytes(
                    installer.managed_launch_guard_script(
                        node,
                        cli,
                        installer.sha256_file(node),
                        installer.sha256_file(cli),
                    )
                )
                wrapper.write_bytes(
                    installer.managed_entrypoint_script(node, cli, guard)
                )
            os.chmod(guard, 0o700)
            os.chmod(wrapper, 0o700)
            clean = root / "clean"
            project = root / "project"
            clean.mkdir(mode=0o700)
            (project / ".prime/agent").mkdir(parents=True, mode=0o700)
            (project / ".prime/agent/settings.json").write_text(
                "{}", encoding="utf-8"
            )

            def run_wrapper(
                arguments: tuple[str, ...],
                *,
                cwd: Path,
                allow_settings: bool = False,
            ) -> subprocess.CompletedProcess[str]:
                environment = {
                    "HOME": os.fspath(root),
                    "PATH": "/usr/bin:/bin",
                    "ORCA_PRIME_AGENT_RESOURCE_GUARD": "1",
                    "PRIME_AGENT_SESSION_DIR": "/tmp/escaped-primary",
                    "PRIME_AGENT_CODING_AGENT_SESSION_DIR": "/tmp/escaped-legacy",
                }
                if allow_settings:
                    environment["ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS"] = "1"
                return subprocess.run(
                    [os.fspath(wrapper), *arguments],
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                )

            def observed_payload(
                result: subprocess.CompletedProcess[str],
            ) -> dict[str, object]:
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = installer.strict_json(result.stdout.encode("utf-8"))
                self.assertIsInstance(payload, dict)
                assert isinstance(payload, dict)
                self.assertEqual(payload["session"], os.fspath(tool_root / "sessions"))
                self.assertEqual(
                    payload["legacySession"], os.fspath(tool_root / "sessions")
                )
                self.assertIsNone(payload["resourceGuard"])
                return payload

            for arguments in (("agents",), ("attach", "saved-session")):
                with self.subTest(arguments=arguments, mode="default-block"):
                    result = run_wrapper(arguments, cwd=clean)
                    self.assertEqual(result.returncode, 78)
                    self.assertIn("effective-project settings review", result.stderr)

            project_model = run_wrapper(("model", "list"), cwd=project)
            self.assertEqual(project_model.returncode, 78)
            self.assertIn("project .prime/agent/settings.json", project_model.stderr)

            guarded_model = run_wrapper(("model", "list"), cwd=clean)
            model_payload = observed_payload(guarded_model)
            model_argv = model_payload["argv"]
            self.assertIsInstance(model_argv, list)
            assert isinstance(model_argv, list)
            self.assertEqual(
                model_argv[1:],
                [
                    "model",
                    "list",
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                ],
            )

            ordinary_payload = observed_payload(
                run_wrapper(("ordinary prompt",), cwd=clean)
            )
            ordinary_argv = ordinary_payload["argv"]
            self.assertIsInstance(ordinary_argv, list)
            assert isinstance(ordinary_argv, list)
            self.assertEqual(
                ordinary_argv[1:],
                [
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                    "ordinary prompt",
                ],
            )

            daemon_socket = os.fspath(root / "daemon.sock")
            daemon_stop_arguments = (
                "--daemon-socket",
                daemon_socket,
                "stop",
                "agent-id",
            )
            daemon_stop_payload = observed_payload(
                run_wrapper(daemon_stop_arguments, cwd=project)
            )
            self.assertEqual(
                daemon_stop_payload["argv"][1:], list(daemon_stop_arguments)
            )

            daemon_runtime_payload = observed_payload(
                run_wrapper(
                    ("--daemon-socket", daemon_socket, "ordinary prompt"),
                    cwd=clean,
                )
            )
            self.assertEqual(
                daemon_runtime_payload["argv"][1:],
                [
                    "--daemon-socket",
                    daemon_socket,
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                    "ordinary prompt",
                ],
            )

            for arguments in (("agents",), ("attach", "saved-session")):
                with self.subTest(arguments=arguments, mode="explicit-opt-in"):
                    result = run_wrapper(
                        arguments, cwd=clean, allow_settings=True
                    )
                    payload = observed_payload(result)
                    observed = payload["argv"]
                    self.assertIsInstance(observed, list)
                    assert isinstance(observed, list)
                    self.assertEqual(
                        observed[1 : 1 + len(arguments)],
                        list(arguments),
                    )
                    self.assertEqual(
                        observed[1 + len(arguments) :],
                        ["--no-extensions", "--no-skills", "--no-prompt-templates"],
                    )

            for arguments in (
                ("--session-dir", "/tmp/escaped-cli"),
                ("--session-dir=/tmp/escaped-cli",),
            ):
                with self.subTest(arguments=arguments, mode="session-dir-block"):
                    blocked_session_dir = run_wrapper(arguments, cwd=clean)
                    self.assertEqual(blocked_session_dir.returncode, 78)
                    self.assertIn(
                        "fixed to managed Extreme SSD", blocked_session_dir.stderr
                    )

    def test_guard_placement_survives_trailing_value_hungry_flag(self) -> None:
        # Regression for independent review round 13/14, 2026-08-18, P1-A:
        # guarded_arguments()'s previous append-near-the-end placement
        # appended RESOURCE_GUARDS after the user's own full argv (or
        # before a trailing "--"). Real upstream parseArgs() (dist/cli/
        # args.js, verified directly against the pinned v0.7.2 bundle)
        # unconditionally consumes the token immediately after 17 different
        # value-taking options as that option's OWN value, with no check on
        # what the next token looks like -- so whenever the user's own last
        # real token was one of those options with a missing/empty value
        # (the realistic trigger: a shell expanding an unset variable, e.g.
        # `model list --model $UNSET_VAR`), the first appended guard flag
        # was silently consumed as that option's value instead of ever being
        # scanned as its own flag, defeating --no-extensions entirely and
        # loading a hostile project's extensions with zero opt-in (60
        # confirmed reproductions in the round-13 finding, against a real
        # generated wrapper + real generated launch guard + real pinned
        # Node + the real prime-agent-0.7.2.tgz bundle). This test proves
        # the FIX -- guards now land immediately before the first
        # flag-looking token instead of at the very end -- via the same
        # real generated wrapper + real generated launch guard subprocess
        # fixture every other guard-placement test in this file uses,
        # observing the actual argv the launch guard would hand to NODE/CLI.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "release"
            bin_dir = release / "bin"
            bin_dir.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root, 0o700)
            os.chmod(release, 0o700)
            node = bin_dir / "node"
            cli = release / "cli.js"
            node.write_text(
                "#!/usr/bin/python3\n"
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "  'argv': sys.argv[1:],\n"
                "  'resourceGuard': os.environ.get('ORCA_PRIME_AGENT_RESOURCE_GUARD'),\n"
                "}))\n",
                encoding="utf-8",
            )
            os.chmod(node, 0o700)
            cli.write_text("// argument sentinel\n", encoding="utf-8")
            os.chmod(cli, 0o600)
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            guard = bin_dir / "prime-agent-launch-guard.py"
            wrapper = bin_dir / "prime-agent"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
            ):
                guard.write_bytes(
                    installer.managed_launch_guard_script(
                        node,
                        cli,
                        installer.sha256_file(node),
                        installer.sha256_file(cli),
                    )
                )
                wrapper.write_bytes(
                    installer.managed_entrypoint_script(node, cli, guard)
                )
            os.chmod(guard, 0o700)
            os.chmod(wrapper, 0o700)
            # Deliberately NO .prime/agent/settings.json -- proving
            # RESOURCE_GUARDS, not the settings gate, is what protects this.
            clean = root / "clean-no-settings-json"
            clean.mkdir(mode=0o700)

            def run_wrapper(
                arguments: tuple[str, ...],
            ) -> subprocess.CompletedProcess[str]:
                environment = {
                    "HOME": os.fspath(root),
                    "PATH": "/usr/bin:/bin",
                    # "agents"/"attach" carry a separate effective-project
                    # settings-review gate (independent of RESOURCE_GUARDS
                    # placement, which is what this test exercises); opt in
                    # so those cases reach the launch guard at all. Harmless
                    # for every other command in `cases` below.
                    "ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS": "1",
                }
                return subprocess.run(
                    [os.fspath(wrapper), *arguments],
                    cwd=clean,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                )

            def argv_of(result: subprocess.CompletedProcess[str]) -> list[object]:
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = installer.strict_json(result.stdout.encode("utf-8"))
                self.assertIsInstance(payload, dict)
                assert isinstance(payload, dict)
                self.assertIsNone(payload["resourceGuard"])
                argv = payload["argv"]
                self.assertIsInstance(argv, list)
                assert isinstance(argv, list)
                return argv[1:]

            guards = [
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
            ]
            # A representative sample of the 17 real value-taking upstream
            # options (dist/cli/args.js), each left with a missing/empty
            # trailing value -- the realistic `--model "$UNSET_VAR"` shape --
            # across every RUNTIME_PUBLIC_COMMANDS member plus the "help"
            # MISS case. The fixed placement must land the guards BEFORE the
            # trailing flag, never after it (which is exactly what let the
            # first guard get silently swallowed as that flag's own value
            # pre-fix).
            cases = (
                (("model", "list", "--model"), ["model", "list"]),
                (("model", "list", "--provider"), ["model", "list"]),
                (("model", "list", "--api-key"), ["model", "list"]),
                (("model", "list", "--theme"), ["model", "list"]),
                (("model", "list", "--tools"), ["model", "list"]),
                (("model", "list", "-t"), ["model", "list"]),
                (("model", "list", "--thinking"), ["model", "list"]),
                (("model", "list", "--extension"), ["model", "list"]),
                (("session", "export", "/tmp/x", "--model"), ["session", "export", "/tmp/x"]),
                (("attach", "myagent", "--model"), ["attach", "myagent"]),
                (("agents", "--model"), ["agents"]),
                (("help", "zzzzzzzzzzzz", "--model"), ["help", "zzzzzzzzzzzz"]),
            )
            for arguments, prefix in cases:
                with self.subTest(arguments=arguments):
                    observed = argv_of(run_wrapper(arguments))
                    trailing_flag = arguments[len(prefix):]
                    self.assertEqual(
                        observed,
                        [*prefix, *guards, *trailing_flag],
                        f"guards must land before the trailing flag {trailing_flag!r}, "
                        "not after it (where a real upstream value-taking option "
                        "would silently swallow the first guard as its own value)",
                    )

            # Companion positive check: the SAME options with a real,
            # non-empty value must still parse that value correctly (no
            # functional regression) while still carrying every guard.
            legit_cases = (
                (
                    ("model", "list", "--model", "gpt4"),
                    ["model", "list", *guards, "--model", "gpt4"],
                ),
                (
                    ("session", "export", "/tmp/x", "/tmp/y"),
                    ["session", "export", "/tmp/x", "/tmp/y", *guards],
                ),
                (
                    ("attach", "myagent"),
                    ["attach", "myagent", *guards],
                ),
            )
            for arguments, expected in legit_cases:
                with self.subTest(arguments=arguments, mode="legit"):
                    observed = argv_of(run_wrapper(arguments))
                    self.assertEqual(observed, expected)

    def test_atomic_create_private_file_never_clobbers_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            parent = root / "private"
            parent.mkdir(mode=0o700)
            path = parent / "receipt.json"
            original_rename = installer.rename_noreplace
            inserted = False

            def insert_late_occupant(source: Path, destination: Path) -> None:
                nonlocal inserted
                if destination == path and not inserted:
                    inserted = True
                    destination.write_text("late occupant", encoding="utf-8")
                    os.chmod(destination, 0o600)
                original_rename(source, destination)

            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(
                    installer,
                    "rename_noreplace",
                    side_effect=insert_late_occupant,
                ),
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "refusing to replace"
                ):
                    installer.atomic_create_private_file(path, b"managed\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "late occupant")
            self.assertEqual(list(parent.glob(f".{path.name}.*")), [])

            rolled_back = parent / "pending.json"
            calls = 0

            def fail_publication_sync(_path: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise installer.PrimeInstallError(
                        "injected create publication sync failure"
                    )

            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(
                    installer,
                    "fsync_directory",
                    side_effect=fail_publication_sync,
                ),
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "was rolled back"
                ):
                    installer.atomic_create_private_file(rolled_back, b"pending\n")
            self.assertEqual(calls, 2)
            self.assertFalse(rolled_back.exists())
            self.assertEqual(list(parent.glob(f".{rolled_back.name}.*")), [])

            large_path = parent / "large-asset.tgz"
            large_raw = b"x" * (4 * 1024 * 1024 + 1)
            with mock.patch.object(installer, "SSD_ROOT", root):
                installer.atomic_create_private_file(large_path, large_raw)
                self.assertEqual(large_path.stat().st_size, len(large_raw))
                installer.remove_private_file_durable(large_path, large_raw)
            self.assertFalse(large_path.exists())

    def test_atomic_symlink_never_clobbers_existing_occupant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            target = ssd / "target"
            target.parent.mkdir(mode=0o700)
            target.write_text("target", encoding="utf-8")
            user_home = root / "user"
            link = user_home / ".local/bin/prime-agent"
            link.parent.mkdir(parents=True, mode=0o700)
            link.write_text("unrelated", encoding="utf-8")
            os.chmod(link, 0o600)
            with (
                mock.patch.object(installer, "SSD_ROOT", ssd),
                mock.patch.object(installer, "USER_HOME", user_home),
            ):
                with self.assertRaisesRegex(installer.PrimeInstallError, "refusing"):
                    installer.atomic_symlink(target, link)
            self.assertTrue(link.is_file())
            self.assertEqual(link.read_text(encoding="utf-8"), "unrelated")

    def test_new_private_and_link_ancestors_are_durably_synced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            ssd.mkdir(mode=0o700)
            private = ssd / "shared-tools"
            synced: list[Path] = []

            with (
                mock.patch.object(installer, "SSD_ROOT", ssd),
                mock.patch.object(
                    installer,
                    "fsync_directory",
                    side_effect=lambda path: synced.append(Path(path)),
                ),
            ):
                installer.ensure_private_dir(private)
                self.assertEqual(synced, [ssd])
                synced.clear()
                installer.ensure_private_dir(private)
                self.assertEqual(synced, [ssd])

            user_home = root / "user"
            user_home.mkdir(mode=0o700)
            link = user_home / ".local/bin/prime-agent"
            synced.clear()
            with (
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(
                    installer,
                    "fsync_directory",
                    side_effect=lambda path: synced.append(Path(path)),
                ),
            ):
                parent_descriptor = installer.ensure_local_link_parent(link)
                self.assertIsInstance(parent_descriptor, int)
                os.close(parent_descriptor)
            self.assertEqual(synced, [user_home, user_home / ".local"])

            failing_home = root / "failing-user"
            failing_home.mkdir(mode=0o700)
            failing_link = failing_home / ".local/bin/prime-agent"
            target = ssd / "target"
            target.write_text("target", encoding="utf-8")
            with (
                mock.patch.object(installer, "SSD_ROOT", ssd),
                mock.patch.object(installer, "USER_HOME", failing_home),
                mock.patch.object(
                    installer,
                    "fsync_directory",
                    side_effect=installer.PrimeInstallError(
                        "injected ancestor directory sync failure"
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "ancestor directory sync"
                ):
                    installer.atomic_symlink(target, failing_link)
            self.assertFalse(failing_link.exists())
            self.assertFalse(failing_link.is_symlink())

    def test_atomic_symlink_rolls_back_post_create_fsync_failure(self) -> None:
        # remove_exact_symlink() -- now bound to the same dir_fd-chained
        # ancestor descriptor as creation -- also durably syncs via
        # fsync_open_directory() at the end of its own successful removal
        # (see test_remove_exact_symlink_ancestor_swap_cannot_escape_verified_parent).
        # atomic_symlink()'s rollback path therefore calls
        # fsync_open_directory() a SECOND time (inside remove_exact_symlink's
        # cleanup) after the injected creation-side failure. Only the first
        # call -- the one this test targets -- must fail; the rollback's own
        # call must succeed so the durable "rolled back" outcome this test
        # asserts is still reachable, exactly as it was before removal
        # shared this helper with creation.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            target = ssd / "target"
            target.parent.mkdir(mode=0o700)
            target.write_text("target", encoding="utf-8")
            user_home = root / "user"
            link = user_home / ".local/bin/prime-agent"
            link.parent.mkdir(parents=True, mode=0o700)

            real_fsync_open_directory = installer.fsync_open_directory
            sync_calls = 0

            def fail_first_post_create_sync(descriptor: int) -> None:
                nonlocal sync_calls
                sync_calls += 1
                if sync_calls == 1:
                    raise installer.PrimeInstallError(
                        "injected post-create directory fsync failure"
                    )
                real_fsync_open_directory(descriptor)

            with (
                mock.patch.object(installer, "SSD_ROOT", ssd),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(
                    installer,
                    "fsync_open_directory",
                    side_effect=fail_first_post_create_sync,
                ),
            ):
                with self.assertRaisesRegex(installer.PrimeInstallError, "rolled back"):
                    installer.atomic_symlink(target, link)
            self.assertFalse(link.exists())
            self.assertFalse(link.is_symlink())
            self.assertEqual(list(link.parent.glob(f".{link.name}.remove-*")), [])

    def test_exact_symlink_removal_restores_late_unrelated_occupant(self) -> None:
        # remove_exact_symlink() now performs its quarantine rename via
        # rename_noreplace_dir_fd() (dir_fd + bare name), not the lexical
        # rename_noreplace() this test previously hooked -- see
        # test_remove_exact_symlink_ancestor_swap_cannot_escape_verified_parent
        # for the dir_fd-binding regression test itself. This test still
        # proves the SAME late-occupant-during-removal race is refused, just
        # hooked at the new call site.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            target = ssd / "target"
            target.parent.mkdir(mode=0o700)
            target.write_text("target", encoding="utf-8")
            user_home = root / "user"
            link = user_home / ".local/bin/prime-agent"
            link.parent.mkdir(parents=True, mode=0o700)
            link.symlink_to(target)
            original_rename_dir_fd = installer.rename_noreplace_dir_fd
            inserted = False

            def insert_late_occupant(
                src_dir_fd: int,
                src_name: str,
                dst_dir_fd: int,
                dst_name: str,
                *,
                display_destination: Path,
            ) -> None:
                nonlocal inserted
                if src_name == link.name and not inserted:
                    inserted = True
                    link.unlink()
                    link.write_text("late occupant", encoding="utf-8")
                    os.chmod(link, 0o600)
                original_rename_dir_fd(
                    src_dir_fd,
                    src_name,
                    dst_dir_fd,
                    dst_name,
                    display_destination=display_destination,
                )

            with (
                mock.patch.object(installer, "SSD_ROOT", ssd),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(
                    installer,
                    "rename_noreplace_dir_fd",
                    side_effect=insert_late_occupant,
                ),
            ):
                with self.assertRaisesRegex(installer.PrimeInstallError, "identity changed"):
                    installer.remove_exact_symlink(link, target)
            self.assertTrue(link.is_file())
            self.assertEqual(link.read_text(encoding="utf-8"), "late occupant")
            self.assertEqual(target.read_text(encoding="utf-8"), "target")

    def test_verify_link_rejects_resolved_two_hop_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            target = ssd / "target"
            target.parent.mkdir(mode=0o700)
            target.write_text("target", encoding="utf-8")
            alias = ssd / "alias"
            alias.symlink_to(target)
            user_home = root / "user"
            link = user_home / ".local/bin/prime-agent"
            link.parent.mkdir(parents=True, mode=0o700)
            link.symlink_to(alias)
            with (
                mock.patch.object(installer, "SSD_ROOT", ssd),
                mock.patch.object(installer, "USER_HOME", user_home),
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "target drifted"
                ):
                    installer.verify_link(link, target)
            self.assertEqual(os.readlink(link), os.fspath(alias))
            self.assertEqual(target.read_text(encoding="utf-8"), "target")

    def test_quarantine_rejects_release_symlink_without_moving_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            release.parent.mkdir(parents=True)
            victim = root / "unrelated"
            victim.mkdir(mode=0o700)
            sentinel = victim / "sentinel"
            sentinel.write_text("keep", encoding="utf-8")
            release.symlink_to(victim, target_is_directory=True)
            user_home = root / "user"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
                mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home"),
                mock.patch.object(installer, "STATE_LINK", user_home / ".prime"),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(installer, "BIN_LINK", user_home / ".local/bin/prime-agent"),
                mock.patch.object(installer, "RECEIPT_PATH", tool_root / "receipt.json"),
                mock.patch.object(installer, "PENDING_PATH", tool_root / "pending.json"),
            ):
                with self.assertRaisesRegex(installer.PrimeInstallError, "is a symlink"):
                    installer.quarantine_partial_release()
            self.assertTrue(release.is_symlink())
            self.assertTrue(sentinel.is_file())

    def test_quarantine_retains_partial_release_state_and_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            release.mkdir(parents=True, mode=0o700)
            os.chmod(release, 0o700)
            payload = release / "partial"
            payload.write_text("partial", encoding="utf-8")
            os.chmod(payload, 0o600)
            state = tool_root / "state"
            probe = tool_root / "probe-home"
            self.create_managed_home(state, session_dir=tool_root / "sessions")
            self.create_managed_home(probe, session_dir=tool_root / "sessions")
            user_home = root / "user"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", state),
                mock.patch.object(installer, "PROBE_HOME", probe),
                mock.patch.object(installer, "STATE_LINK", user_home / ".prime"),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(installer, "BIN_LINK", user_home / ".local/bin/prime-agent"),
                mock.patch.object(installer, "RECEIPT_PATH", tool_root / "receipt.json"),
                mock.patch.object(installer, "PENDING_PATH", tool_root / "pending.json"),
                mock.patch.object(installer, "managed_process_ids", return_value=[]),
            ):
                result = installer.quarantine_partial_release()
            destination = Path(result["path"])
            self.assertEqual(result["items"], ["release", "state", "probe-home"])
            self.assertTrue((destination / "release/partial").is_file())
            self.assertTrue((destination / "state/agent/settings.json").is_file())
            self.assertTrue((destination / "probe-home/agent/settings.json").is_file())
            self.assertFalse(release.exists())
            self.assertFalse(state.exists())
            self.assertFalse(probe.exists())

    def test_quarantine_rolls_back_each_failed_bundle_move(self) -> None:
        for failing_label in ("release", "state", "probe-home"):
            with self.subTest(failing_label=failing_label):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    tool_root = root / "tool"
                    release = tool_root / "releases" / f"v{installer.VERSION}"
                    release.mkdir(parents=True, mode=0o700)
                    os.chmod(release, 0o700)
                    payload = release / "partial"
                    payload.write_text("partial", encoding="utf-8")
                    os.chmod(payload, 0o600)
                    state = tool_root / "state"
                    probe = tool_root / "probe-home"
                    self.create_managed_home(
                        state, session_dir=tool_root / "sessions"
                    )
                    self.create_managed_home(
                        probe, session_dir=tool_root / "sessions"
                    )
                    user_home = root / "user"
                    sources = {
                        "release": release,
                        "state": state,
                        "probe-home": probe,
                    }
                    original_rename = installer.rename_noreplace
                    failed = False

                    def fail_selected_move(source: Path, destination: Path) -> None:
                        nonlocal failed
                        if Path(source) == sources[failing_label] and not failed:
                            failed = True
                            raise OSError("injected bundle move failure")
                        original_rename(source, destination)

                    with (
                        mock.patch.object(installer, "SSD_ROOT", root),
                        mock.patch.object(installer, "TOOL_ROOT", tool_root),
                        mock.patch.object(installer, "RELEASE_DIR", release),
                        mock.patch.object(installer, "STATE_DIR", state),
                        mock.patch.object(installer, "PROBE_HOME", probe),
                        mock.patch.object(installer, "STATE_LINK", user_home / ".prime"),
                        mock.patch.object(installer, "USER_HOME", user_home),
                        mock.patch.object(
                            installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                        ),
                        mock.patch.object(
                            installer, "RECEIPT_PATH", tool_root / "receipt.json"
                        ),
                        mock.patch.object(
                            installer, "PENDING_PATH", tool_root / "pending.json"
                        ),
                        mock.patch.object(installer, "managed_process_ids", return_value=[]),
                        mock.patch.object(
                            installer, "rename_noreplace", side_effect=fail_selected_move
                        ),
                    ):
                        with self.assertRaisesRegex(
                            installer.PrimeInstallError, "recovery evidence"
                        ):
                            installer.quarantine_partial_release()
                    self.assertTrue(all(path.exists() for path in sources.values()))
                    recovery = next((tool_root / "recovery").iterdir())
                    manifest = installer.strict_json(
                        (recovery / "manifest.json").read_bytes()
                    )
                    self.assertEqual(manifest["status"], "rolled_back")

    def test_quarantine_rolls_back_each_failed_move_fsync(self) -> None:
        # The first two directory syncs durably create recovery_root and its
        # per-attempt destination; call 3 publishes the create-only manifest.
        # Calls 4-10 are the three move pairs plus the final recovery-root
        # commit sync exercised by this rollback test.
        for failing_call in range(4, 11):
            with self.subTest(failing_call=failing_call):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    tool_root = root / "tool"
                    release = tool_root / "releases" / f"v{installer.VERSION}"
                    release.mkdir(parents=True, mode=0o700)
                    os.chmod(release, 0o700)
                    payload = release / "partial"
                    payload.write_text("partial", encoding="utf-8")
                    os.chmod(payload, 0o600)
                    state = tool_root / "state"
                    probe = tool_root / "probe-home"
                    self.create_managed_home(
                        state, session_dir=tool_root / "sessions"
                    )
                    self.create_managed_home(
                        probe, session_dir=tool_root / "sessions"
                    )
                    user_home = root / "user"
                    calls = 0
                    injected = False

                    def fail_selected_fsync(_path: Path) -> None:
                        nonlocal calls, injected
                        calls += 1
                        if calls == failing_call and not injected:
                            injected = True
                            raise installer.PrimeInstallError(
                                "injected directory fsync failure"
                            )

                    with (
                        mock.patch.object(installer, "SSD_ROOT", root),
                        mock.patch.object(installer, "TOOL_ROOT", tool_root),
                        mock.patch.object(installer, "RELEASE_DIR", release),
                        mock.patch.object(installer, "STATE_DIR", state),
                        mock.patch.object(installer, "PROBE_HOME", probe),
                        mock.patch.object(installer, "STATE_LINK", user_home / ".prime"),
                        mock.patch.object(installer, "USER_HOME", user_home),
                        mock.patch.object(
                            installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                        ),
                        mock.patch.object(
                            installer, "RECEIPT_PATH", tool_root / "receipt.json"
                        ),
                        mock.patch.object(
                            installer, "PENDING_PATH", tool_root / "pending.json"
                        ),
                        mock.patch.object(installer, "managed_process_ids", return_value=[]),
                        mock.patch.object(
                            installer, "fsync_directory", side_effect=fail_selected_fsync
                        ),
                    ):
                        with self.assertRaisesRegex(
                            installer.PrimeInstallError, "recovery evidence"
                        ):
                            installer.quarantine_partial_release()
                    self.assertTrue(release.exists())
                    self.assertTrue(state.exists())
                    self.assertTrue(probe.exists())
                    recovery = next((tool_root / "recovery").iterdir())
                    manifest = installer.strict_json(
                        (recovery / "manifest.json").read_bytes()
                    )
                    self.assertEqual(manifest["status"], "rolled_back")

    def test_quarantine_crash_during_rollback_remains_nonterminal(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            recovery_root = tool_root / "recovery"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            release.mkdir(parents=True, mode=0o700)
            os.chmod(release, 0o700)
            (release / "partial").write_text("partial", encoding="utf-8")
            state = tool_root / "state"
            self.create_managed_home(state, session_dir=tool_root / "sessions")
            user_home = root / "user"
            original_rename = installer.rename_noreplace
            recovery_root_syncs = 0

            def fail_final_recovery_root_sync(path: Path) -> None:
                nonlocal recovery_root_syncs
                if Path(path) == recovery_root:
                    recovery_root_syncs += 1
                    if recovery_root_syncs == 2:
                        raise installer.PrimeInstallError(
                            "injected final recovery-root sync failure"
                        )

            def crash_on_first_reverse_move(source: Path, target: Path) -> None:
                if target == state and source != state:
                    raise SimulatedCrash("injected crash during rollback")
                original_rename(source, target)

            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", state),
                mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home"),
                mock.patch.object(installer, "STATE_LINK", user_home / ".prime"),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(
                    installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                ),
                mock.patch.object(
                    installer, "RECEIPT_PATH", tool_root / "receipt.json"
                ),
                mock.patch.object(
                    installer, "PENDING_PATH", tool_root / "pending.json"
                ),
                mock.patch.object(installer, "managed_process_ids", return_value=[]),
                mock.patch.object(
                    installer,
                    "fsync_directory",
                    side_effect=fail_final_recovery_root_sync,
                ),
                mock.patch.object(
                    installer,
                    "rename_noreplace",
                    side_effect=crash_on_first_reverse_move,
                ),
            ):
                with self.assertRaises(SimulatedCrash):
                    installer.quarantine_partial_release()
                destination = next(recovery_root.iterdir())
                manifest = installer.strict_json(
                    (destination / "manifest.json").read_bytes()
                )
                self.assertEqual(manifest["status"], "rolling_back")
                expected = f"RECOVERY:{destination}:rolling_back"
                self.assertEqual(installer.recovery_manifest_conflicts(), [expected])
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "unresolved or unknown"
                ):
                    installer.resume_incomplete_quarantine()

    def test_quarantine_never_clobbers_late_forward_or_rollback_occupants(self) -> None:
        with self.subTest(race="forward"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                tool_root = root / "tool"
                release = tool_root / "releases" / f"v{installer.VERSION}"
                release.mkdir(parents=True, mode=0o700)
                os.chmod(release, 0o700)
                (release / "partial").write_text("original", encoding="utf-8")
                user_home = root / "user"
                original_rename = installer.rename_noreplace
                inserted = False

                def insert_forward_occupant(source: Path, target: Path) -> None:
                    nonlocal inserted
                    if source == release and not inserted:
                        inserted = True
                        target.mkdir(mode=0o700)
                        (target / "late").write_text("late", encoding="utf-8")
                    original_rename(source, target)

                with (
                    mock.patch.object(installer, "SSD_ROOT", root),
                    mock.patch.object(installer, "TOOL_ROOT", tool_root),
                    mock.patch.object(installer, "RELEASE_DIR", release),
                    mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home"),
                    mock.patch.object(installer, "STATE_LINK", user_home / ".prime"),
                    mock.patch.object(installer, "USER_HOME", user_home),
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    ),
                    mock.patch.object(
                        installer, "RECEIPT_PATH", tool_root / "receipt.json"
                    ),
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending.json"
                    ),
                    mock.patch.object(installer, "managed_process_ids", return_value=[]),
                    mock.patch.object(
                        installer,
                        "rename_noreplace",
                        side_effect=insert_forward_occupant,
                    ),
                ):
                    with self.assertRaisesRegex(
                        installer.PrimeInstallError, "recovery evidence"
                    ):
                        installer.quarantine_partial_release()
                recovery = next((tool_root / "recovery").iterdir())
                self.assertEqual(
                    (release / "partial").read_text(encoding="utf-8"), "original"
                )
                self.assertEqual(
                    (recovery / "release/late").read_text(encoding="utf-8"), "late"
                )
                manifest = installer.strict_json(
                    (recovery / "manifest.json").read_bytes()
                )
                self.assertEqual(manifest["status"], "rolled_back")

        with self.subTest(race="rollback"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                tool_root = root / "tool"
                release = tool_root / "releases" / f"v{installer.VERSION}"
                release.mkdir(parents=True, mode=0o700)
                os.chmod(release, 0o700)
                (release / "partial").write_text("original", encoding="utf-8")
                state = tool_root / "state"
                probe = tool_root / "probe-home"
                self.create_managed_home(state, session_dir=tool_root / "sessions")
                self.create_managed_home(probe, session_dir=tool_root / "sessions")
                user_home = root / "user"
                original_rename = installer.rename_noreplace
                failed = False

                def fail_after_late_source(source: Path, target: Path) -> None:
                    nonlocal failed
                    if source == state and not failed:
                        failed = True
                        release.mkdir(mode=0o700)
                        (release / "late").write_text("late", encoding="utf-8")
                        raise OSError("injected move failure after late occupant")
                    original_rename(source, target)

                with (
                    mock.patch.object(installer, "SSD_ROOT", root),
                    mock.patch.object(installer, "TOOL_ROOT", tool_root),
                    mock.patch.object(installer, "RELEASE_DIR", release),
                    mock.patch.object(installer, "STATE_DIR", state),
                    mock.patch.object(installer, "PROBE_HOME", probe),
                    mock.patch.object(installer, "STATE_LINK", user_home / ".prime"),
                    mock.patch.object(installer, "USER_HOME", user_home),
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    ),
                    mock.patch.object(
                        installer, "RECEIPT_PATH", tool_root / "receipt.json"
                    ),
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending.json"
                    ),
                    mock.patch.object(installer, "managed_process_ids", return_value=[]),
                    mock.patch.object(
                        installer,
                        "rename_noreplace",
                        side_effect=fail_after_late_source,
                    ),
                ):
                    with self.assertRaisesRegex(
                        installer.PrimeInstallError, "recovery evidence"
                    ):
                        installer.quarantine_partial_release()
                recovery = next((tool_root / "recovery").iterdir())
                self.assertEqual(
                    (release / "late").read_text(encoding="utf-8"), "late"
                )
                self.assertEqual(
                    (recovery / "release/partial").read_text(encoding="utf-8"),
                    "original",
                )
                manifest = installer.strict_json(
                    (recovery / "manifest.json").read_bytes()
                )
                self.assertEqual(manifest["status"], "rollback_failed")
                self.assertTrue(
                    any("occupied" in error for error in manifest["rollback_errors"])
                )

    def test_recover_resumes_a_moving_quarantine_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            recovery_root = tool_root / "recovery"
            destination = recovery_root / "partial-interrupted"
            destination.mkdir(parents=True, mode=0o700)
            for path in (tool_root, recovery_root, destination):
                os.chmod(path, 0o700)
            release = tool_root / "releases" / f"v{installer.VERSION}"
            release.parent.mkdir(mode=0o700)
            recovered_release = destination / "release"
            recovered_release.mkdir(mode=0o700)
            os.chmod(recovered_release, 0o700)
            (recovered_release / "partial").write_text(
                "original", encoding="utf-8"
            )
            state = tool_root / "state"
            probe = tool_root / "probe-home"
            sessions = tool_root / "sessions"
            self.create_managed_home(state, session_dir=sessions)
            self.create_managed_home(probe, session_dir=sessions)
            sessions.mkdir(mode=0o700)
            manifest_path = destination / "manifest.json"
            installer.atomic_write(
                manifest_path,
                installer.canonical_json(
                    {
                        "schema": "orca.prime-agent-partial-recovery.v1",
                        "version": installer.VERSION,
                        "status": "moving",
                        "items": ["release", "state", "probe-home", "sessions"],
                    }
                ),
                0o600,
            )
            synced: list[Path] = []
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", state),
                mock.patch.object(installer, "PROBE_HOME", probe),
                mock.patch.object(installer, "managed_process_ids", return_value=[]),
                mock.patch.object(
                    installer,
                    "fsync_directory",
                    side_effect=lambda path: synced.append(Path(path)),
                ),
            ):
                result = installer.resume_incomplete_quarantine()
            self.assertIsNotNone(result)
            assert result is not None
            self.assertTrue(result["resumed_after_interruption"])
            for label, source in (
                ("state", state),
                ("probe-home", probe),
                ("sessions", sessions),
            ):
                self.assertFalse(source.exists())
                self.assertTrue((destination / label).exists())
            self.assertEqual(
                (destination / "release/partial").read_text(encoding="utf-8"),
                "original",
            )
            manifest = installer.strict_json(manifest_path.read_bytes())
            self.assertEqual(manifest["status"], "quarantined")
            self.assertTrue(manifest["resumed_after_interruption"])
            self.assertEqual(synced[:2], [release.parent, destination])
            self.assertIn(recovery_root, synced)

    def test_recover_blocks_unresolved_or_unknown_recovery_manifests(self) -> None:
        for status in ("recovery_conflict", "rollback_failed", "unexpected"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                tool_root = root / "tool"
                recovery_root = tool_root / "recovery"
                destination = recovery_root / "partial-unresolved"
                destination.mkdir(parents=True, mode=0o700)
                for path in (tool_root, recovery_root, destination):
                    os.chmod(path, 0o700)
                installer.atomic_write(
                    destination / "manifest.json",
                    installer.canonical_json(
                        {
                            "schema": "orca.prime-agent-partial-recovery.v1",
                            "version": installer.VERSION,
                            "status": status,
                            "items": ["release"],
                        }
                    ),
                    0o600,
                )
                pending = tool_root / "pending.json"
                receipt = tool_root / "receipt.json"
                pending.write_bytes(b"pending")
                receipt.write_bytes(b"receipt")
                os.chmod(pending, 0o600)
                os.chmod(receipt, 0o600)
                with (
                    mock.patch.object(installer, "SSD_ROOT", root),
                    mock.patch.object(installer, "TOOL_ROOT", tool_root),
                    mock.patch.object(
                        installer,
                        "RELEASE_DIR",
                        tool_root / "releases" / f"v{installer.VERSION}",
                    ),
                    mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
                    mock.patch.object(
                        installer, "PROBE_HOME", tool_root / "probe-home"
                    ),
                    mock.patch.object(
                        installer, "PENDING_PATH", pending
                    ),
                    mock.patch.object(
                        installer, "RECEIPT_PATH", receipt
                    ),
                    mock.patch.object(
                        installer, "finalize_pending_install"
                    ) as finalize,
                    mock.patch.object(installer, "verify") as verify,
                    mock.patch.object(
                        installer, "quarantine_partial_release"
                    ) as quarantine,
                ):
                    with self.assertRaisesRegex(
                        installer.PrimeInstallError, "unresolved or unknown"
                    ):
                        installer._recover_locked((0, 0))
                finalize.assert_not_called()
                verify.assert_not_called()
                quarantine.assert_not_called()

    def test_plan_reports_unresolved_recovery_manifest_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            recovery_root = tool_root / "recovery"
            destination = recovery_root / "partial-unresolved"
            destination.mkdir(parents=True, mode=0o700)
            for path in (tool_root, recovery_root, destination):
                os.chmod(path, 0o700)
            manifest_path = destination / "manifest.json"
            manifest_raw = installer.canonical_json(
                {
                    "schema": "orca.prime-agent-partial-recovery.v1",
                    "version": installer.VERSION,
                    "status": "rollback_failed",
                    "items": ["release"],
                }
            )
            installer.atomic_write(manifest_path, manifest_raw, 0o600)
            user_home = root / "user"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(
                    installer,
                    "RELEASE_DIR",
                    tool_root / "releases" / f"v{installer.VERSION}",
                ),
                mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
                mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home"),
                mock.patch.object(installer, "STATE_LINK", user_home / ".prime"),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(
                    installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                ),
                mock.patch.object(
                    installer, "RECEIPT_PATH", tool_root / "receipt.json"
                ),
                mock.patch.object(
                    installer, "PENDING_PATH", tool_root / "pending.json"
                ),
                mock.patch.object(
                    installer,
                    "preflight",
                    return_value={
                        "volume_uuid": "TEST-UUID",
                        "node_version": installer.NODE_VERSION,
                        "npm_version": installer.NPM_VERSION,
                        "orca_support": {"test": "support"},
                    },
                ),
                mock.patch.object(
                    installer, "prime_agent_command_candidates", return_value=[]
                ),
            ):
                result = installer.plan()
            expected = f"RECOVERY:{destination}:rollback_failed"
            self.assertFalse(result["ok"])
            self.assertEqual(result["recovery_conflicts"], [expected])
            self.assertIn(expected, result["conflicts"])
            self.assertEqual(manifest_path.read_bytes(), manifest_raw)

    def test_pending_install_refuses_missing_probe_without_committing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            release.mkdir(parents=True, mode=0o700)
            os.chmod(release, 0o700)
            state = tool_root / "state"
            self.create_managed_home(state, session_dir=tool_root / "sessions")
            probe = tool_root / "probe-home"
            pending = tool_root / "pending.json"
            receipt_path = tool_root / "receipt.json"
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            receipt = {
                "volume_uuid": "TEST-UUID",
                "orca_support": {"test": "support"},
                "lifecycle_lock": os.fspath(lifecycle_lock),
            }
            installer.atomic_write(
                pending,
                installer.canonical_json(
                    {"schema": installer.JOURNAL_SCHEMA, "receipt": receipt}
                ),
                0o600,
            )
            user_home = root / "user"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", state),
                mock.patch.object(installer, "PROBE_HOME", probe),
                mock.patch.object(installer, "STATE_LINK", user_home / ".prime"),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(installer, "BIN_LINK", user_home / ".local/bin/prime-agent"),
                mock.patch.object(installer, "RECEIPT_PATH", receipt_path),
                mock.patch.object(installer, "PENDING_PATH", pending),
                mock.patch.object(installer, "validate_receipt_identity", return_value=receipt),
                mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID"),
                mock.patch.object(
                    installer, "verify_orca_support", return_value={"test": "support"}
                ),
            ):
                with self.assertRaisesRegex(installer.PrimeInstallError, "probe-home"):
                    installer.finalize_pending_install()
            self.assertTrue(pending.is_file())
            self.assertFalse(receipt_path.exists())
            self.assertFalse((user_home / ".prime").exists())

    def test_pending_install_checks_alternate_command_before_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pending = Path(directory).resolve() / "pending-install.json"
            pending.write_bytes(b"pending")
            with (
                mock.patch.object(installer, "PENDING_PATH", pending),
                mock.patch.object(
                    installer,
                    "prime_agent_command_candidates",
                    return_value=[Path("/alternate/bin/prime-agent")],
                ) as scan,
                mock.patch.object(installer, "finalize_pending_install") as finalize,
                mock.patch.object(installer, "preflight") as preflight,
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "existing prime-agent command"
                ):
                    installer._install_locked((0, 0))
            scan.assert_called_once_with()
            finalize.assert_not_called()
            preflight.assert_not_called()

    def test_pending_journal_follows_complete_tree_durability_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            release.mkdir(parents=True, mode=0o700)
            payload = release / "payload"
            payload.write_text("runtime", encoding="utf-8")
            os.chmod(payload, 0o600)
            state = tool_root / "state"
            probe = tool_root / "probe-home"
            self.create_managed_home(state, session_dir=tool_root / "sessions")
            self.create_managed_home(probe, session_dir=tool_root / "sessions")
            sessions = tool_root / "sessions"
            sessions.mkdir(mode=0o700)
            os.chmod(tool_root, 0o700)
            pending = tool_root / "pending-install.json"
            receipt = {"version": installer.VERSION}
            events: list[str] = []
            real_atomic_create = installer.atomic_create_private_file

            def record_sync(path: Path) -> None:
                events.append("sync:" + os.fspath(path))

            def record_write(path: Path, raw: bytes, mode: int = 0o600) -> None:
                events.append("write:" + os.fspath(path))
                real_atomic_create(path, raw, mode)

            patches = (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", state),
                mock.patch.object(installer, "PROBE_HOME", probe),
                mock.patch.object(installer, "PENDING_PATH", pending),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                with (
                    mock.patch.object(
                        installer, "sync_private_tree", side_effect=record_sync
                    ),
                    mock.patch.object(
                        installer,
                        "atomic_create_private_file",
                        side_effect=record_write,
                    ),
                ):
                    durable = installer.write_pending_install(receipt)
                self.assertEqual(
                    events,
                    [
                        "sync:" + os.fspath(release),
                        "sync:" + os.fspath(state),
                        "sync:" + os.fspath(probe),
                        "sync:" + os.fspath(sessions),
                        "write:" + os.fspath(pending),
                    ],
                )
                self.assertIn("release_tree_sha256", durable)
                self.assertTrue(pending.is_file())
                pending.unlink()

                for failing_call in range(1, 5):
                    with self.subTest(failing_call=failing_call):
                        calls = 0

                        def fail_selected_sync(_path: Path) -> None:
                            nonlocal calls
                            calls += 1
                            if calls == failing_call:
                                raise installer.PrimeInstallError(
                                    "injected durable-tree sync failure"
                                )

                        with (
                            mock.patch.object(
                                installer,
                                "sync_private_tree",
                                side_effect=fail_selected_sync,
                            ),
                            mock.patch.object(
                                installer, "atomic_create_private_file"
                            ) as journal_write,
                        ):
                            with self.assertRaisesRegex(
                                installer.PrimeInstallError, "injected"
                            ):
                                installer.write_pending_install(receipt)
                        journal_write.assert_not_called()
                        self.assertFalse(pending.exists())

    def test_pending_journal_publication_preserves_late_occupant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            release.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root, 0o700)
            os.chmod(release, 0o700)
            payload = release / "payload"
            payload.write_text("runtime", encoding="utf-8")
            os.chmod(payload, 0o600)
            state = tool_root / "state"
            probe = tool_root / "probe-home"
            sessions = tool_root / "sessions"
            self.create_managed_home(state, session_dir=sessions)
            self.create_managed_home(probe, session_dir=sessions)
            sessions.mkdir(mode=0o700)
            pending = tool_root / "pending-install.json"
            original_rename = installer.rename_noreplace
            inserted = False

            def insert_late_journal(source: Path, destination: Path) -> None:
                nonlocal inserted
                if destination == pending and not inserted:
                    inserted = True
                    destination.write_text("late journal", encoding="utf-8")
                    os.chmod(destination, 0o600)
                original_rename(source, destination)

            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", state),
                mock.patch.object(installer, "PROBE_HOME", probe),
                mock.patch.object(installer, "PENDING_PATH", pending),
                mock.patch.object(
                    installer,
                    "rename_noreplace",
                    side_effect=insert_late_journal,
                ),
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "refusing to replace"
                ):
                    installer.write_pending_install({"version": installer.VERSION})
            self.assertEqual(pending.read_text(encoding="utf-8"), "late journal")
            self.assertEqual(list(tool_root.glob(f".{pending.name}.*")), [])

    def test_sync_private_tree_orders_file_and_directory_barriers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree = root / "tree"
            child = tree / "child"
            child.mkdir(parents=True, mode=0o700)
            os.chmod(tree, 0o700)
            payload = child / "payload"
            payload.write_text("runtime", encoding="utf-8")
            os.chmod(payload, 0o600)
            (tree / "payload-link").symlink_to(Path("child/payload"))
            events: list[str] = []
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(
                    installer,
                    "fsync_regular_file",
                    side_effect=lambda path: events.append("file:" + os.fspath(path)),
                ),
                mock.patch.object(
                    installer,
                    "fsync_directory",
                    side_effect=lambda path: events.append("dir:" + os.fspath(path)),
                ),
            ):
                installer.sync_private_tree(tree)
            self.assertEqual(
                events,
                [
                    "file:" + os.fspath(payload),
                    "dir:" + os.fspath(child),
                    "dir:" + os.fspath(tree),
                    "dir:" + os.fspath(tree.parent),
                ],
            )

    def test_uninstall_restores_command_if_process_appears_after_disable(self) -> None:
        receipt = {"bin_target": "/managed/prime-agent"}
        events: list[str] = []

        def removed(_path: Path, _target: Path) -> None:
            events.append("removed")

        def scanned(_receipt: dict[str, object]) -> list[int]:
            events.append("scanned")
            return [4321]

        def restored(_target: Path, _link: Path) -> None:
            events.append("restored")

        with (
            mock.patch.object(installer, "load_receipt", return_value=receipt),
            # Not under test here (see
            # test_uninstall_reasserts_lifecycle_lock_identity_before_mutation
            # for that): stub to a no-op so this test's real subject --
            # process-appears-after-disable restore -- is unaffected by
            # uninstall()'s round-6 lock re-assertion requiring a real,
            # matching lifecycle.lock on disk.
            mock.patch.object(installer, "assert_lifecycle_lock_path_identity"),
            mock.patch.object(installer, "verify_command_state", return_value=True),
            mock.patch.object(installer, "remove_exact_symlink", side_effect=removed),
            mock.patch.object(installer, "managed_process_ids", side_effect=scanned),
            mock.patch.object(installer, "atomic_symlink", side_effect=restored),
        ):
            with self.assertRaisesRegex(installer.PrimeInstallError, "command was restored"):
                installer._uninstall_locked((0, 0))
        self.assertEqual(events, ["removed", "scanned", "restored"])

    def test_process_scan_detects_title_only_daemon(self) -> None:
        receipt = {"release_dir": "/managed/release"}
        completed = subprocess.CompletedProcess(
            ["ps"], 0, "424242 prime-agent prime-agent\n", ""
        )
        with mock.patch.object(
            installer.subprocess, "run", return_value=completed
        ) as run:
            self.assertEqual(installer.managed_process_ids(receipt), [424242])
        self.assertIn("pid=,comm=,args=", run.call_args.args[0])

    def test_process_scan_ignores_nearby_process_title(self) -> None:
        receipt = {"release_dir": "/managed/release"}
        completed = subprocess.CompletedProcess(
            ["ps"], 0, "424242 prime-agent-helper prime-agent-helper\n", ""
        )
        with mock.patch.object(installer.subprocess, "run", return_value=completed):
            self.assertEqual(installer.managed_process_ids(receipt), [])

    def test_process_scan_ignores_managed_path_prefix_only(self) -> None:
        receipt = {"release_dir": "/managed/release"}
        entrypoint = (
            "/managed/release/lib/node_modules/prime-agent/dist/bundle/cli.js"
        )
        completed = subprocess.CompletedProcess(
            ["ps"],
            0,
            f"424242 /usr/bin/python3 /usr/bin/python3 --note={entrypoint}.backup\n",
            "",
        )
        with mock.patch.object(installer.subprocess, "run", return_value=completed):
            self.assertEqual(installer.managed_process_ids(receipt), [])

    def test_process_scan_detects_exact_path_argument_with_spaces(self) -> None:
        receipt = {"release_dir": "/managed release"}
        entrypoint = (
            "/managed release/lib/node_modules/prime-agent/dist/bundle/cli.js"
        )
        completed = subprocess.CompletedProcess(
            ["ps"], 0, f"424242 node /managed/node {entrypoint} --version\n", ""
        )
        with mock.patch.object(installer.subprocess, "run", return_value=completed):
            self.assertEqual(installer.managed_process_ids(receipt), [424242])

    def test_uninstall_scans_even_when_command_is_already_disabled(self) -> None:
        receipt = {"bin_target": "/managed/prime-agent"}
        with (
            mock.patch.object(installer, "load_receipt", return_value=receipt),
            mock.patch.object(installer, "assert_lifecycle_lock_path_identity"),
            mock.patch.object(installer, "verify_command_state", return_value=False),
            mock.patch.object(installer, "managed_process_ids", return_value=[4321]) as scan,
        ):
            with self.assertRaisesRegex(installer.PrimeInstallError, "still running"):
                installer._uninstall_locked((0, 0))
        scan.assert_called_once_with(receipt)

    def test_uninstall_restores_command_after_indeterminate_process_scan(self) -> None:
        receipt = {"bin_target": "/managed/prime-agent"}
        with (
            mock.patch.object(installer, "load_receipt", return_value=receipt),
            mock.patch.object(installer, "assert_lifecycle_lock_path_identity"),
            mock.patch.object(installer, "verify_command_state", return_value=True),
            mock.patch.object(installer, "remove_exact_symlink"),
            mock.patch.object(
                installer,
                "managed_process_ids",
                side_effect=installer.PrimeInstallError("scan unavailable"),
            ),
            mock.patch.object(installer, "atomic_symlink") as restore,
        ):
            with self.assertRaisesRegex(installer.PrimeInstallError, "command was restored"):
                installer._uninstall_locked((0, 0))
        restore.assert_called_once_with(Path(receipt["bin_target"]), installer.BIN_LINK)

    def test_uninstall_preserves_late_occupant_when_restore_fails(self) -> None:
        receipt = {"bin_target": "/managed/prime-agent"}
        with (
            mock.patch.object(installer, "load_receipt", return_value=receipt),
            mock.patch.object(installer, "assert_lifecycle_lock_path_identity"),
            mock.patch.object(installer, "verify_command_state", return_value=True),
            mock.patch.object(installer, "remove_exact_symlink"),
            mock.patch.object(installer, "managed_process_ids", return_value=[4321]),
            mock.patch.object(
                installer,
                "atomic_symlink",
                side_effect=installer.PrimeInstallError("late occupant"),
            ),
        ):
            with self.assertRaisesRegex(installer.PrimeInstallError, "not overwritten"):
                installer._uninstall_locked((0, 0))

    def test_enable_cleanup_preserves_post_unlink_durability_error(self) -> None:
        receipt = {"bin_target": "/managed/prime-agent"}
        cleanup_error = installer.PrimeInstallError(
            "managed symlink was removed but parent-directory durability is "
            "unconfirmed: /managed/bin/prime-agent"
        )
        with (
            mock.patch.object(
                installer, "verify", return_value={"command_enabled": False}
            ),
            mock.patch.object(installer, "load_receipt", return_value=receipt),
            mock.patch.object(installer, "atomic_symlink"),
            mock.patch.object(
                installer,
                "verify_command_state",
                side_effect=installer.PrimeInstallError("ambiguous command"),
            ),
            mock.patch.object(
                installer, "remove_exact_symlink", side_effect=cleanup_error
            ),
        ):
            with self.assertRaises(installer.PrimeInstallError) as raised:
                installer._enable_locked((0, 0))
        message = str(raised.exception)
        self.assertIn("removed but parent-directory durability is unconfirmed", message)
        self.assertNotIn("preserved for inspection", message)

    def test_uninstall_reasserts_lifecycle_lock_identity_before_mutation(self) -> None:
        # Regression for independent dual review round 6, 2026-08-18, P2:
        # enable() (_enable_locked() -> verify(lock_identity)) re-asserts
        # the lifecycle lock's identity before its first mutation, but
        # uninstall() (_uninstall_locked()) omitted this entirely despite
        # performing the riskiest mutation of the two -- removing the
        # managed command link. Confirms a stale caller-supplied identity
        # is detected and fails closed BEFORE remove_exact_symlink() is
        # ever called (no mutation happens), while the correct, current
        # identity lets the same operation proceed to that mutation.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            tool_root.mkdir(mode=0o700)
            lock = tool_root / "lifecycle.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
            real_identity = (lock.stat().st_dev, lock.stat().st_ino)
            unrelated = root / "unrelated-file"
            unrelated.write_bytes(b"")
            stale_identity = (unrelated.stat().st_dev, unrelated.stat().st_ino)
            receipt = {"bin_target": "/managed/prime-agent"}
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "load_receipt", return_value=receipt),
                mock.patch.object(installer, "verify_command_state", return_value=True),
                mock.patch.object(installer, "remove_exact_symlink") as remove,
                mock.patch.object(installer, "managed_process_ids", return_value=[]),
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "lifecycle lock identity changed while held",
                ):
                    installer._uninstall_locked(stale_identity)
                remove.assert_not_called()

                remove.reset_mock()
                # verify_command_state() is called again after the (mocked,
                # no-op) removal, so it must now report disabled to reach a
                # clean success return.
                with mock.patch.object(
                    installer,
                    "verify_command_state",
                    side_effect=[True, False],
                ):
                    result = installer._uninstall_locked(real_identity)
                remove.assert_called_once()
            self.assertTrue(result["ok"])
            self.assertTrue(result["command_disabled"])

    def test_exclusive_lifecycle_lock_refuses_in_flight_shared_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            tool_root.mkdir(mode=0o700)
            lock = tool_root / "lifecycle.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
            ready = root / "ready"
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import fcntl,os,sys,time; "
                        "fd=os.open(sys.argv[1],os.O_RDONLY); "
                        "fcntl.flock(fd,fcntl.LOCK_SH); "
                        "open(sys.argv[2],'wb').close(); time.sleep(30)"
                    ),
                    os.fspath(lock),
                    os.fspath(ready),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(100):
                    if ready.exists():
                        break
                    child.poll()
                    if child.returncode is not None:
                        self.fail("shared-lock child exited before readiness")
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                with (
                    mock.patch.object(installer, "SSD_ROOT", root),
                    mock.patch.object(installer, "TOOL_ROOT", tool_root),
                ):
                    with self.assertRaisesRegex(installer.PrimeInstallError, "busy"):
                        with installer.exclusive_lifecycle_lock(create=False):
                            self.fail("exclusive lock must not be acquired")
            finally:
                child.terminate()
                child.wait(timeout=5)

    def test_generated_launch_guard_holds_shared_lock_for_child_lifetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            tool_root.mkdir(mode=0o700)
            lock = tool_root / "lifecycle.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
            # `ready` doubles as the CLI argument the generated launch guard
            # itself now validates (round 13/14, 2026-08-18, P2:
            # validate_exec_target()) before ever exec'ing NODE/CLI -- it
            # must therefore already exist, as a real, private (0600)
            # regular file, before the guard runs at all, unlike before that
            # validation existed (when this fixture let fake_node itself
            # create the file as its own readiness signal). Pre-seed it with
            # non-empty placeholder content instead, and treat fake_node's
            # `: > "$1"` truncating it back to empty as the readiness signal
            # -- still proof positive that NODE actually launched and
            # received the right argv[1], just no longer entangled with
            # CLI's own existence requirement.
            ready = root / "ready"
            ready.write_bytes(b"not-yet-truncated-by-fake-node")
            os.chmod(ready, 0o600)
            fake_node = root / "fake-node"
            fake_node.write_text(
                '#!/bin/sh\n: > "$1"\nsleep 2\n', encoding="utf-8"
            )
            os.chmod(fake_node, 0o700)
            guard = root / "launch-guard.py"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
            ):
                guard.write_bytes(
                    installer.managed_launch_guard_script(
                        fake_node,
                        ready,
                        installer.sha256_file(fake_node),
                        installer.sha256_file(ready),
                    )
                )
            os.chmod(guard, 0o700)
            child = subprocess.Popen(
                ["/usr/bin/python3", "-B", os.fspath(guard)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={"PYTHONDONTWRITEBYTECODE": "1"},
            )
            try:
                for _ in range(200):
                    if ready.stat().st_size == 0:
                        break
                    child.poll()
                    if child.returncode is not None:
                        self.fail("generated launch guard exited before readiness")
                    time.sleep(0.01)
                self.assertEqual(ready.stat().st_size, 0)
                with (
                    mock.patch.object(installer, "SSD_ROOT", root),
                    mock.patch.object(installer, "TOOL_ROOT", tool_root),
                ):
                    with self.assertRaisesRegex(installer.PrimeInstallError, "busy"):
                        with installer.exclusive_lifecycle_lock(create=False):
                            self.fail("exclusive lock must not overlap launch guard")
            finally:
                child.wait(timeout=5)

    def test_launch_guard_refuses_to_exec_a_symlinked_cli(self) -> None:
        # Regression for independent review round 13/14, 2026-08-18, P2:
        # the generated launch guard used to hand NODE and CLI to
        # subprocess.run() with zero validation, unlike LOCK (~15 lines of
        # symlink/containment/ownership/mode checks) just above it in the
        # same script. A same-UID actor who replaced CLI with a symlink to
        # an arbitrary target at any point between this script being
        # generated and this exact invocation running would have had that
        # target silently exec'd as CLI. validate_exec_target() now applies
        # the same rigor LOCK already gets to both NODE and CLI immediately
        # before they are used. This is a real end-to-end run of the actual
        # generated launch guard script (no mocking of the check under
        # test): NODE is a real, valid, executable sentinel that would
        # write a marker file if it ever actually ran; CLI is a real
        # symlink pointing outside SSD_ROOT. Confirms the guard fails
        # closed BEFORE NODE ever executes -- proven by the marker never
        # appearing, not merely by the exit code.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            tool_root.mkdir(mode=0o700)
            lock = tool_root / "lifecycle.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)

            marker = root / "node-actually-ran"
            node = root / "node"
            node.write_text(
                f'#!/bin/sh\n: > {shlex.quote(os.fspath(marker))}\n',
                encoding="utf-8",
            )
            os.chmod(node, 0o700)

            outside = root.parent / f"{root.name}-cli-victim"
            outside.write_text("// attacker-controlled target\n", encoding="utf-8")
            try:
                cli = root / "cli.js"
                cli.symlink_to(outside)

                guard = root / "launch-guard.py"
                with (
                    mock.patch.object(installer, "SSD_ROOT", root),
                    mock.patch.object(installer, "TOOL_ROOT", tool_root),
                ):
                    guard.write_bytes(
                    installer.managed_launch_guard_script(
                        node,
                        cli,
                        installer.sha256_file(node),
                        installer.sha256_file(cli),
                    )
                )
                os.chmod(guard, 0o700)

                result = subprocess.run(
                    ["/usr/bin/python3", "-B", os.fspath(guard)],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                    env={"PYTHONDONTWRITEBYTECODE": "1"},
                )
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertIn("managed exec target", result.stderr)
                self.assertIn("symlink", result.stderr)
                self.assertFalse(
                    marker.exists(), "NODE must never have run against a symlinked CLI"
                )
            finally:
                outside.unlink()

    def test_launch_guard_reasserts_lock_identity_after_flock(self) -> None:
        # Regression for independent review round 13/14, 2026-08-18, P2:
        # flock(2) binds exclusivity to the OPEN FILE DESCRIPTION -- and
        # therefore to the inode held open at acquire time -- not to the
        # path. The generated launch guard used to acquire its shared
        # flock() and then trust LOCK's path forever after, never
        # re-checking that the path still names the SAME inode once the
        # lock was actually held; a same-UID actor who renamed a brand-new
        # file over LOCK's path in the narrow window between the pre-flock
        # identity check and flock() actually taking effect would have gone
        # completely undetected, exactly the class of race the installer
        # side's own assert_lifecycle_lock_path_identity() already guards
        # against for its own callers. This directly proves the generated
        # script's own after-flock re-check fires, by exec'ing the real
        # generated source into a namespace (same technique as
        # test_is_help_command_request_uses_exact_match_only) and calling
        # its real main() with the SECOND os.lstat(LOCK) call -- the new
        # post-flock re-assertion this round adds -- intercepted to report
        # a different inode than the one this process's own fd is bound to,
        # while the FIRST (pre-existing, pre-flock) os.lstat(LOCK) call
        # still sees the real, untampered lock so this test isolates
        # exactly the new check, not the old one.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            tool_root.mkdir(mode=0o700)
            lock = tool_root / "lifecycle.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
            node = root / "node"
            node.write_text("#!/bin/sh\necho should-not-run\n", encoding="utf-8")
            os.chmod(node, 0o700)
            cli = root / "cli.js"
            cli.write_text("// cli\n", encoding="utf-8")
            os.chmod(cli, 0o600)

            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
            ):
                source = installer.managed_launch_guard_script(
                    node, cli, installer.sha256_file(node), installer.sha256_file(cli)
                ).decode("utf-8")
            namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
            exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102

            real_lstat = os.lstat
            real_flock = fcntl.flock
            real_lock_identity = real_lstat(lock)

            class FakeStatResult:
                st_dev = real_lock_identity.st_dev
                st_ino = real_lock_identity.st_ino + 1  # deliberately different

            # Gate the fake identity on fcntl.flock() having actually been
            # called (and returned) already, rather than on a raw call
            # count: os.path.realpath()'s own internal implementation calls
            # os.lstat() on LOCK too (to resolve any symlink components)
            # BEFORE main()'s own explicit `before = os.lstat(LOCK)` line, so
            # counting raw invocations would race against an implementation
            # detail of realpath() itself. Tying the swap to "after flock()
            # was actually called" instead ties it to the real program
            # point this test targets -- the FIRST os.lstat(LOCK) call after
            # flock() returns is main()'s own new post-flock re-assertion,
            # and nothing else in main() calls os.lstat(LOCK) after flock()
            # returns.
            flock_called = {"done": False}

            def fake_flock(fd, operation):
                result = real_flock(fd, operation)
                flock_called["done"] = True
                return result

            def fake_lstat(path, *args, **kwargs):
                if flock_called["done"] and os.fspath(path) == os.fspath(lock):
                    # Simulate a same-UID racer having swapped the file in
                    # the window right after flock() took effect.
                    return FakeStatResult()
                return real_lstat(path, *args, **kwargs)

            buffer = io.StringIO()
            with (
                mock.patch("os.lstat", side_effect=fake_lstat),
                mock.patch("fcntl.flock", side_effect=fake_flock),
                mock.patch.object(sys, "argv", ["prime-agent-launch-guard.py"]),
                contextlib.redirect_stderr(buffer),
            ):
                returncode = namespace["main"]()

            self.assertTrue(flock_called["done"])
            self.assertEqual(returncode, 78)
            self.assertIn("identity changed while held", buffer.getvalue())
            # NODE must never have been reached -- the re-assertion must
            # fire strictly before subprocess.run([NODE, CLI, ...]).
            self.assertNotIn("should-not-run", buffer.getvalue())

    # Round 40, 2026-08-20 (independent Claude opus/max round-39 review,
    # P1-2): the three tests below all use a REAL Node binary (located on
    # PATH -- `shutil.which("node")` -- rather than the pinned tarball this
    # installer downloads, since these tests target Node's own documented
    # CLI/module-resolution BEHAVIOR, which is stable core Node.js
    # semantics, not something specific to the exact pinned patch version;
    # skipped entirely, rather than mocked around, when no real Node is
    # available at all) instead of mocked assertions, per this file's own
    # established convention that a finding this specific to real Node's
    # actual behavior needs a real Node to prove -- see e.g.
    # test_install_locked_detects_undeclared_package_in_subdirectory_anchored_nested_node_modules's
    # own "Verified with real Node" precedent above.
    def _require_real_node(self) -> str:
        node = shutil.which("node")
        if node is None:
            self.skipTest("no real Node binary available on PATH")
        return node

    def _require_real_openssl_cli(self) -> str:
        openssl = shutil.which("openssl")
        if openssl is None:
            self.skipTest("no real openssl CLI available on PATH")
        return openssl

    def _generate_self_signed_ca_and_leaf(
        self, openssl: str, directory: Path
    ) -> tuple[Path, Path, Path]:
        # Round 43, P1-A: real CA + leaf certificate pair (via the real
        # system `openssl` CLI, not a canned/committed fixture) used by
        # the NODE_TLS_REJECT_UNAUTHORIZED and NODE_EXTRA_CA_CERTS
        # regression tests below. Returns (ca_cert, leaf_cert, leaf_key).
        ca_key = directory / "ca-key.pem"
        ca_cert = directory / "ca-cert.pem"
        leaf_key = directory / "leaf-key.pem"
        leaf_csr = directory / "leaf.csr"
        leaf_cert = directory / "leaf-cert.pem"
        ext_file = directory / "leaf-ext.cnf"
        ext_file.write_text(
            "subjectAltName = DNS:localhost,IP:127.0.0.1\n", encoding="utf-8"
        )
        for argv in (
            [
                openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", os.fspath(ca_key), "-out", os.fspath(ca_cert),
                "-days", "2", "-subj", "/CN=Orca Round43 Test CA",
            ],
            [
                openssl, "req", "-newkey", "rsa:2048", "-nodes",
                "-keyout", os.fspath(leaf_key), "-out", os.fspath(leaf_csr),
                "-subj", "/CN=localhost",
            ],
            [
                openssl, "x509", "-req", "-in", os.fspath(leaf_csr),
                "-CA", os.fspath(ca_cert), "-CAkey", os.fspath(ca_key),
                "-CAcreateserial", "-out", os.fspath(leaf_cert),
                "-days", "2", "-extfile", os.fspath(ext_file),
            ],
        ):
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=30, check=False
            )
            self.assertEqual(
                result.returncode, 0, f"openssl setup failed: {result.stderr}"
            )
        return ca_cert, leaf_cert, leaf_key

    def test_scrubbed_node_environment_blocks_real_node_options_injection(
        self,
    ) -> None:
        # Round 40, P1-2 item 1. Grounds BOTH halves in real Node: first
        # confirms real Node actually honors an inherited NODE_OPTIONS
        # (the real vulnerability -- pre-round-40, `subprocess.run([NODE,
        # CLI, ...])` in the generated launch guard's main() passed no
        # `env=` at all, so this was inherited completely unscrubbed), then
        # confirms scrubbed_node_environment() -- the exact function
        # main() now calls -- removes it while leaving an unrelated
        # variable untouched, and that real Node run with THAT environment
        # no longer honors the injection.
        node = self._require_real_node()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cli = root / "cli.js"
            cli.write_text("console.log('CLI_RAN');\n", encoding="utf-8")
            injected = root / "injected.js"
            injected.write_text(
                "console.log('INJECTED_VIA_NODE_OPTIONS');\n", encoding="utf-8"
            )
            source = installer.managed_launch_guard_script(
                Path(node), cli, "a" * 64, "b" * 64
            ).decode("utf-8")
            namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
            exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102
            scrub = namespace["scrubbed_node_environment"]

            poisoned_env = dict(os.environ)
            poisoned_env["NODE_OPTIONS"] = f"--require={injected}"
            # Round 43, P1-A: round 40's scrubbed_node_environment() was a
            # denylist, so an arbitrary unlisted variable name like
            # "KEEP_ME" survived untouched (that was the whole point of a
            # denylist). Now that it is an allowlist, an arbitrary,
            # unreviewed name is exactly what MUST be dropped -- so this
            # is now the negative control, not the positive one.
            poisoned_env["KEEP_ME"] = "arbitrary-unreviewed-name"
            # Positive control: a genuinely necessary, reviewed name
            # (terminal detection -- see NODE_ENV_ALLOWED_NAMES) must
            # still survive.
            poisoned_env["TERM"] = "xterm-round43-keep-me"

            # Ground truth: real Node, run with this environment exactly as
            # inherited (no scrubbing at all), DOES execute the injected
            # module -- proving this is a real vulnerability, not a
            # theoretical one.
            unscrubbed_result = subprocess.run(
                [node, os.fspath(cli)],
                env=poisoned_env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertIn("INJECTED_VIA_NODE_OPTIONS", unscrubbed_result.stdout)

            with mock.patch.dict(os.environ, poisoned_env, clear=True):
                scrubbed_env = scrub()
            self.assertNotIn("NODE_OPTIONS", scrubbed_env)
            self.assertNotIn("NODE_PATH", scrubbed_env)
            self.assertNotIn(
                "KEEP_ME",
                scrubbed_env,
                "an arbitrary, unreviewed variable name must NOT survive "
                "an allowlist -- surviving here would mean this is still "
                "effectively a denylist",
            )
            self.assertEqual(scrubbed_env.get("TERM"), "xterm-round43-keep-me")

            scrubbed_result = subprocess.run(
                [node, os.fspath(cli)],
                env=scrubbed_env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertNotIn(
                "INJECTED_VIA_NODE_OPTIONS", scrubbed_result.stdout
            )
            self.assertIn("CLI_RAN", scrubbed_result.stdout)

    def test_scrubbed_node_environment_blocks_real_node_tls_reject_unauthorized(
        self,
    ) -> None:
        # Round 43, P1-A. Real, end-to-end reproduction of one of the two
        # concrete findings round 42 added to round 40's NODE_OPTIONS/
        # NODE_PATH-only denylist: a real self-signed TLS leaf certificate
        # (via the real system `openssl` CLI, generated fresh -- not a
        # canned fixture), a real loopback HTTPS/TLS server (real pinned-
        # Node-version binary), and a real `tls.connect()` client. First
        # proves the ground truth (an inherited NODE_TLS_REJECT_UNAUTHORIZED
        # =0 really does make real Node accept a certificate it would
        # otherwise refuse), then proves scrubbed_node_environment() closes
        # it -- the same env dict, scrubbed, no longer honors the override.
        node = self._require_real_node()
        openssl = self._require_real_openssl_cli()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _ca_cert, leaf_cert, leaf_key = self._generate_self_signed_ca_and_leaf(
                openssl, root
            )
            server_js = root / "server.js"
            server_js.write_text(
                "const https = require('https');\n"
                "const fs = require('fs');\n"
                "const server = https.createServer({\n"
                f"  cert: fs.readFileSync({os.fspath(leaf_cert)!r}),\n"
                f"  key: fs.readFileSync({os.fspath(leaf_key)!r}),\n"
                "}, (req, res) => { res.end('OK'); });\n"
                "server.listen(0, '127.0.0.1', () => {\n"
                "  process.stdout.write('LISTENING:' + server.address().port + '\\n');\n"
                "});\n",
                encoding="utf-8",
            )
            client_js = root / "client.js"
            client_js.write_text(
                "const tls = require('tls');\n"
                "const port = Number(process.argv[2]);\n"
                "const socket = tls.connect({ host: '127.0.0.1', port, servername: 'localhost' }, () => {\n"
                "  console.log('CLIENT_OK:authorized=' + socket.authorized);\n"
                "  socket.end();\n"
                "});\n"
                "socket.on('error', (e) => console.log('CLIENT_ERR:' + e.code));\n"
                "setTimeout(() => { console.log('CLIENT_TIMEOUT'); process.exit(1); }, 4000).unref();\n",
                encoding="utf-8",
            )
            source = installer.managed_launch_guard_script(
                Path(node), client_js, "a" * 64, "b" * 64
            ).decode("utf-8")
            namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
            exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102
            scrub = namespace["scrubbed_node_environment"]

            def run_client(env: dict[str, str]) -> str:
                server = subprocess.Popen(
                    [node, os.fspath(server_js)],
                    env=dict(os.environ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                try:
                    listening_line = server.stdout.readline()
                    self.assertTrue(
                        listening_line.startswith("LISTENING:"), listening_line
                    )
                    port = listening_line.strip().split(":", 1)[1]
                    client = subprocess.run(
                        [node, os.fspath(client_js), port],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    return client.stdout
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                    if server.stdout is not None:
                        server.stdout.close()

            # Baseline: an unrelated environment (no override) -- real
            # Node correctly REFUSES the self-signed leaf.
            baseline_stdout = run_client(dict(os.environ))
            self.assertIn("CLIENT_ERR:", baseline_stdout)
            self.assertNotIn("CLIENT_OK", baseline_stdout)

            poisoned_env = dict(os.environ)
            poisoned_env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"

            # Ground truth: real Node, given this poisoned env exactly as
            # inherited, DOES accept the untrusted certificate.
            unscrubbed_stdout = run_client(poisoned_env)
            self.assertIn("CLIENT_OK:authorized=false", unscrubbed_stdout)

            with mock.patch.dict(os.environ, poisoned_env, clear=True):
                scrubbed_env = scrub()
            self.assertNotIn("NODE_TLS_REJECT_UNAUTHORIZED", scrubbed_env)

            # Fix confirmed: real Node, run with the scrubbed environment
            # (built from the SAME poisoned input), refuses again.
            scrubbed_stdout = run_client(scrubbed_env)
            self.assertIn("CLIENT_ERR:", scrubbed_stdout)
            self.assertNotIn("CLIENT_OK", scrubbed_stdout)

    def test_scrubbed_node_environment_blocks_real_node_extra_ca_certs(
        self,
    ) -> None:
        # Round 43, P1-A. Real, end-to-end reproduction of the second
        # concrete finding: a real CA cert + a leaf cert it signs (both
        # via the real system `openssl` CLI), and NODE_EXTRA_CA_CERTS
        # pointed at the CA -- real Node genuinely trusts the leaf when
        # this is inherited; scrubbed_node_environment() must remove it.
        node = self._require_real_node()
        openssl = self._require_real_openssl_cli()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ca_cert, leaf_cert, leaf_key = self._generate_self_signed_ca_and_leaf(
                openssl, root
            )
            server_js = root / "server.js"
            server_js.write_text(
                "const https = require('https');\n"
                "const fs = require('fs');\n"
                "const server = https.createServer({\n"
                f"  cert: fs.readFileSync({os.fspath(leaf_cert)!r}),\n"
                f"  key: fs.readFileSync({os.fspath(leaf_key)!r}),\n"
                "}, (req, res) => { res.end('OK'); });\n"
                "server.listen(0, '127.0.0.1', () => {\n"
                "  process.stdout.write('LISTENING:' + server.address().port + '\\n');\n"
                "});\n",
                encoding="utf-8",
            )
            client_js = root / "client.js"
            client_js.write_text(
                "const tls = require('tls');\n"
                "const port = Number(process.argv[2]);\n"
                "const socket = tls.connect({ host: '127.0.0.1', port, servername: 'localhost' }, () => {\n"
                "  console.log('CLIENT_OK:authorized=' + socket.authorized);\n"
                "  socket.end();\n"
                "});\n"
                "socket.on('error', (e) => console.log('CLIENT_ERR:' + e.code));\n"
                "setTimeout(() => { console.log('CLIENT_TIMEOUT'); process.exit(1); }, 4000).unref();\n",
                encoding="utf-8",
            )
            source = installer.managed_launch_guard_script(
                Path(node), client_js, "a" * 64, "b" * 64
            ).decode("utf-8")
            namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
            exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102
            scrub = namespace["scrubbed_node_environment"]

            def run_client(env: dict[str, str]) -> str:
                server = subprocess.Popen(
                    [node, os.fspath(server_js)],
                    env=dict(os.environ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                try:
                    listening_line = server.stdout.readline()
                    self.assertTrue(
                        listening_line.startswith("LISTENING:"), listening_line
                    )
                    port = listening_line.strip().split(":", 1)[1]
                    client = subprocess.run(
                        [node, os.fspath(client_js), port],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    return client.stdout
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                    if server.stdout is not None:
                        server.stdout.close()

            poisoned_env = dict(os.environ)
            poisoned_env["NODE_EXTRA_CA_CERTS"] = os.fspath(ca_cert)

            # Ground truth: real Node, given this poisoned env exactly as
            # inherited, trusts the leaf via the injected CA.
            unscrubbed_stdout = run_client(poisoned_env)
            self.assertIn("CLIENT_OK:authorized=true", unscrubbed_stdout)

            with mock.patch.dict(os.environ, poisoned_env, clear=True):
                scrubbed_env = scrub()
            self.assertNotIn("NODE_EXTRA_CA_CERTS", scrubbed_env)

            # Fix confirmed: real Node, run with the scrubbed environment,
            # no longer trusts the leaf.
            scrubbed_stdout = run_client(scrubbed_env)
            self.assertIn("CLIENT_ERR:", scrubbed_stdout)
            self.assertNotIn("CLIENT_OK", scrubbed_stdout)

    def test_scrubbed_node_environment_blocks_real_node_compile_cache_write(
        self,
    ) -> None:
        # Round 43, P1-A. NODE_COMPILE_CACHE (https://nodejs.org/api/
        # module.html#module-compile-cache) was only ever indirectly
        # blocked, pre-round-43, via this installer's own separate
        # NODE_DISABLE_COMPILE_CACHE=1 export -- it was never itself in
        # round 40's five-key denylist. Real, observable reproduction:
        # setting it to an attacker-chosen directory really does cause
        # real Node to write real V8 compile-cache files there.
        node = self._require_real_node()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cli = root / "cli.js"
            cli.write_text("console.log('CLI_RAN');\n", encoding="utf-8")
            source = installer.managed_launch_guard_script(
                Path(node), cli, "a" * 64, "b" * 64
            ).decode("utf-8")
            namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
            exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102
            scrub = namespace["scrubbed_node_environment"]

            attacker_cache_dir = root / "attacker-observed-cache"
            attacker_cache_dir.mkdir()
            poisoned_env = dict(os.environ)
            poisoned_env["NODE_COMPILE_CACHE"] = os.fspath(attacker_cache_dir)

            # Ground truth: real Node, given this poisoned env exactly as
            # inherited, DOES write cache files into the attacker's
            # chosen directory.
            unscrubbed_result = subprocess.run(
                [node, os.fspath(cli)],
                env=poisoned_env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertIn("CLI_RAN", unscrubbed_result.stdout)
            self.assertNotEqual(
                list(attacker_cache_dir.iterdir()),
                [],
                "real Node did not write to NODE_COMPILE_CACHE -- test "
                "assumption invalid for this Node build/version",
            )

            for entry in attacker_cache_dir.rglob("*"):
                if entry.is_file():
                    entry.unlink()
            for entry in sorted(
                attacker_cache_dir.rglob("*"), key=lambda p: -len(p.parts)
            ):
                if entry.is_dir():
                    entry.rmdir()
            self.assertEqual(list(attacker_cache_dir.iterdir()), [])

            with mock.patch.dict(os.environ, poisoned_env, clear=True):
                scrubbed_env = scrub()
            self.assertNotIn("NODE_COMPILE_CACHE", scrubbed_env)

            # Fix confirmed: real Node, run with the scrubbed environment
            # (built from the SAME poisoned input), writes nothing there.
            scrubbed_result = subprocess.run(
                [node, os.fspath(cli)],
                env=scrubbed_env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertIn("CLI_RAN", scrubbed_result.stdout)
            self.assertEqual(list(attacker_cache_dir.iterdir()), [])

    def test_scrubbed_node_environment_excludes_openssl_conf_and_native_loading_vars(
        self,
    ) -> None:
        # Round 43, P1-A. OPENSSL_CONF is Node's own documented
        # environment variable for exactly the "influences code loading"
        # category round 42 flagged (https://nodejs.org/api/cli.html --
        # OpenSSL configuration file). This round built a REAL, compiled,
        # harmless marker .dylib and a real OpenSSL 3.x `[provider_sect]`
        # config activating it, and confirmed the underlying MECHANISM is
        # real: the real system `openssl` CLI (a dynamically-linked
        # OpenSSL 3.6.x build) genuinely dlopen()'d it and ran its
        # constructor. Against this installer's exact pinned Node binary
        # specifically, though, that same config/exec chain did not
        # reproduce in real testing (this Node build's statically-linked,
        # non-FIPS OpenSSL did not visibly consult OPENSSL_CONF for
        # provider auto-activation for a plain script or for
        # `--openssl-config=<file>`) -- Node's own docs hedge this
        # variable's effect as being "among other uses... to enable
        # FIPS-compliant crypto if Node.js is built with ./configure
        # --openssl-fips", consistent with what was observed
        # (process.config.variables.openssl_is_fips is false here). This
        # is exactly the "safer/simpler proxy" this round's dispatch
        # explicitly allows for a case where a full live-dlopen
        # reproduction is impractical against the actual pinned binary --
        # so this test proves the specific env var is genuinely stripped
        # by the real generated scrubbed_node_environment() (not merely
        # that some function returns an expected dict shape), while being
        # honest that end-to-end exploitation was not reproducible
        # against this exact pinned Node build.
        node = self._require_real_node()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cli = root / "cli.js"
            cli.write_text("console.log('CLI_RAN');\n", encoding="utf-8")
            source = installer.managed_launch_guard_script(
                Path(node), cli, "a" * 64, "b" * 64
            ).decode("utf-8")
            namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
            exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102
            scrub = namespace["scrubbed_node_environment"]

            poisoned_env = dict(os.environ)
            poisoned_env["OPENSSL_CONF"] = os.fspath(root / "attacker.cnf")
            poisoned_env["OPENSSL_MODULES"] = os.fspath(root / "attacker-modules")
            poisoned_env["OPENSSL_ENGINES"] = os.fspath(root / "attacker-engines")
            poisoned_env["SSL_CERT_FILE"] = os.fspath(root / "attacker-ca.pem")
            poisoned_env["SSL_CERT_DIR"] = os.fspath(root / "attacker-ca-dir")
            poisoned_env["NODE_ICU_DATA"] = os.fspath(root / "attacker-icu")
            poisoned_env["NODE_REDIRECT_WARNINGS"] = os.fspath(
                root / "attacker-warnings.log"
            )
            poisoned_env["NODE_V8_COVERAGE"] = os.fspath(root / "attacker-coverage")
            poisoned_env["NODE_REPL_HISTORY"] = os.fspath(
                root / "attacker-repl-history"
            )
            poisoned_env["NAPI_RS_NATIVE_LIBRARY_PATH"] = os.fspath(
                root / "attacker-native.so"
            )

            with mock.patch.dict(os.environ, poisoned_env, clear=True):
                scrubbed_env = scrub()
            for excluded in (
                "OPENSSL_CONF", "OPENSSL_MODULES", "OPENSSL_ENGINES",
                "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_ICU_DATA",
                "NODE_REDIRECT_WARNINGS", "NODE_V8_COVERAGE",
                "NODE_REPL_HISTORY", "NAPI_RS_NATIVE_LIBRARY_PATH",
            ):
                self.assertNotIn(excluded, scrubbed_env)

            # Sanity: the CLI itself still runs fine with the scrubbed
            # environment (none of these were ever legitimately needed).
            result = subprocess.run(
                [node, os.fspath(cli)],
                env=scrubbed_env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertIn("CLI_RAN", result.stdout)

    def test_scrubbed_node_environment_preserves_necessary_session_and_credential_variables(
        self,
    ) -> None:
        # Round 43, P1-A positive control: converting to an allowlist
        # must not silently drop variables this installer's own generated
        # wrapper sets (README.md "For session starts it also:") or that
        # the real, materialized upstream bundle genuinely reads to
        # function as a multi-provider AI coding agent (confirmed by
        # inspecting the real extracted v0.7.2 release tree this round).
        node = self._require_real_node()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cli = root / "cli.js"
            cli.write_text("console.log('CLI_RAN');\n", encoding="utf-8")
            source = installer.managed_launch_guard_script(
                Path(node), cli, "a" * 64, "b" * 64
            ).decode("utf-8")
            namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
            exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102
            scrub = namespace["scrubbed_node_environment"]

            necessary = {
                # This installer's own generated env (managed_entrypoint_
                # script()'s exports).
                "PRIME_AGENT_CODING_AGENT_DIR": "/managed/state/agent",
                "PRIME_AGENT_LAUNCHER_PATH": "/managed/bin/prime-agent",
                "PRIME_AGENT_SESSION_DIR": "/managed/sessions",
                "PRIME_AGENT_CODING_AGENT_SESSION_DIR": "/managed/sessions",
                "NODE_DISABLE_COMPILE_CACHE": "1",
                "DO_NOT_TRACK": "1",
                "PI_SKIP_VERSION_CHECK": "1",
                "PRIME_AGENT_TELEMETRY": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
                "ORCA_PRIME_AGENT_RESOURCE_GUARD": "1",
                # Session basics.
                "HOME": "/Users/example",
                "PATH": "/usr/bin:/bin",
                "TERM": "xterm-256color",
                "LANG": "en_US.UTF-8",
                "LC_ALL": "C",
                "TMPDIR": "/tmp/example",
                # Real upstream-bundle-read provider credentials.
                "ANTHROPIC_API_KEY": "sk-ant-example",
                "OPENAI_API_KEY": "sk-openai-example",
                "AWS_ACCESS_KEY_ID": "AKIA-example",
                "GOOGLE_APPLICATION_CREDENTIALS": "/creds.json",
            }
            poisoned_env = dict(os.environ)
            poisoned_env.update(necessary)
            with mock.patch.dict(os.environ, poisoned_env, clear=True):
                scrubbed_env = scrub()
            for key, value in necessary.items():
                self.assertEqual(
                    scrubbed_env.get(key),
                    value,
                    f"{key} was dropped by the allowlist but is genuinely needed",
                )

            # And the tool still functions with this exact scrubbed
            # environment (the empirical acceptance bar the round-43
            # dispatch specifically asked for).
            result = subprocess.run(
                [node, os.fspath(cli)],
                env=scrubbed_env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CLI_RAN", result.stdout)

    def test_unexpected_ancestor_node_modules_detects_real_node_rce_path(
        self,
    ) -> None:
        # Round 40, P1-2 item 4. Reproduces the reviewer's exact finding
        # with real Node: a package planted at an ancestor node_modules
        # location OUTSIDE RELEASE_DIR (two levels above it here, modeling
        # TOOL_ROOT/node_modules) is loaded and executed by real Node's own
        # module resolution when a bare specifier isn't found inside
        # RELEASE_DIR -- proving the resolution chain genuinely reaches
        # there before this test ever asserts anything about this
        # installer's own mitigation. Then confirms
        # unexpected_ancestor_node_modules() -- the exact function the
        # generated launch guard's main() now calls before ever exec'ing
        # NODE -- detects that identical layout.
        node = self._require_real_node()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "tool-root/releases/v0.7.2"
            bundle_dir = release / "lib/node_modules/prime-agent/dist/bundle"
            bundle_dir.mkdir(parents=True)
            cli = bundle_dir / "cli.js"
            cli.write_text(
                "try { require('bufferutil'); } "
                "catch (e) { console.log('NOT_LOADED:' + e.code); }\n",
                encoding="utf-8",
            )
            # Sanity: nothing planted yet -- real Node fails to resolve the
            # bare specifier cleanly (MODULE_NOT_FOUND), proving the CLI
            # script itself is not somehow satisfying the require() some
            # other way.
            clean_result = subprocess.run(
                [node, os.fspath(cli)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertIn("NOT_LOADED:MODULE_NOT_FOUND", clean_result.stdout)

            # Plant the malicious package two levels above RELEASE_DIR
            # (TOOL_ROOT/node_modules -- release.parent.parent/"node_modules"
            # -- release.parent is TOOL_ROOT/releases).
            ancestor_node_modules = release.parent.parent / "node_modules"
            ancestor_pkg = ancestor_node_modules / "bufferutil"
            ancestor_pkg.mkdir(parents=True)
            (ancestor_pkg / "package.json").write_text(
                '{"name": "bufferutil", "main": "index.js"}\n', encoding="utf-8"
            )
            (ancestor_pkg / "index.js").write_text(
                "console.log('EXPLOIT_EXECUTED_VIA_ANCESTOR_NODE_MODULES');\n",
                encoding="utf-8",
            )

            # Ground truth: real Node, invoked directly (modeling this
            # installer's own pre-round-40 subprocess.run() call shape --
            # no ancestor guard, whatever RELEASE_DIR-external content
            # exists is exactly as reachable to Node as anything inside
            # it), DOES load and execute the planted ancestor package.
            exploited_result = subprocess.run(
                [node, os.fspath(cli)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertIn(
                "EXPLOIT_EXECUTED_VIA_ANCESTOR_NODE_MODULES",
                exploited_result.stdout,
            )

            # Now confirm the real generated launch guard's own check --
            # unexpected_ancestor_node_modules(), called from main() before
            # NODE is ever exec'd -- detects this identical layout.
            with mock.patch.object(installer, "RELEASE_DIR", release):
                source = installer.managed_launch_guard_script(
                    Path(node), cli, "a" * 64, "b" * 64
                ).decode("utf-8")
            namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
            exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102
            detected = namespace["unexpected_ancestor_node_modules"]()
            self.assertEqual(detected, os.fspath(ancestor_node_modules))

    def test_launch_guard_main_refuses_before_real_exec_when_ancestor_node_modules_present(
        self,
    ) -> None:
        # Round 40, P1-2 items 1 and 4, wired-in end to end: runs the
        # REAL, complete generated launch guard main() (same in-process
        # exec-into-namespace technique as
        # test_main_refuses_when_lock_identity_changes_after_flock above),
        # with a REAL Node binary as NODE and a REAL cli.js as CLI, through
        # BOTH scenarios -- proving the new checks are actually wired into
        # main()'s real control flow, not merely correct as standalone
        # functions (the two tests above), AND that a genuinely clean tree
        # is not falsely blocked.
        node = self._require_real_node()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            bundle_dir = release / "lib/node_modules/prime-agent/dist/bundle"
            bundle_dir.mkdir(parents=True, mode=0o700)
            marker = root / "cli-ran.marker"
            cli = bundle_dir / "cli.js"
            cli.write_text(
                "require('fs').writeFileSync(" + repr(os.fspath(marker)) + ", 'ran');\n",
                encoding="utf-8",
            )
            os.chmod(cli, 0o600)
            lock = tool_root / "lifecycle.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
            for directory_path in (root, tool_root, release.parent, release):
                os.chmod(directory_path, 0o700)
            # NODE must itself pass validate_exec_target()'s own SSD_ROOT-
            # containment/private-ownership/content-digest checks -- copy
            # the real Node binary inside this sandboxed root so it is
            # both a private, current-uid-owned regular file AND inside
            # the mocked SSD_ROOT this test uses, then pin its digest to
            # this exact copy.
            node_copy = root / "node"
            shutil.copy(Path(node).resolve(), node_copy)
            os.chmod(node_copy, 0o700)

            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
            ):
                source = installer.managed_launch_guard_script(
                    node_copy,
                    cli,
                    installer.sha256_file(node_copy),
                    installer.sha256_file(cli),
                ).decode("utf-8")
            namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
            exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102

            def run_main() -> tuple[int, str]:
                buffer = io.StringIO()
                with (
                    mock.patch.object(sys, "argv", ["prime-agent-launch-guard.py"]),
                    contextlib.redirect_stderr(buffer),
                ):
                    returncode = namespace["main"]()
                return returncode, buffer.getvalue()

            # Sanity: clean tree -- real Node genuinely runs CLI end to
            # end, through main()'s own real subprocess.run() call
            # (including its new env=scrubbed_node_environment()
            # argument), and the marker file it writes actually appears.
            returncode, stderr_output = run_main()
            self.assertEqual(returncode, 0, stderr_output)
            self.assertTrue(marker.exists())
            marker.unlink()

            # Plant an ancestor node_modules directory (TOOL_ROOT/
            # node_modules -- one level above RELEASE_DIR/.. i.e.
            # release.parent.parent) -- main() must refuse BEFORE Node
            # ever runs; the marker file must never appear.
            (tool_root / "node_modules").mkdir(mode=0o700)
            returncode, stderr_output = run_main()
            self.assertNotEqual(returncode, 0)
            # Round 43: main()'s fail() message was generalized (the same
            # check now also covers Module.globalPaths locations that are
            # not literally named "node_modules" -- see the round-43 test
            # class below) -- this is the exact new wording.
            self.assertIn(
                "unexpected item on Node's own module resolution path",
                stderr_output,
            )
            self.assertFalse(marker.exists())

    # Round 43, 2026-08-20 (independent Claude opus/max round-42 review,
    # P1-B): the tests below extend round 40's ancestor-only
    # unexpected_ancestor_node_modules()/assert_no_unexpected_ancestor_
    # node_modules() coverage to real Node's Module.globalPaths locations
    # ($HOME/.node_modules, $HOME/.node_libraries, and
    # <release>/toolchain/lib/node). All real-HOME-dependent tests use a
    # temporary directory as HOME (never the real user's home directory).

    def test_node_global_folder_paths_matches_real_node_globalpaths(
        self,
    ) -> None:
        # Ground-truths BOTH independent implementations (the generated
        # launch guard's node_global_folder_paths() and the real
        # installer module's own node_global_folder_paths(release_dir))
        # against real Node's own require('module').globalPaths output,
        # for both a HOME-set and a HOME-unset scenario.
        node = self._require_real_node()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "tool/releases/v0.7.2"
            toolchain_bin = release / "toolchain/bin"
            toolchain_bin.mkdir(parents=True)
            node_copy = toolchain_bin / "node"
            shutil.copy(Path(node).resolve(), node_copy)
            os.chmod(node_copy, 0o700)
            fake_home = root / "fake-home"
            fake_home.mkdir()

            def real_global_paths(env: dict[str, str]) -> list[str]:
                result = subprocess.run(
                    [
                        os.fspath(node_copy), "-e",
                        "console.log(JSON.stringify(require('module').globalPaths))",
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            for env_home in (None, os.fspath(fake_home)):
                env = {"PATH": os.environ.get("PATH", "")}
                if env_home is not None:
                    env["HOME"] = env_home
                real_paths = real_global_paths(env)

                # Generated launch-guard side (module-level NODE global).
                source = installer.managed_launch_guard_script(
                    node_copy, root / "cli.js", "a" * 64, "b" * 64
                ).decode("utf-8")
                namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
                exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102
                with mock.patch.dict(os.environ, env, clear=True):
                    generated_paths = namespace["node_global_folder_paths"]()
                self.assertEqual(
                    sorted(generated_paths), sorted(real_paths),
                    f"generated-side mismatch for HOME={env_home!r}",
                )

                # Real installer module side (verify()'s own copy).
                with mock.patch.dict(os.environ, env, clear=True):
                    installer_paths = installer.node_global_folder_paths(release)
                self.assertEqual(
                    sorted(installer_paths), sorted(real_paths),
                    f"installer-module-side mismatch for HOME={env_home!r}",
                )

    def test_unexpected_ancestor_node_modules_detects_real_home_global_folders(
        self,
    ) -> None:
        # Real reproduction of the reviewer's exact $HOME/.node_modules
        # scenario (using a temp HOME), plus its sibling
        # $HOME/.node_libraries: real Node genuinely loads and executes a
        # package planted there, and the generated launch guard's
        # unexpected_ancestor_node_modules() -- extended this round --
        # detects both.
        node = self._require_real_node()
        for global_folder_name, package_name in (
            (".node_modules", "orca-marker-package"),
            (".node_libraries", "orca-marker-package"),
        ):
            with self.subTest(global_folder_name=global_folder_name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    release = root / "tool-root/releases/v0.7.2"
                    bundle_dir = release / "lib/node_modules/prime-agent/dist/bundle"
                    bundle_dir.mkdir(parents=True)
                    cli = bundle_dir / "cli.js"
                    cli.write_text(
                        f"try {{ require({package_name!r}); }} "
                        "catch (e) { console.log('NOT_LOADED:' + e.code); }\n",
                        encoding="utf-8",
                    )
                    fake_home = root / "fake-home"
                    fake_home.mkdir()

                    # Sanity: nothing planted yet.
                    clean_result = subprocess.run(
                        [node, os.fspath(cli)],
                        env={"HOME": os.fspath(fake_home), "PATH": os.environ.get("PATH", "")},
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    self.assertIn("NOT_LOADED:MODULE_NOT_FOUND", clean_result.stdout)

                    global_folder = fake_home / global_folder_name
                    package_dir = global_folder / package_name
                    package_dir.mkdir(parents=True)
                    (package_dir / "package.json").write_text(
                        json.dumps({"name": package_name, "main": "index.js"}),
                        encoding="utf-8",
                    )
                    (package_dir / "index.js").write_text(
                        "console.log('EXPLOIT_EXECUTED_VIA_' + "
                        + repr(global_folder_name.upper().lstrip("."))
                        + ");\n",
                        encoding="utf-8",
                    )

                    # Ground truth: real Node, invoked directly (no
                    # guard), DOES load and execute the planted package.
                    exploited_result = subprocess.run(
                        [node, os.fspath(cli)],
                        env={"HOME": os.fspath(fake_home), "PATH": os.environ.get("PATH", "")},
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    self.assertIn(
                        "EXPLOIT_EXECUTED_VIA_", exploited_result.stdout
                    )

                    # Confirm the generated launch guard's own check
                    # detects this identical layout.
                    with mock.patch.object(installer, "RELEASE_DIR", release):
                        source = installer.managed_launch_guard_script(
                            Path(node), cli, "a" * 64, "b" * 64
                        ).decode("utf-8")
                    namespace: dict[str, object] = {
                        "__name__": "generated_launch_guard"
                    }
                    exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102
                    with mock.patch.dict(
                        os.environ, {"HOME": os.fspath(fake_home)}, clear=True
                    ):
                        detected = namespace["unexpected_ancestor_node_modules"]()
                    self.assertEqual(detected, os.fspath(global_folder))

    def test_unexpected_ancestor_node_modules_detects_real_toolchain_lib_node_global_folder(
        self,
    ) -> None:
        # Real reproduction of the third Module.globalPaths location:
        # <node prefix>/lib/node, which on this installer's fixed
        # toolchain layout is RELEASE_DIR/toolchain/lib/node -- INSIDE
        # release_dir, not an ancestor, so round 40's ancestor walk alone
        # never reaches it.
        node = self._require_real_node()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "tool-root/releases/v0.7.2"
            toolchain_bin = release / "toolchain/bin"
            toolchain_bin.mkdir(parents=True)
            node_copy = toolchain_bin / "node"
            shutil.copy(Path(node).resolve(), node_copy)
            os.chmod(node_copy, 0o700)
            bundle_dir = release / "lib/node_modules/prime-agent/dist/bundle"
            bundle_dir.mkdir(parents=True)
            cli = bundle_dir / "cli.js"
            cli.write_text(
                "try { require('orca-marker-package'); } "
                "catch (e) { console.log('NOT_LOADED:' + e.code); }\n",
                encoding="utf-8",
            )
            fake_home = root / "fake-home"
            fake_home.mkdir()
            run_env = {"HOME": os.fspath(fake_home), "PATH": os.environ.get("PATH", "")}

            clean_result = subprocess.run(
                [os.fspath(node_copy), os.fspath(cli)],
                env=run_env, capture_output=True, text=True, timeout=15, check=False,
            )
            self.assertIn("NOT_LOADED:MODULE_NOT_FOUND", clean_result.stdout)

            global_folder = release / "toolchain/lib/node"
            package_dir = global_folder / "orca-marker-package"
            package_dir.mkdir(parents=True)
            (package_dir / "package.json").write_text(
                json.dumps({"name": "orca-marker-package", "main": "index.js"}),
                encoding="utf-8",
            )
            (package_dir / "index.js").write_text(
                "console.log('EXPLOIT_EXECUTED_VIA_TOOLCHAIN_LIB_NODE');\n",
                encoding="utf-8",
            )

            exploited_result = subprocess.run(
                [os.fspath(node_copy), os.fspath(cli)],
                env=run_env, capture_output=True, text=True, timeout=15, check=False,
            )
            self.assertIn(
                "EXPLOIT_EXECUTED_VIA_TOOLCHAIN_LIB_NODE", exploited_result.stdout
            )

            with mock.patch.object(installer, "RELEASE_DIR", release):
                source = installer.managed_launch_guard_script(
                    node_copy, cli, "a" * 64, "b" * 64
                ).decode("utf-8")
            namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
            exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102
            with mock.patch.dict(os.environ, run_env, clear=True):
                detected = namespace["unexpected_ancestor_node_modules"]()
            self.assertEqual(detected, os.fspath(global_folder))

    def test_launch_guard_main_refuses_before_real_exec_when_home_node_modules_global_folder_present(
        self,
    ) -> None:
        # Full main() wiring, end to end, for the new global-folder check
        # (mirrors test_launch_guard_main_refuses_before_real_exec_when_
        # ancestor_node_modules_present above, which covers the plain
        # ancestor walk) -- uses a temp HOME so real Node's own
        # Module.globalPaths genuinely includes it.
        node = self._require_real_node()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            bundle_dir = release / "lib/node_modules/prime-agent/dist/bundle"
            bundle_dir.mkdir(parents=True, mode=0o700)
            marker = root / "cli-ran.marker"
            cli = bundle_dir / "cli.js"
            cli.write_text(
                "require('fs').writeFileSync(" + repr(os.fspath(marker)) + ", 'ran');\n",
                encoding="utf-8",
            )
            os.chmod(cli, 0o600)
            lock = tool_root / "lifecycle.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
            for directory_path in (root, tool_root, release.parent, release):
                os.chmod(directory_path, 0o700)
            node_copy = root / "node"
            shutil.copy(Path(node).resolve(), node_copy)
            os.chmod(node_copy, 0o700)
            fake_home = root / "fake-home"
            fake_home.mkdir(mode=0o700)

            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
            ):
                source = installer.managed_launch_guard_script(
                    node_copy, cli,
                    installer.sha256_file(node_copy), installer.sha256_file(cli),
                ).decode("utf-8")
            namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
            exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102

            def run_main() -> tuple[int, str]:
                buffer = io.StringIO()
                with (
                    mock.patch.object(sys, "argv", ["prime-agent-launch-guard.py"]),
                    mock.patch.dict(
                        os.environ, {"HOME": os.fspath(fake_home)}, clear=True
                    ),
                    contextlib.redirect_stderr(buffer),
                ):
                    returncode = namespace["main"]()
                return returncode, buffer.getvalue()

            # Sanity: clean tree, real Node genuinely runs CLI end to end.
            returncode, stderr_output = run_main()
            self.assertEqual(returncode, 0, stderr_output)
            self.assertTrue(marker.exists())
            marker.unlink()

            # Plant a package at $HOME/.node_modules -- main() must
            # refuse BEFORE Node ever runs; the marker must never appear.
            (fake_home / ".node_modules").mkdir(mode=0o700)
            returncode, stderr_output = run_main()
            self.assertNotEqual(returncode, 0)
            self.assertIn(
                "unexpected item on Node's own module resolution path",
                stderr_output,
            )
            self.assertFalse(marker.exists())

    def test_assert_no_unexpected_ancestor_node_modules_catches_home_global_folders(
        self,
    ) -> None:
        # verify()-side counterpart -- pure Python logic, no real Node
        # needed here (the real-Node grounding is established by the
        # tests above); uses a temp HOME, never the real user's home.
        for global_folder_name in (".node_modules", ".node_libraries"):
            with self.subTest(global_folder_name=global_folder_name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    release = root / "tool/releases/v0.7.2"
                    release.mkdir(parents=True)
                    fake_home = root / "fake-home"
                    (fake_home / global_folder_name).mkdir(parents=True)
                    with mock.patch.dict(
                        os.environ, {"HOME": os.fspath(fake_home)}, clear=True
                    ):
                        with self.assertRaises(installer.PrimeInstallError):
                            installer.assert_no_unexpected_ancestor_node_modules(
                                release
                            )

    def test_assert_no_unexpected_ancestor_node_modules_catches_toolchain_lib_node(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "tool/releases/v0.7.2"
            (release / "toolchain/lib/node").mkdir(parents=True)
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    r"toolchain/lib/node$",
                ):
                    installer.assert_no_unexpected_ancestor_node_modules(release)

    def test_assert_no_unexpected_ancestor_node_modules_ignores_home_globals_when_home_unset_or_empty(
        self,
    ) -> None:
        # Mirrors real Node's own `if (homeDir)` check (see
        # node_global_folder_paths()'s docstring): an unset OR empty HOME
        # means the two HOME-based locations are not even considered --
        # so planting them must NOT cause a false-positive refusal.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "tool/releases/v0.7.2"
            release.mkdir(parents=True)
            # A directory OUTSIDE root entirely -- if this function ever
            # (incorrectly) treated a missing/empty HOME as "" and
            # resolved ".node_modules" relative to it, this would land
            # somewhere unrelated; either way this must not raise, since
            # real Node never adds these paths when HOME is unset/empty.
            for home_value in (None, ""):
                environ_override = {} if home_value is None else {"HOME": home_value}
                with mock.patch.dict(os.environ, environ_override, clear=True):
                    if home_value is None:
                        os.environ.pop("HOME", None)
                    # Must not raise.
                    installer.assert_no_unexpected_ancestor_node_modules(release)

    # Round 45, 2026-08-20 (independent Claude opus/max round-44 review):
    # node_global_folder_paths()'s two HOME-relative entries previously
    # used a bare os.path.join() (no lexical normalization), while real
    # Node builds the same entries with path.resolve() (pure lexical
    # normalization of ".."/"."/duplicate separators, no filesystem
    # access). A HOME containing ".." through a path component that does
    # not physically exist made the un-normalized candidate string ENOENT
    # under os.path.lexists() even though real Node's own lexical
    # resolution landed on a real, existing directory it actually loads
    # code from. The following tests reproduce that exact scenario end to
    # end (real generated launch guard, real pinned Node) for both
    # independent implementations, plus the leading-double-slash edge
    # case the round-44 review flagged as needing real-Node verification.

    def test_node_global_folder_paths_dotdot_traversal_through_nonexistent_component_mechanism(
        self,
    ) -> None:
        # Isolates the exact mechanism (no Node needed): a HOME value
        # that lexically normalizes back to a real, existing directory,
        # but only by passing through an intermediate component that
        # does not exist on disk. The bare os.path.join() candidate must
        # miss it (ENOENT on the nonexistent intermediate component); the
        # os.path.abspath()-normalized candidate this round's fix
        # produces must find it.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fake_home = root / "fake-home"
            (fake_home / ".node_modules").mkdir(parents=True)
            home_value = os.fspath(fake_home) + "/nonexistent-decoy/.."

            pre_fix_candidate = os.path.join(home_value, ".node_modules")
            self.assertFalse(
                os.path.lexists(pre_fix_candidate),
                "un-normalized candidate unexpectedly exists -- test setup invalid",
            )

            post_fix_candidate = os.path.abspath(pre_fix_candidate)
            self.assertEqual(
                post_fix_candidate, os.fspath(fake_home / ".node_modules")
            )
            self.assertTrue(os.path.lexists(post_fix_candidate))

    def test_launch_guard_main_refuses_home_dotdot_traversal_through_nonexistent_component(
        self,
    ) -> None:
        # Full real reproduction of round 44's finding: real Node
        # (invoked directly, no guard) genuinely resolves and loads a
        # package planted at the attacker's target via a HOME value that
        # traverses ".." through a nonexistent decoy component; the
        # generated launch guard's main(), with this round's fix, refuses
        # BEFORE that real exec ever happens.
        node = self._require_real_node()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            bundle_dir = release / "lib/node_modules/prime-agent/dist/bundle"
            bundle_dir.mkdir(parents=True, mode=0o700)
            marker = root / "cli-ran.marker"
            cli = bundle_dir / "cli.js"
            cli.write_text(
                "require('fs').writeFileSync(" + repr(os.fspath(marker)) + ", 'ran');\n",
                encoding="utf-8",
            )
            os.chmod(cli, 0o600)
            lock = tool_root / "lifecycle.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
            for directory_path in (root, tool_root, release.parent, release):
                os.chmod(directory_path, 0o700)
            node_copy = root / "node"
            shutil.copy(Path(node).resolve(), node_copy)
            os.chmod(node_copy, 0o700)
            fake_home = root / "fake-home"
            fake_home.mkdir(mode=0o700)
            # Traverses ".." through "nonexistent-decoy", which is never
            # created -- lexically normalizes straight back to fake_home.
            home_value = os.fspath(fake_home) + "/nonexistent-decoy/.."

            # Ground truth: real Node's own Module.globalPaths, given
            # this exact traversal HOME, genuinely includes fake_home's
            # real .node_modules -- confirming real Node performs the
            # same pure-lexical normalization this fix now matches.
            global_paths_result = subprocess.run(
                [
                    os.fspath(node_copy), "-e",
                    "console.log(JSON.stringify(require('module').globalPaths))",
                ],
                env={"HOME": home_value, "PATH": os.environ.get("PATH", "")},
                capture_output=True, text=True, timeout=15, check=False,
            )
            self.assertEqual(global_paths_result.returncode, 0, global_paths_result.stderr)
            real_global_paths = json.loads(global_paths_result.stdout)
            self.assertIn(
                os.fspath(fake_home / ".node_modules"), real_global_paths
            )

            # Ground truth exploit: real Node, invoked directly (no
            # guard) with this traversal HOME, DOES load and execute a
            # package planted at fake_home/.node_modules.
            package_dir = fake_home / ".node_modules" / "orca-marker-package"
            package_dir.mkdir(parents=True)
            (package_dir / "package.json").write_text(
                json.dumps({"name": "orca-marker-package", "main": "index.js"}),
                encoding="utf-8",
            )
            (package_dir / "index.js").write_text(
                "console.log('EXPLOIT_EXECUTED_VIA_DOTDOT_TRAVERSAL');\n",
                encoding="utf-8",
            )
            exploit_cli = root / "exploit-cli.js"
            exploit_cli.write_text(
                "try { require('orca-marker-package'); } "
                "catch (e) { console.log('NOT_LOADED:' + e.code); }\n",
                encoding="utf-8",
            )
            exploited_result = subprocess.run(
                [os.fspath(node_copy), os.fspath(exploit_cli)],
                env={"HOME": home_value, "PATH": os.environ.get("PATH", "")},
                capture_output=True, text=True, timeout=15, check=False,
            )
            self.assertIn("EXPLOIT_EXECUTED_VIA_DOTDOT_TRAVERSAL", exploited_result.stdout)

            # The fix, closed: the generated launch guard's main(), with
            # this exact traversal HOME, refuses BEFORE Node ever runs
            # CLI -- the marker must never appear.
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
            ):
                source = installer.managed_launch_guard_script(
                    node_copy, cli,
                    installer.sha256_file(node_copy), installer.sha256_file(cli),
                ).decode("utf-8")
            namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
            exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102

            buffer = io.StringIO()
            with (
                mock.patch.object(sys, "argv", ["prime-agent-launch-guard.py"]),
                mock.patch.dict(os.environ, {"HOME": home_value}, clear=True),
                contextlib.redirect_stderr(buffer),
            ):
                returncode = namespace["main"]()
            self.assertNotEqual(returncode, 0)
            self.assertIn(
                "unexpected item on Node's own module resolution path",
                buffer.getvalue(),
            )
            self.assertFalse(marker.exists())

    def test_assert_no_unexpected_ancestor_node_modules_catches_home_dotdot_traversal_through_nonexistent_component(
        self,
    ) -> None:
        # verify()-side counterpart of the test above -- pure Python
        # logic (the real-Node grounding for this exact traversal shape
        # is established by the launch-guard test above).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "tool/releases/v0.7.2"
            release.mkdir(parents=True)
            fake_home = root / "fake-home"
            (fake_home / ".node_libraries").mkdir(parents=True)
            home_value = os.fspath(fake_home) + "/nonexistent-decoy/.."

            with mock.patch.dict(os.environ, {"HOME": home_value}, clear=True):
                self.assertIn(
                    os.fspath(fake_home / ".node_libraries"),
                    installer.node_global_folder_paths(release),
                )
                with self.assertRaises(installer.PrimeInstallError):
                    installer.assert_no_unexpected_ancestor_node_modules(release)

    def test_node_global_folder_paths_leading_double_slash_diverges_textually_but_same_inode(
        self,
    ) -> None:
        # Round-44-flagged edge case: Python's os.path specially
        # preserves exactly two leading slashes (a POSIX-permitted
        # convention -- see os.path.normpath's documented behavior),
        # while real Node's path.resolve()/path.normalize() collapse a
        # leading "//" to a single "/". Confirms the two implementations
        # genuinely diverge TEXTUALLY, but that this platform's own
        # filesystem path resolution treats both forms as the identical
        # inode, so the presence check this function feeds is unaffected
        # in practice.
        node = self._require_real_node()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "tool/releases/v0.7.2"
            release.mkdir(parents=True)
            (root / ".node_modules").mkdir()
            home_value = "/" + os.fspath(root)  # doubles the leading slash
            self.assertTrue(home_value.startswith("//"))

            python_path = os.path.abspath(os.path.join(home_value, ".node_modules"))
            self.assertTrue(python_path.startswith("//"))

            node_result = subprocess.run(
                [
                    node, "-e",
                    "console.log(require('path').resolve(process.env.HOME, '.node_modules'))",
                ],
                env={"HOME": home_value, "PATH": os.environ.get("PATH", "")},
                capture_output=True, text=True, timeout=15, check=False,
            )
            self.assertEqual(node_result.returncode, 0, node_result.stderr)
            node_path = node_result.stdout.strip()
            self.assertFalse(node_path.startswith("//"))

            # Textual divergence, confirmed.
            self.assertNotEqual(python_path, node_path)

            # Same real file regardless, confirmed.
            self.assertTrue(os.path.lexists(python_path))
            self.assertTrue(os.path.lexists(node_path))
            self.assertEqual(os.stat(python_path).st_ino, os.stat(node_path).st_ino)

            # And the function under test still correctly detects
            # presence despite the textual divergence.
            with mock.patch.dict(os.environ, {"HOME": home_value}, clear=True):
                with self.assertRaises(installer.PrimeInstallError):
                    installer.assert_no_unexpected_ancestor_node_modules(release)

    def test_pending_install_commits_with_command_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            state = tool_root / "state"
            probe_home = tool_root / "probe-home"
            session_dir = tool_root / "sessions"
            user_home = root / "user"
            receipt_path = tool_root / "receipts" / f"v{installer.VERSION}.json"
            pending = tool_root / "pending-install.json"
            for path in (release / "bin", user_home, receipt_path.parent):
                path.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(release, 0o700)
            settings = self.create_managed_home(
                state, session_dir=tool_root / "sessions"
            )
            self.create_managed_home(
                probe_home, session_dir=tool_root / "sessions"
            )
            session_dir.mkdir(mode=0o700)
            entrypoint = release / "bin" / "prime-agent"
            entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(entrypoint, 0o700)
            launch_guard = release / "bin" / "prime-agent-launch-guard.py"
            launch_guard.write_text("#!/usr/bin/python3\n", encoding="utf-8")
            os.chmod(launch_guard, 0o700)
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            with mock.patch.object(installer, "SSD_ROOT", root):
                digest, entries = installer.tree_digest(release)
            state_link = user_home / ".prime"
            bin_link = user_home / ".local/bin/prime-agent"
            receipt = {
                "schema": installer.RECEIPT_SCHEMA,
                "version": installer.VERSION,
                "tag_commit": installer.TAG_COMMIT,
                "volume_uuid": "TEST-UUID",
                "release_dir": os.fspath(release),
                "state_dir": os.fspath(state),
                "state_link": os.fspath(state_link),
                "bin_link": os.fspath(bin_link),
                "bin_target": os.fspath(entrypoint),
                "launch_guard": os.fspath(launch_guard),
                "lifecycle_lock": os.fspath(lifecycle_lock),
                "node_target": os.fspath(release / "toolchain/bin/node"),
                "npm_target": os.fspath(
                    release / "toolchain/lib/node_modules/npm/bin/npm-cli.js"
                ),
                "probe_home": os.fspath(probe_home),
                "probe_agent_dir": os.fspath(probe_home / "agent"),
                "session_dir": os.fspath(session_dir),
                "asset_sha256": installer.ASSETS,
                "node_asset_sha256": installer.NODE_ASSET_SHA256,
                "upstream_lock_sha256": installer.LOCK_SHA256,
                "license_sha256": installer.LICENSE_SHA256,
                "production_lock_sha256": installer.GENERATED_LOCK_SHA256,
                "patched_manifest_names": sorted(("prime-agent", *installer.WORKSPACE_PACKAGES)),
                "closure": {
                    "lock_sha256": installer.GENERATED_LOCK_SHA256,
                    "packages_checked": installer.GENERATED_LOCK_PACKAGE_COUNT,
                    "registry_packages_checked": installer.GENERATED_LOCK_PACKAGE_COUNT - 4,
                },
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "lifecycle_scripts_executed": False,
                "daemon_started": False,
                "credentials_configured": False,
                "command_default_enabled": False,
                "project_settings_default_allowed": False,
                "project_executable_resources_default_allowed": False,
                "telemetry_default_enabled": False,
                "telemetry_settings": os.fspath(settings),
                "release_tree_sha256": digest,
                "release_tree_entries": entries,
                "orca_support": {"test": "support"},
            }
            installer.atomic_write(
                pending,
                installer.canonical_json(
                    {"schema": installer.JOURNAL_SCHEMA, "receipt": receipt}
                ),
                0o600,
            )
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", state),
                mock.patch.object(installer, "PROBE_HOME", probe_home),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(installer, "STATE_LINK", state_link),
                mock.patch.object(installer, "BIN_LINK", bin_link),
                mock.patch.object(installer, "RECEIPT_PATH", receipt_path),
                mock.patch.object(installer, "PENDING_PATH", pending),
                mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID"),
                mock.patch.object(
                    installer, "verify_orca_support", return_value={"test": "support"}
                ),
            ):
                with mock.patch.object(
                    installer,
                    "prime_agent_command_candidates",
                    return_value=[Path("/alternate/bin/prime-agent")],
                ):
                    with self.assertRaisesRegex(
                        installer.PrimeInstallError, "existing prime-agent command"
                    ):
                        installer.finalize_pending_install()
                self.assertTrue(pending.is_file())
                self.assertFalse(receipt_path.exists())
                self.assertFalse(state_link.exists())
                first = installer.finalize_pending_install()
            self.assertEqual(first["version"], installer.VERSION)
            self.assertTrue(state_link.is_symlink())
            self.assertFalse(bin_link.exists())
            self.assertFalse(bin_link.is_symlink())
            self.assertTrue(receipt_path.is_file())
            self.assertFalse(pending.exists())

    def test_receipt_publication_preserves_late_occupant_and_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "release"
            state = tool_root / "state"
            probe = tool_root / "probe-home"
            sessions = tool_root / "sessions"
            receipts = tool_root / "receipts"
            user_home = root / "user"
            for path in (release, receipts, user_home):
                path.mkdir(parents=True, mode=0o700, exist_ok=True)
            for path in (tool_root, release, receipts, user_home):
                os.chmod(path, 0o700)
            payload = release / "payload"
            payload.write_text("runtime", encoding="utf-8")
            os.chmod(payload, 0o600)
            self.create_managed_home(state, session_dir=sessions)
            self.create_managed_home(probe, session_dir=sessions)
            sessions.mkdir(mode=0o700)
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            with mock.patch.object(installer, "SSD_ROOT", root):
                digest, entries = installer.tree_digest(release)
            receipt = {
                "version": installer.VERSION,
                "volume_uuid": "TEST-UUID",
                "orca_support": {"test": "support"},
                "lifecycle_lock": os.fspath(lifecycle_lock),
                "release_tree_sha256": digest,
                "release_tree_entries": entries,
            }
            pending = tool_root / "pending-install.json"
            receipt_path = receipts / f"v{installer.VERSION}.json"
            installer.atomic_write(
                pending,
                installer.canonical_json(
                    {"schema": installer.JOURNAL_SCHEMA, "receipt": receipt}
                ),
                0o600,
            )
            original_rename = installer.rename_noreplace
            inserted = False

            def insert_late_receipt(source: Path, destination: Path) -> None:
                nonlocal inserted
                if destination == receipt_path and not inserted:
                    inserted = True
                    destination.write_text("late receipt", encoding="utf-8")
                    os.chmod(destination, 0o600)
                original_rename(source, destination)

            state_link = user_home / ".prime"
            bin_link = user_home / ".local/bin/prime-agent"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", state),
                mock.patch.object(installer, "PROBE_HOME", probe),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(installer, "STATE_LINK", state_link),
                mock.patch.object(installer, "BIN_LINK", bin_link),
                mock.patch.object(installer, "RECEIPT_PATH", receipt_path),
                mock.patch.object(installer, "PENDING_PATH", pending),
                mock.patch.object(
                    installer, "validate_receipt_identity", return_value=receipt
                ),
                mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID"),
                mock.patch.object(
                    installer, "verify_orca_support", return_value={"test": "support"}
                ),
                mock.patch.object(
                    installer,
                    "rename_noreplace",
                    side_effect=insert_late_receipt,
                ),
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "refusing to replace"
                ):
                    installer.finalize_pending_install()
            self.assertEqual(
                receipt_path.read_text(encoding="utf-8"), "late receipt"
            )
            self.assertTrue(pending.is_file())
            self.assertTrue(state_link.is_symlink())
            self.assertFalse(bin_link.exists())
            self.assertEqual(list(receipts.glob(f".{receipt_path.name}.*")), [])

    def test_make_patched_asset_refuses_symlinked_output_path(self) -> None:
        # Regression for P1-1: make_patched_asset() used to publish through a
        # direct `patched.open("wb")` on a known, predictable output path,
        # with no O_NOFOLLOW, create-only semantics, or destination-identity
        # check. If that path had been swapped for a symlink to a file
        # outside SSD_ROOT between assets-dir creation and this write, the
        # write would silently follow the symlink and clobber the victim.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            assets = release / "assets"
            assets.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root / "releases", 0o700)
            os.chmod(release, 0o700)
            os.chmod(assets, 0o700)

            original_asset = root / "prime-agent-source.tgz"
            manifest_payload = installer.canonical_json(
                {"name": "prime-agent", "version": installer.VERSION}
            )
            with tarfile.open(original_asset, "w:gz") as archive:
                info = tarfile.TarInfo("package/package.json")
                info.size = len(manifest_payload)
                archive.addfile(info, io.BytesIO(manifest_payload))
            os.chmod(original_asset, 0o600)

            # A victim file OUTSIDE the mocked SSD root, plus a symlink at the
            # exact predictable output path make_patched_asset is about to
            # write, simulating a same-UID process that placed it there
            # between assets-dir creation and this call.
            victim = root.parent / f"{root.name}-make-patched-asset-victim"
            victim.write_bytes(b"victim content, must survive unchanged")
            output_name = "prime-agent-orca-pinned-victim-test.tgz"
            output_path = assets / output_name
            try:
                output_path.symlink_to(victim)
                with (
                    mock.patch.object(installer, "SSD_ROOT", root),
                    mock.patch.object(installer, "RELEASE_DIR", release),
                ):
                    with self.assertRaisesRegex(
                        installer.PrimeInstallError, "refusing to replace"
                    ):
                        installer.make_patched_asset(
                            original_asset,
                            installer.sha256_file(original_asset),
                            {"packages": {}},
                            assets,
                            expected_name="prime-agent",
                            managed_name="prime-agent",
                            output_name=output_name,
                        )
                self.assertTrue(output_path.is_symlink())
                self.assertEqual(os.readlink(output_path), os.fspath(victim))
                self.assertEqual(
                    victim.read_bytes(), b"victim content, must survive unchanged"
                )
                self.assertEqual(list(assets.glob(f".{output_name}.*")), [])
            finally:
                victim.unlink()

    def test_make_patched_asset_refuses_preexisting_unpack_directory(self) -> None:
        # Regression for independent review round 2, 2026-08-18, P1
        # (residual of round-1 P1-1): make_patched_asset() used to call
        # ensure_private_dir() on its per-package ".unpacked-<name>"
        # directory, which accepts a pre-existing same-UID directory.
        # Because that name is fully deterministic (computed from
        # `managed_name`, a module constant), a same-UID attacker could
        # pre-plant it -- with extra files already inside -- at any point
        # during the long download window that precedes this call, and
        # have it silently trusted: extraction never deletes non-member
        # files, so anything already there survived and was swept into the
        # published patched tarball by the old rglob()-based archive loop.
        # create_fresh_private_dir() now refuses ANY pre-existing occupant
        # at this deterministic path.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            assets = release / "assets"
            assets.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root / "releases", 0o700)
            os.chmod(release, 0o700)
            os.chmod(assets, 0o700)

            original_asset = root / "prime-agent-source.tgz"
            manifest_payload = installer.canonical_json(
                {"name": "prime-agent", "version": installer.VERSION}
            )
            with tarfile.open(original_asset, "w:gz") as archive:
                info = tarfile.TarInfo("package/package.json")
                info.size = len(manifest_payload)
                archive.addfile(info, io.BytesIO(manifest_payload))
            os.chmod(original_asset, 0o600)

            # Attacker pre-plants the deterministic unpack directory, with a
            # file that was never part of the real, digest-verified
            # tarball already sitting where the real "package" root lands.
            unpacked = release / ".unpacked-prime-agent"
            planted_package = unpacked / "package"
            planted_package.mkdir(parents=True, mode=0o700)
            # mkdir(parents=True, mode=...) only applies `mode` to the leaf;
            # chmod the intermediate directory explicitly so it looks
            # exactly like the fully-valid, same-UID 0700 directory
            # ensure_private_dir() used to silently accept.
            os.chmod(unpacked, 0o700)
            (planted_package / "evil.js").write_bytes(b"attacker payload")

            output_name = "prime-agent-orca-pinned-preplant-test.tgz"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "RELEASE_DIR", release),
            ):
                with self.assertRaisesRegex(installer.PrimeInstallError, "cannot create"):
                    installer.make_patched_asset(
                        original_asset,
                        installer.sha256_file(original_asset),
                        {"packages": {}},
                        assets,
                        expected_name="prime-agent",
                        managed_name="prime-agent",
                        output_name=output_name,
                    )
            # Nothing was ever published from the attacker's directory, and
            # the pre-planted content is left exactly as the attacker left
            # it (never read, never trusted).
            self.assertFalse((assets / output_name).exists())
            self.assertEqual(
                (planted_package / "evil.js").read_bytes(), b"attacker payload"
            )

    def test_extract_node_toolchain_refuses_preexisting_destination_with_symlinked_subdir(
        self,
    ) -> None:
        # Regression for independent review round 2, 2026-08-18, P1
        # (residual of round-1 P1-1): extract_node_toolchain() used to call
        # ensure_private_dir() on its destination, which accepts a
        # pre-existing same-UID directory. A same-UID attacker could
        # pre-plant the deterministic "toolchain" directory with a
        # symlinked "bin" pointing outside SSD_ROOT during the long
        # download window that precedes this call; extraction then wrote,
        # and chmod'd, straight through the symlink, and the function
        # returned SUCCESS with a node path resolving outside SSD_ROOT.
        # create_fresh_private_dir() now refuses any pre-existing occupant
        # at this deterministic path, so the extraction loop never runs.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            ssd.mkdir(mode=0o700)
            destination = ssd / "toolchain"

            outside = root / "outside-victim"
            outside.mkdir(mode=0o755)
            (outside / "sentinel").write_bytes(b"do-not-touch")

            destination.mkdir(mode=0o700)
            (destination / "bin").symlink_to(outside, target_is_directory=True)

            # A complete node+npm layout, so an unfixed extract_node_toolchain
            # would actually reach its final success return (matching the
            # original report's "raised=None (the function RETURNS SUCCESS)")
            # instead of incidentally failing on an unrelated missing file.
            node_asset = root / "node.tgz"
            expected_root = f"node-v{installer.NODE_VERSION}-darwin-arm64"
            node_payload = b"#!/bin/sh\necho fake-node\n"
            npm_payload = b"#!/usr/bin/env node\n// fake npm cli\n"
            with tarfile.open(node_asset, "w:gz") as archive:
                info = tarfile.TarInfo(f"{expected_root}/bin/node")
                info.mode = 0o755
                info.size = len(node_payload)
                archive.addfile(info, io.BytesIO(node_payload))
                info = tarfile.TarInfo(f"{expected_root}/lib/node_modules/npm/bin/npm-cli.js")
                info.mode = 0o644
                info.size = len(npm_payload)
                archive.addfile(info, io.BytesIO(npm_payload))
            os.chmod(node_asset, 0o600)

            with mock.patch.object(installer, "SSD_ROOT", ssd):
                with self.assertRaises(installer.PrimeInstallError):
                    installer.extract_node_toolchain(
                        node_asset, destination, installer.sha256_file(node_asset)
                    )

            # Nothing under the attacker's directory may have been written
            # to, or have had its permissions changed, because the
            # extraction loop must never have run.
            self.assertEqual(sorted(p.name for p in outside.iterdir()), ["sentinel"])
            self.assertEqual((outside / "sentinel").read_bytes(), b"do-not-touch")
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)

    def test_safe_extract_main_asset_symlink_scan_catches_late_planted_symlink(
        self,
    ) -> None:
        # Regression for a test-coverage gap independent review round 7,
        # 2026-08-18 found: assert_tree_has_no_symlinks() (added round 2,
        # 2026-08-18, P1, as the fail-closed check for a same-UID racer that
        # plants a symlink somewhere in the extracted tree during the
        # narrow in-process window create_fresh_private_dir() cannot itself
        # close) had ZERO effective regression coverage -- the round-7
        # reviewer deleted both of its call sites (here, and in
        # extract_node_toolchain()) and the full suite still passed. The
        # test that appeared to cover this,
        # test_extract_node_toolchain_refuses_preexisting_destination_with_symlinked_subdir,
        # actually fails earlier inside create_fresh_private_dir() (its
        # `destination` pre-exists) and never reaches
        # assert_tree_has_no_symlinks() at all.
        #
        # This test instead lets extraction complete completely normally --
        # a symlink can never arrive via a tar member here (safe_extract_
        # main_asset() rejects any member.issym()/islnk() outright, before
        # ever writing it) -- and simulates a same-UID racer winning the
        # narrow window between the extraction loop finishing and this
        # function's own symlink scan starting, by hooking Path.rglob()
        # itself: the FIRST (and, in this function, only) rglob("*") call
        # made against the exact `package_dir` this call computes is where
        # assert_tree_has_no_symlinks() begins its walk, so planting the
        # symlink there -- immediately before delegating to the real
        # rglob() -- places it exactly at the boundary of the race window
        # the docstring describes, with no other check in between able to
        # have caught it first (mirrors this file's established convention
        # of injecting a same-UID race deterministically via a wrapped real
        # call rather than real concurrent threads; see
        # test_atomic_symlink_ancestor_swap_cannot_escape_verified_parent).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            archive_path = root / "asset.tgz"
            payload = installer.canonical_json(
                {"name": "prime-agent", "version": installer.VERSION}
            )
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("package/package.json")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            os.chmod(archive_path, 0o600)

            destination = root / "out"
            destination.mkdir(mode=0o700)
            expected_package_dir = destination / "package"
            outside_target = root / "outside-victim"
            outside_target.mkdir(mode=0o700)

            real_rglob = Path.rglob
            planted = {"done": False}

            def hooked_rglob(self: Path, pattern: str):
                if (
                    not planted["done"]
                    and pattern == "*"
                    and self == expected_package_dir
                ):
                    planted["done"] = True
                    (self / "sneaky-symlink").symlink_to(outside_target)
                return real_rglob(self, pattern)

            with mock.patch.object(Path, "rglob", hooked_rglob):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "unexpected symlink in managed extraction tree",
                ):
                    installer.safe_extract_main_asset(
                        archive_path, destination, installer.sha256_file(archive_path)
                    )
            # The hook must actually have fired -- otherwise this test would
            # trivially pass by never exercising the race at all.
            self.assertTrue(planted["done"])

    def test_extract_node_toolchain_symlink_scan_catches_late_planted_symlink(
        self,
    ) -> None:
        # Companion to test_safe_extract_main_asset_symlink_scan_catches_
        # late_planted_symlink, above, for extract_node_toolchain()'s OWN
        # assert_tree_has_no_symlinks(destination) call -- the round-7
        # coverage gap applied to both call sites, and
        # test_extract_node_toolchain_refuses_preexisting_destination_with_symlinked_subdir
        # (a pre-existing `destination`) only ever exercised
        # create_fresh_private_dir()'s separate refusal, never this check.
        # A symlink member is silently skipped (not written) by this
        # function's own per-member loop, so -- as with the main-asset
        # variant -- the only way a symlink reaches `destination` before
        # this scan is a same-UID race; simulated the same deterministic
        # way, by planting it the instant this function's own rglob("*")
        # walk over `destination` begins.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            ssd.mkdir(mode=0o700)
            destination = ssd / "toolchain"
            outside_target = root / "outside-victim"
            outside_target.mkdir(mode=0o700)

            expected_root = f"node-v{installer.NODE_VERSION}-darwin-arm64"
            node_payload = b"#!/bin/sh\necho fake-node\n"
            npm_payload = b"#!/usr/bin/env node\n// fake npm cli\n"
            node_asset = root / "node.tgz"
            with tarfile.open(node_asset, "w:gz") as archive:
                info = tarfile.TarInfo(f"{expected_root}/bin/node")
                info.mode = 0o755
                info.size = len(node_payload)
                archive.addfile(info, io.BytesIO(node_payload))
                info = tarfile.TarInfo(f"{expected_root}/lib/node_modules/npm/bin/npm-cli.js")
                info.mode = 0o644
                info.size = len(npm_payload)
                archive.addfile(info, io.BytesIO(npm_payload))
            os.chmod(node_asset, 0o600)

            real_rglob = Path.rglob
            planted = {"done": False}

            def hooked_rglob(self: Path, pattern: str):
                if (
                    not planted["done"]
                    and pattern == "*"
                    and self == destination
                ):
                    planted["done"] = True
                    (self / "sneaky-symlink").symlink_to(outside_target)
                return real_rglob(self, pattern)

            with (
                mock.patch.object(installer, "SSD_ROOT", ssd),
                mock.patch.object(Path, "rglob", hooked_rglob),
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "unexpected symlink in managed extraction tree",
                ):
                    installer.extract_node_toolchain(
                        node_asset, destination, installer.sha256_file(node_asset)
                    )
            self.assertTrue(planted["done"])

    def test_safe_extract_main_asset_rejects_disk_content_swapped_after_download(
        self,
    ) -> None:
        # Regression for independent review round 13/14, 2026-08-18, P1-B:
        # safe_download() verifies downloaded bytes IN MEMORY against a
        # pinned digest and then writes them to disk; safe_extract_main_
        # asset() used to re-open that exact same on-disk path via
        # tarfile.open(asset, "r:gz") -- by PATH, a second time -- with NO
        # digest re-check at that point. A same-UID actor with a window
        # between that verified write and this later, independent open
        # (which can span the rest of preflight, every other safe_download()
        # call, and Node/npm version probing) could swap the file for
        # different-but-still-well-formed tarball content and have it
        # extract -- and, via the eventual patched-asset `npm ci`, execute
        # -- completely unnoticed.
        #
        # This fakes the OUTCOME of a real safe_download() call (write
        # digest-verified bytes to disk, exactly as safe_download() itself
        # does -- a private, 0600, owner-only regular file) and then
        # simulates a same-UID racer overwriting those exact on-disk bytes
        # in place before extraction runs, with a DIFFERENT but still
        # well-formed tarball -- so a pre-fix run would actually extract the
        # tampered content successfully instead of merely crashing on
        # garbage bytes, which is what makes this a real silent-compromise
        # finding and not just a crash.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            asset = root / "prime-agent-source.tgz"

            def build_tarball(payload: bytes) -> bytes:
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                    info = tarfile.TarInfo("package/package.json")
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                return buffer.getvalue()

            original_manifest = installer.canonical_json(
                {"name": "prime-agent", "version": installer.VERSION}
            )
            original_bytes = build_tarball(original_manifest)
            expected_sha256 = installer.sha256_bytes(original_bytes)

            # Step 1: exactly what a real safe_download() call leaves behind
            # -- digest-verified bytes written to disk as a private, 0600,
            # owner-only regular file.
            asset.write_bytes(original_bytes)
            os.chmod(asset, 0o600)

            # Step 2: a same-UID racer swaps the file's content in place,
            # after the verified write and strictly before extraction --
            # different content (a DIFFERENT valid manifest identity), not
            # just corrupted bytes, so a pre-fix run would extract it as if
            # it were the real, verified release.
            tampered_manifest = installer.canonical_json(
                {"name": "prime-agent", "version": installer.VERSION, "malicious": True}
            )
            tampered_bytes = build_tarball(tampered_manifest)
            self.assertNotEqual(installer.sha256_bytes(tampered_bytes), expected_sha256)
            with open(asset, "wb") as handle:
                handle.write(tampered_bytes)
            os.chmod(asset, 0o600)

            destination = root / "out"
            destination.mkdir(mode=0o700)
            with self.assertRaisesRegex(
                installer.PrimeInstallError,
                "changed on disk before extraction",
            ):
                installer.safe_extract_main_asset(asset, destination, expected_sha256)
            # Nothing from the tampered tarball may have been extracted.
            self.assertFalse((destination / "package").exists())

    def test_extract_node_toolchain_rejects_disk_content_swapped_after_download(
        self,
    ) -> None:
        # Companion to test_safe_extract_main_asset_rejects_disk_content_
        # swapped_after_download, above, for extract_node_toolchain()'s
        # identical TOCTOU gap against the pinned Node.js toolchain asset --
        # a same-UID swap of the actual Node/npm runtime that later gets
        # executed is at least as severe as swapping the Prime Agent release
        # tarball itself.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            ssd.mkdir(mode=0o700)
            asset = root / "node.tgz"
            expected_root = f"node-v{installer.NODE_VERSION}-darwin-arm64"

            def build_node_tarball(node_payload: bytes) -> bytes:
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                    info = tarfile.TarInfo(f"{expected_root}/bin/node")
                    info.mode = 0o755
                    info.size = len(node_payload)
                    archive.addfile(info, io.BytesIO(node_payload))
                    npm_payload = b"#!/usr/bin/env node\n// fake npm cli\n"
                    info = tarfile.TarInfo(
                        f"{expected_root}/lib/node_modules/npm/bin/npm-cli.js"
                    )
                    info.mode = 0o644
                    info.size = len(npm_payload)
                    archive.addfile(info, io.BytesIO(npm_payload))
                return buffer.getvalue()

            original_bytes = build_node_tarball(b"#!/bin/sh\necho real-node\n")
            expected_sha256 = installer.sha256_bytes(original_bytes)
            asset.write_bytes(original_bytes)
            os.chmod(asset, 0o600)

            # Same-UID racer swaps the pinned Node runtime for a DIFFERENT,
            # still well-formed, tarball after the verified write.
            tampered_bytes = build_node_tarball(b"#!/bin/sh\necho attacker-controlled\n")
            self.assertNotEqual(installer.sha256_bytes(tampered_bytes), expected_sha256)
            with open(asset, "wb") as handle:
                handle.write(tampered_bytes)
            os.chmod(asset, 0o600)

            destination = ssd / "toolchain"
            with mock.patch.object(installer, "SSD_ROOT", ssd):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "changed on disk before extraction",
                ):
                    installer.extract_node_toolchain(asset, destination, expected_sha256)
            # Nothing from the tampered archive may have been extracted.
            self.assertFalse((destination / "bin" / "node").exists())

    def test_safe_extract_main_asset_manifest_excludes_untracked_files(self) -> None:
        # Regression for independent review round 2, 2026-08-18, P1: the
        # manifest safe_extract_main_asset() returns must list only the
        # paths it itself verified from the digest-checked tar, never
        # anything else physically sitting in `destination` -- extraction
        # only writes tar members and never deletes pre-existing,
        # non-member content, so a caller that walked the directory
        # instead of trusting this manifest (the old code) would publish
        # untracked files unchanged.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            archive_path = root / "asset.tgz"
            payload = installer.canonical_json(
                {"name": "prime-agent", "version": installer.VERSION}
            )
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("package/package.json")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            os.chmod(archive_path, 0o600)

            destination = root / "out"
            destination.mkdir(mode=0o700)
            untracked_dir = destination / "package"
            untracked_dir.mkdir(mode=0o700)
            (untracked_dir / "evil.js").write_bytes(b"attacker payload")

            package_dir, manifest, digests, dir_modes = installer.safe_extract_main_asset(
                archive_path, destination, installer.sha256_file(archive_path)
            )
            self.assertEqual(set(digests), {Path("package.json")})
            self.assertEqual(dir_modes, {})
            self.assertEqual(
                digests[Path("package.json")], installer.sha256_bytes(payload)
            )

            self.assertIn(Path("package.json"), manifest)
            self.assertNotIn(Path("evil.js"), manifest)
            # The untracked file is left physically in place (extraction
            # never deletes non-member content) but must never be reported
            # as part of the verified manifest.
            self.assertTrue((package_dir / "evil.js").exists())

    def test_make_patched_asset_ignores_files_injected_after_extraction(self) -> None:
        # Regression for independent review round 2, 2026-08-18, P1:
        # make_patched_asset() used to build the published tarball by
        # walking package_dir.rglob("*") -- everything physically present
        # at archiving time, including a file a same-UID racer wrote into
        # package_dir in the window between safe_extract_main_asset()
        # returning and the archive loop starting. It now archives only
        # the manifest safe_extract_main_asset() itself verified, so a
        # file injected in that exact window is never published. Follows
        # this file's established convention for testing a TOCTOU window
        # (see test_atomic_symlink_ancestor_swap_cannot_escape_verified_parent):
        # wrap the real function with a side_effect that races immediately
        # after it returns, rather than mocking away the logic under test.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            assets = release / "assets"
            assets.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root / "releases", 0o700)
            os.chmod(release, 0o700)
            os.chmod(assets, 0o700)

            original_asset = root / "prime-agent-source.tgz"
            manifest_payload = installer.canonical_json(
                {"name": "prime-agent", "version": installer.VERSION}
            )
            with tarfile.open(original_asset, "w:gz") as archive:
                info = tarfile.TarInfo("package/package.json")
                info.size = len(manifest_payload)
                archive.addfile(info, io.BytesIO(manifest_payload))
            os.chmod(original_asset, 0o600)

            real_safe_extract_main_asset = installer.safe_extract_main_asset

            def race_after_extraction(asset: Path, destination: Path, expected_sha256: str):
                package_dir, manifest, digests, dir_modes = real_safe_extract_main_asset(
                    asset, destination, expected_sha256
                )
                # The instant after extraction finishes and is verified, a
                # same-UID racer drops an extra file straight into the
                # now-real (and no longer creatable-fresh) package
                # directory.
                (package_dir / "postinstall-evil.js").write_bytes(b"attacker payload")
                return package_dir, manifest, digests, dir_modes

            output_name = "prime-agent-orca-pinned-race-test.tgz"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(
                    installer,
                    "safe_extract_main_asset",
                    side_effect=race_after_extraction,
                ),
            ):
                patched, _digest, _manifest, _identity, _content_digests = installer.make_patched_asset(
                    original_asset,
                    installer.sha256_file(original_asset),
                    {"packages": {}},
                    assets,
                    expected_name="prime-agent",
                    managed_name="prime-agent",
                    output_name=output_name,
                )

            with tarfile.open(patched, "r:gz") as published:
                names = published.getnames()
            self.assertIn("package/package.json", names)
            self.assertNotIn("package/postinstall-evil.js", names)

    def test_make_patched_asset_detects_content_swap_before_archiving(self) -> None:
        # Regression for independent dual review round 6, 2026-08-18, P1
        # (pre-existing, not round-5-introduced): make_patched_asset()
        # lstat-verified each extracted member's TYPE and OWNER immediately
        # before archiving it, but never its CONTENT -- archive.add() then
        # performed its own, entirely separate open()+read() of the same
        # path to actually pull the bytes into the published tarball. A
        # same-UID actor with a real window between the digest-verified
        # extraction and that later, separate read (reviewer measured
        # ~1.9s to ~27s per file in a real reproduction with a polling
        # thread, 4/4 trials) could swap a file's content in that gap and
        # have the swapped bytes archived into the artifact that becomes
        # the shipped/executed "patched" output, with no downstream gate
        # catching it (the receipt's integrity fields are computed from the
        # already-tampered tree, not cross-checked against the original
        # verified digest).
        #
        # safe_extract_main_asset() now captures each regular file's
        # content digest while streaming it from the tarball-verified
        # source, and make_patched_asset() re-verifies each file's bytes
        # against that digest at the exact moment it reads them for
        # archiving (via read_private_file(), whose returned bytes are
        # archived directly -- there is no second, separate read of the
        # path afterward) -- closing the gap entirely rather than merely
        # narrowing it. Follows this file's established convention for
        # testing a TOCTOU window (see
        # test_make_patched_asset_ignores_files_injected_after_extraction):
        # wrap the real safe_extract_main_asset() with a side_effect that
        # swaps a file's content immediately after it returns (a
        # deterministic stand-in for winning the race, not a mock of the
        # archiving logic under test).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            assets = release / "assets"
            assets.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root / "releases", 0o700)
            os.chmod(release, 0o700)
            os.chmod(assets, 0o700)

            original_asset = root / "prime-agent-source.tgz"
            manifest_payload = installer.canonical_json(
                {"name": "prime-agent", "version": installer.VERSION}
            )
            real_payload = (
                b"real, digest-verified content straight from the release tarball\n"
            )
            with tarfile.open(original_asset, "w:gz") as archive:
                info = tarfile.TarInfo("package/package.json")
                info.size = len(manifest_payload)
                archive.addfile(info, io.BytesIO(manifest_payload))
                info = tarfile.TarInfo("package/dist/bundle.js")
                info.size = len(real_payload)
                archive.addfile(info, io.BytesIO(real_payload))
            os.chmod(original_asset, 0o600)

            real_safe_extract_main_asset = installer.safe_extract_main_asset

            def swap_after_extraction(asset: Path, destination: Path, expected_sha256: str):
                package_dir, manifest, digests, dir_modes = real_safe_extract_main_asset(
                    asset, destination, expected_sha256
                )
                # The instant after extraction finishes -- and this file's
                # content has already been digest-verified against the real
                # tarball -- a same-UID racer overwrites the already
                # -extracted file's bytes in place, before
                # make_patched_asset()'s archiving loop ever reaches it.
                swapped = package_dir / "dist/bundle.js"
                installer.atomic_write(
                    swapped, b"attacker-controlled payload\n", 0o600
                )
                return package_dir, manifest, digests, dir_modes

            output_name = "prime-agent-orca-pinned-content-swap-test.tgz"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(
                    installer,
                    "safe_extract_main_asset",
                    side_effect=swap_after_extraction,
                ),
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "extracted member content changed before publish",
                ):
                    installer.make_patched_asset(
                        original_asset,
                        installer.sha256_file(original_asset),
                        {"packages": {}},
                        assets,
                        expected_name="prime-agent",
                        managed_name="prime-agent",
                        output_name=output_name,
                    )
            # Nothing was ever published from the tampered tree -- the
            # attacker-controlled content must never reach a shipped
            # artifact, not even a partially-written one.
            self.assertFalse((assets / output_name).exists())
            self.assertEqual(list(assets.glob(f".{output_name}.*")), [])

    def test_make_patched_asset_directory_entry_survives_symlink_swap_in_the_gap(
        self,
    ) -> None:
        # Regression for independent review round 7/8, 2026-08-18, P1
        # (independently confirmed by two reviewers): the directory branch
        # of make_patched_asset()'s archiving loop used to lstat-validate a
        # directory member and then publish it via archive.add(path, ...,
        # recursive=False) -- but archive.add() performs its OWN, entirely
        # separate, internal lstat() (via tarfile's gettarinfo(), which
        # calls os.lstat(), not the earlier pathlib check) of `path` to
        # actually build the TarInfo it writes. A same-UID racer that swaps
        # the validated directory for a symlink to an arbitrary, possibly
        # out-of-tree target in the gap between this function's own
        # validating lstat() and that later, separate, internal
        # archive.add() re-stat had the published archive silently contain
        # a symlink member in place of the intended directory entry, from a
        # link-free, digest-verified source archive.
        #
        # Verified against pre-fix (round-7 HEAD) code in an isolated
        # scratch copy: injecting the swap any EARLIER than this exact gap
        # (e.g. immediately after safe_extract_main_asset() returns, before
        # the archiving loop even starts) is actually already caught by the
        # pre-fix code's OWN lstat check -- S_ISLNK there correctly refuses
        # it -- so a faithful regression test for this specific finding
        # must land the swap exactly between that check and archive.add()'s
        # own later, independent re-stat, not merely "at some point after
        # extraction". This is simulated deterministically (matching this
        # file's established convention for TOCTOU tests -- injecting a
        # race as a side effect of a real call rather than using actual
        # concurrent threads) by hooking Path.lstat() itself: the swap
        # fires as a side effect of the SPECIFIC lstat() call this
        # function's own directory-branch validation makes for this exact
        # path (learned from safe_extract_main_asset()'s real, unmocked
        # return value, not hardcoded), immediately after that call
        # captures its own (still pre-swap) result -- so the validation
        # step still sees a valid directory and proceeds, while archive.
        # add()'s later, separate os.lstat() sees the now-swapped symlink.
        #
        # Fixed the same way round 6 already fixed the analogous gap for
        # file CONTENT (read_private_file(): one atomic verify-then-use
        # operation, no second path-based lookup): the directory branch no
        # longer touches the path at all -- it publishes a fixed DIRTYPE
        # TarInfo built entirely from the mode safe_extract_main_asset()
        # itself recorded when it originally created this exact directory.
        # Against the fix, this same hook is confirmed to never even fire
        # (verified below): the vulnerable lstat() call the hook targets no
        # longer exists in the directory branch at all, so there is
        # structurally nothing left in this path for a same-UID racer to
        # win a race against.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            assets = release / "assets"
            assets.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root / "releases", 0o700)
            os.chmod(release, 0o700)
            os.chmod(assets, 0o700)

            original_asset = root / "prime-agent-source.tgz"
            manifest_payload = installer.canonical_json(
                {"name": "prime-agent", "version": installer.VERSION}
            )
            with tarfile.open(original_asset, "w:gz") as archive:
                info = tarfile.TarInfo("package/package.json")
                info.size = len(manifest_payload)
                archive.addfile(info, io.BytesIO(manifest_payload))
                # An explicit, empty directory member -- a package-relative
                # "dist" directory with nothing else inside it, so the race
                # below can swap the whole directory for a symlink without
                # needing to first relocate any file that lives under it.
                dir_info = tarfile.TarInfo("package/dist")
                dir_info.type = tarfile.DIRTYPE
                dir_info.mode = 0o755
                archive.addfile(dir_info)
            os.chmod(original_asset, 0o600)

            outside_target = root / "outside-victim"
            outside_target.mkdir(mode=0o700)
            (outside_target / "sentinel").write_bytes(b"do-not-touch")

            real_safe_extract_main_asset = installer.safe_extract_main_asset
            package_dir_holder: dict[str, Path] = {}

            def learn_package_dir(asset: Path, destination: Path, expected_sha256: str):
                result = real_safe_extract_main_asset(asset, destination, expected_sha256)
                package_dir_holder["path"] = result[0]
                return result

            real_lstat = Path.lstat
            triggered = {"done": False}

            def hooked_lstat(self: Path):
                result = real_lstat(self)
                expected = package_dir_holder.get("path")
                if (
                    expected is not None
                    and not triggered["done"]
                    and self == expected / "dist"
                    and stat.S_ISDIR(result.st_mode)
                ):
                    triggered["done"] = True
                    # The instant after this exact validating lstat() call
                    # captured a valid-directory result (returned below,
                    # unchanged), a same-UID racer removes the directory
                    # and drops a symlink to an outside directory in its
                    # place -- before any LATER, separate lookup of the
                    # same path (e.g. archive.add()'s own internal
                    # os.lstat()) would run.
                    self.rmdir()
                    self.symlink_to(outside_target)
                return result

            output_name = "prime-agent-orca-pinned-dir-swap-test.tgz"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(
                    installer,
                    "safe_extract_main_asset",
                    side_effect=learn_package_dir,
                ),
                mock.patch.object(Path, "lstat", hooked_lstat),
            ):
                patched, _digest, _manifest, _identity, _content_digests = installer.make_patched_asset(
                    original_asset,
                    installer.sha256_file(original_asset),
                    {"packages": {}},
                    assets,
                    expected_name="prime-agent",
                    managed_name="prime-agent",
                    output_name=output_name,
                )

            with tarfile.open(patched, "r:gz") as published:
                member = published.getmember("package/dist")
            # The published archive must still contain the correct DIRTYPE
            # member -- never a symlink, regardless of what the same-UID
            # racer swapped the on-disk path to in the meantime.
            self.assertTrue(member.isdir())
            self.assertFalse(member.issym())
            self.assertEqual(member.linkname, "")
            # The attacker's outside directory must never have been touched
            # or read by the archiving loop.
            self.assertEqual(
                (outside_target / "sentinel").read_bytes(), b"do-not-touch"
            )
            # The fixed directory branch performs no path-based lstat() of
            # its own at all -- confirm the hook (anchored to exactly the
            # lstat() call the pre-fix code made here) genuinely never
            # fired, rather than this test accidentally passing because the
            # swap silently failed for an unrelated reason.
            self.assertFalse(triggered["done"])

    def test_atomic_symlink_ancestor_swap_cannot_escape_verified_parent(self) -> None:
        # Regression for P1-2: ensure_local_link_parent() used to verify the
        # immediate parent purely by path, and atomic_symlink() then
        # re-resolved that same parent by path a second time to actually
        # create the link, with no descriptor held open across the gap. A
        # same-UID racer that swapped the verified parent for a symlink to an
        # outside directory in that window made atomic_symlink() create the
        # public command link inside the attacker's directory instead.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            target = ssd / "target"
            target.parent.mkdir(mode=0o700)
            target.write_text("target", encoding="utf-8")
            user_home = root / "user"
            real_bin = user_home / ".local/bin"
            real_bin.mkdir(parents=True, mode=0o700)
            link = real_bin / "prime-agent"
            escape = root / "escape"
            escape.mkdir(mode=0o700)
            relocated = real_bin.with_name("bin-relocated-by-race")

            real_ensure_local_link_parent = installer.ensure_local_link_parent

            def race_after_verification(candidate_link: Path) -> int:
                descriptor = real_ensure_local_link_parent(candidate_link)
                # The instant after the immediate parent (real_bin) is
                # verified and its descriptor is held open, a same-UID racer
                # relocates it and drops a symlink to an outside directory in
                # its place -- exactly the P1-2 repro.
                real_bin.rename(relocated)
                real_bin.symlink_to(escape, target_is_directory=True)
                return descriptor

            with (
                mock.patch.object(installer, "SSD_ROOT", ssd),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(
                    installer,
                    "ensure_local_link_parent",
                    side_effect=race_after_verification,
                ),
            ):
                installer.atomic_symlink(target, link)

            # The link must land inside the directory that was actually
            # verified and held open (its relocated real location) -- never
            # inside the attacker's escape directory, regardless of what the
            # lexical path user/.local/bin resolves to by the time the link
            # is created.
            self.assertFalse((escape / "prime-agent").exists())
            self.assertFalse((escape / "prime-agent").is_symlink())
            self.assertTrue(real_bin.is_symlink())
            self.assertEqual(os.readlink(real_bin), os.fspath(escape))
            relocated_link = relocated / "prime-agent"
            self.assertTrue(relocated_link.is_symlink())
            self.assertEqual(os.readlink(relocated_link), os.fspath(target))

    def test_atomic_symlink_deeper_ancestor_swap_cannot_escape_verified_parent(
        self,
    ) -> None:
        # Regression for P1-2 residual, creation side (independent review
        # round 1, 2026-08-18, finding (b)): the prior fix pinned only the
        # IMMEDIATE parent (~/.local/bin) with O_NOFOLLOW|O_DIRECTORY, but
        # reached it by first walking every EARLIER ancestor (~/.local)
        # purely lexically -- lstat-checked, then re-resolved by path
        # string a moment later. A same-UID racer that swapped ~/.local
        # itself for a symlink to an outside directory in that window could
        # still redirect the eventual immediate-parent open, and therefore
        # the created link, into the attacker's directory -- one level
        # above where the previous regression test's race lands.
        # ensure_local_link_parent() now opens EVERY ancestor relative to
        # the descriptor of the previously opened and verified ancestor
        # (dir_fd-chained all the way from USER_HOME), so this test swaps
        # ~/.local the instant after it is opened and verified but before
        # ~/.local/bin is opened relative to that held descriptor.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            target = ssd / "target"
            target.parent.mkdir(mode=0o700)
            target.write_text("target", encoding="utf-8")
            user_home = root / "user"
            real_local = user_home / ".local"
            real_local.mkdir(parents=True, mode=0o700)
            link = real_local / "bin/prime-agent"
            escape = root / "escape"
            escape.mkdir(mode=0o700)
            relocated = real_local.with_name(".local-relocated-by-race")

            real_open_component = installer.open_verified_directory_component

            def race_between_ancestors(
                parent_descriptor: int,
                name: str,
                display_path: Path,
                *,
                create_missing: bool,
            ) -> int:
                descriptor = real_open_component(
                    parent_descriptor, name, display_path, create_missing=create_missing
                )
                if name == ".local":
                    # ~/.local itself has just been opened and verified, and
                    # its descriptor is held open -- the instant before
                    # ~/.local/bin is opened relative to it, a same-UID
                    # racer relocates ~/.local and drops a symlink to an
                    # outside directory in its place.
                    real_local.rename(relocated)
                    real_local.symlink_to(escape, target_is_directory=True)
                return descriptor

            with (
                mock.patch.object(installer, "SSD_ROOT", ssd),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(
                    installer,
                    "open_verified_directory_component",
                    side_effect=race_between_ancestors,
                ),
            ):
                installer.atomic_symlink(target, link)

            # bin/prime-agent must never be created under the attacker's
            # escape directory, regardless of what the lexical path
            # user/.local resolves to by the time ~/.local/bin is opened.
            self.assertFalse((escape / "bin").exists())
            self.assertFalse((escape / "bin").is_symlink())
            self.assertTrue(real_local.is_symlink())
            self.assertEqual(os.readlink(real_local), os.fspath(escape))
            relocated_link = relocated / "bin/prime-agent"
            self.assertTrue(relocated_link.is_symlink())
            self.assertEqual(os.readlink(relocated_link), os.fspath(target))

    def test_verify_link_rejects_interposed_ancestor_symlink(self) -> None:
        # Regression for P1-2 residual, verification side (independent
        # review round 1, 2026-08-18, finding (a) -- the reason for the
        # prior round's NO_GO). This needs NO race at all: verify_link()
        # used to resolve the link purely via its own lexical path
        # (link.lstat(), os.readlink(link)), with no check that the link's
        # ancestors were still the real, non-symlinked USER_HOME structure.
        # Renaming ~/.local aside and dropping a symlink to an
        # attacker-owned directory that itself holds an honestly-shaped
        # bin/prime-agent -> <the same managed target> made verify_link()
        # -- and therefore verify_command_state() -- report success for a
        # command path whose real parent directory is entirely
        # attacker-controlled. Both must now fail closed.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            target = ssd / "target"
            target.parent.mkdir(mode=0o700)
            target.write_text("target", encoding="utf-8")
            user_home = root / "user"
            real_bin = user_home / ".local/bin"
            real_bin.mkdir(parents=True, mode=0o700)
            link = real_bin / "prime-agent"

            with (
                mock.patch.object(installer, "SSD_ROOT", ssd),
                mock.patch.object(installer, "USER_HOME", user_home),
            ):
                installer.atomic_symlink(target, link)
                # Sanity: the honestly created link verifies before the
                # ancestor is interposed.
                installer.verify_link(link, target)

                real_local = user_home / ".local"
                relocated = real_local.with_name(".local-relocated")
                real_local.rename(relocated)
                attacker_dir = root / "attacker"
                attacker_bin = attacker_dir / "bin"
                attacker_bin.mkdir(parents=True, mode=0o700)
                (attacker_bin / "prime-agent").symlink_to(target)
                real_local.symlink_to(attacker_dir, target_is_directory=True)

                # The lexical path user/.local/bin/prime-agent now resolves,
                # via the interposed symlink, to the attacker's own
                # honestly-shaped bin/prime-agent -> target -- exactly what
                # verify_command_state() inspects. It must be refused, not
                # silently accepted, even though the leaf link's own target
                # text is byte-for-byte identical to the legitimate one.
                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "link parent is missing|unsafe link parent|"
                    "cannot inspect link parent",
                ):
                    installer.verify_link(link, target)

                receipt = {"bin_target": os.fspath(target)}
                with (
                    mock.patch.object(installer, "BIN_LINK", link),
                    mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
                ):
                    with self.assertRaisesRegex(
                        installer.PrimeInstallError,
                        "link parent is missing|unsafe link parent|"
                        "cannot inspect link parent",
                    ):
                        installer.verify_command_state(receipt)

            # The attacker's own files must be untouched, and the real
            # (relocated) link must still be exactly what was honestly
            # created.
            self.assertEqual(
                os.readlink(attacker_bin / "prime-agent"), os.fspath(target)
            )
            real_relocated_link = relocated / "bin/prime-agent"
            self.assertTrue(real_relocated_link.is_symlink())
            self.assertEqual(os.readlink(real_relocated_link), os.fspath(target))

    def test_remove_private_file_durable_detects_reoccupied_original_path(self) -> None:
        # Regression for P1-3: after rename_noreplace() moved the original
        # file into quarantine, remove_private_file_durable() used to verify
        # only the quarantine copy and never re-check that the original
        # lexical path was actually left absent -- so a benign race with
        # another managed step, or a hostile same-UID actor, recreating a
        # file at that exact path immediately after it was vacated went
        # completely undetected and the function still reported success.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            parent = root / "private"
            parent.mkdir(mode=0o700)
            path = parent / "pending.json"
            raw = b"managed pending payload\n"
            installer.atomic_write(path, raw, 0o600)

            original_rename = installer.rename_noreplace
            reoccupied = False

            def insert_reoccupant_after_rename(source: Path, destination: Path) -> None:
                nonlocal reoccupied
                original_rename(source, destination)
                if source == path and not reoccupied:
                    reoccupied = True
                    source.write_text("reoccupant", encoding="utf-8")
                    os.chmod(source, 0o600)

            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(
                    installer,
                    "rename_noreplace",
                    side_effect=insert_reoccupant_after_rename,
                ),
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "re-occupied"
                ):
                    installer.remove_private_file_durable(path, raw)
            # The re-occupant must be left exactly as the race created it,
            # and the quarantine copy must be retained (not silently deleted)
            # once a re-occupation is detected, so the incident is inspectable.
            self.assertTrue(path.is_file())
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_text(encoding="utf-8"), "reoccupant")
            quarantine_candidates = list(parent.glob(f".{path.name}.remove-*"))
            self.assertEqual(len(quarantine_candidates), 1)
            self.assertEqual(quarantine_candidates[0].read_bytes(), raw)

    def test_sandbox_e2e_main_refuses_under_pythonoptimize(self) -> None:
        # Regression for P1-4 (belt-and-suspenders half): sandbox_e2e.py's
        # lifecycle-acceptance body used bare `assert` statements, which
        # `python -O` / `PYTHONOPTIMIZE=1` strip entirely -- so a real
        # lifecycle failure could previously report a pass under those common
        # environment settings. main() must now refuse to run at all under
        # __debug__ is False, before any lifecycle logic executes.
        project_root = Path(installer.__file__).resolve().parent
        sandbox_script = project_root / "tests" / "sandbox_e2e.py"
        env = dict(os.environ)
        env["PYTHONOPTIMIZE"] = "1"
        result = subprocess.run(
            [sys.executable, os.fspath(sandbox_script)],
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        self.assertEqual(result.returncode, 4)
        payload = json.loads(result.stdout)
        self.assertIs(payload["ok"], False)
        self.assertIn("PYTHONOPTIMIZE", payload["error"])

    def test_sandbox_e2e_require_survives_pythonoptimize_with_failing_lifecycle_mock(
        self,
    ) -> None:
        # Regression for P1-4 (core half): proves the hardened lifecycle
        # checks themselves -- not just main()'s outer guard -- still catch a
        # genuinely failing/mocked lifecycle result under `python -O`. Calls
        # sandbox_e2e.run() directly (bypassing main()'s own __debug__ guard)
        # with install() mocked to return a receipt a real install would
        # never produce (command_default_enabled=True), and confirms this
        # still fails loudly instead of silently reporting a pass -- which a
        # bare `assert` would have silently allowed under -O.
        project_root = Path(installer.__file__).resolve().parent
        driver = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {os.fspath(project_root)!r})
            sys.path.insert(0, {os.fspath(project_root / "tests")!r})
            from unittest import mock
            import install_prime_agent as installer
            import sandbox_e2e

            fake_receipt = {{
                "command_default_enabled": True,
                "session_dir": "/nonexistent-session-dir",
                "license_sha256": installer.LICENSE_SHA256,
                "bin_target": "/usr/bin/true",
                "production_lock_sha256": "0" * 64,
                "release_tree_sha256": "0" * 64,
                "release_tree_entries": 0,
            }}
            with mock.patch.object(installer, "install", return_value=fake_receipt):
                sandbox_e2e.run(None)
            """
        )
        result = subprocess.run(
            [sys.executable, "-O", "-c", driver],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AssertionError", result.stderr)
        self.assertIn("command_default_enabled", result.stderr)
        self.assertNotIn('"ok": true', result.stdout)

    def test_remove_exact_symlink_ancestor_swap_cannot_escape_verified_parent(
        self,
    ) -> None:
        # Regression for independent review round 3/4, 2026-08-18, P1
        # finding A (residual of P1-2): remove_exact_symlink() used to
        # lstat/readlink/rename the public command link entirely by lexical
        # path (path.lstat(), os.readlink(path), rename_noreplace(path,
        # quarantine)), with no binding to the SAME dir_fd-chained ancestor
        # descriptor creation (atomic_symlink() -> ensure_local_link_parent())
        # and verification (verify_link() -> verify_link_parent_descriptor())
        # already use. A same-UID racer that swapped the verified parent for
        # a symlink to an outside directory in the window between removal's
        # own ancestor-chain verification and its leaf lstat/rename/unlink
        # steps could make removal follow the interposed symlink instead of
        # the real, held-open parent -- the removal-side counterpart of the
        # creation-side race already refused by
        # test_atomic_symlink_ancestor_swap_cannot_escape_verified_parent.
        # remove_exact_symlink() must now stay bound to the real, relocated
        # directory (via open_verified_ancestor_chain()'s returned
        # descriptor) regardless of what the lexical parent path resolves to
        # by the time the leaf steps run.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            target = ssd / "target"
            target.parent.mkdir(mode=0o700)
            target.write_text("target", encoding="utf-8")
            user_home = root / "user"
            real_bin = user_home / ".local/bin"
            real_bin.mkdir(parents=True, mode=0o700)
            link = real_bin / "prime-agent"
            escape = root / "escape"
            escape.mkdir(mode=0o700)
            relocated = real_bin.with_name("bin-relocated-by-race")

            with (
                mock.patch.object(installer, "SSD_ROOT", ssd),
                mock.patch.object(installer, "USER_HOME", user_home),
            ):
                installer.atomic_symlink(target, link)

                real_open_ancestor_chain = installer.open_verified_ancestor_chain

                def race_after_removal_verification(
                    candidate_link: Path, *, create_missing: bool
                ) -> int:
                    descriptor = real_open_ancestor_chain(
                        candidate_link, create_missing=create_missing
                    )
                    if not create_missing:
                        # remove_exact_symlink() calls with
                        # create_missing=False. The instant after the
                        # immediate parent (real_bin) is verified for
                        # REMOVAL and its descriptor is held open, a
                        # same-UID racer relocates it and drops a symlink
                        # to an outside directory in its place.
                        real_bin.rename(relocated)
                        real_bin.symlink_to(escape, target_is_directory=True)
                    return descriptor

                with mock.patch.object(
                    installer,
                    "open_verified_ancestor_chain",
                    side_effect=race_after_removal_verification,
                ):
                    installer.remove_exact_symlink(link, target)

            # The real link must have been removed from the directory that
            # was actually verified and held open (its relocated real
            # location) -- the attacker's escape directory must never have
            # been touched, regardless of what the lexical path
            # user/.local/bin resolves to by the time removal's leaf steps
            # run.
            self.assertFalse((escape / "prime-agent").exists())
            self.assertFalse((escape / "prime-agent").is_symlink())
            self.assertEqual(sorted(p.name for p in escape.iterdir()), [])
            self.assertTrue(real_bin.is_symlink())
            self.assertEqual(os.readlink(real_bin), os.fspath(escape))
            relocated_link = relocated / "prime-agent"
            self.assertFalse(relocated_link.exists())
            self.assertFalse(relocated_link.is_symlink())
            # No quarantine leftovers under the relocated real directory.
            self.assertEqual(sorted(p.name for p in relocated.iterdir()), [])

    def test_daemon_socket_agents_attach_require_effective_project_gate(
        self,
    ) -> None:
        # Regression for independent review round 3/4, 2026-08-18, P1
        # finding B: managed_entrypoint_script() only remapped a leading
        # "--daemon-socket <value>" pair's managed_command to the true
        # command token ($3) for the two commands stop/rename, so
        # "--daemon-socket <sock> agents"/"... attach <session>" never
        # matched the agents/attach effective-project deny gate and ran
        # with no ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS=1 requirement.
        # Companion P2 in the same finding: because session_start was
        # therefore also miscomputed for every OTHER public command
        # (config, doctor, help, list, package, schedule, send, session,
        # shutdown, status) reached via --daemon-socket, resource-guard
        # flags could land between the socket value and the command token
        # for those forms. Both are fixed together: the entrypoint now
        # remaps managed_command generically for every public command form,
        # and guarded_arguments() carries its own defense-in-depth
        # recognition of the non-session public commands so it never
        # inserts guards before one of them even if RESOURCE_GUARD_ENV
        # somehow reached it anyway.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "release"
            bin_dir = release / "bin"
            bin_dir.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root, 0o700)
            os.chmod(release, 0o700)
            node = bin_dir / "node"
            cli = release / "cli.js"
            node.write_text(
                "#!/usr/bin/python3\n"
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "  'argv': sys.argv[1:],\n"
                "  'resourceGuard': os.environ.get('ORCA_PRIME_AGENT_RESOURCE_GUARD'),\n"
                "}))\n",
                encoding="utf-8",
            )
            os.chmod(node, 0o700)
            cli.write_text("// argument sentinel\n", encoding="utf-8")
            os.chmod(cli, 0o600)
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            guard = bin_dir / "prime-agent-launch-guard.py"
            wrapper = bin_dir / "prime-agent"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
            ):
                guard.write_bytes(
                    installer.managed_launch_guard_script(
                        node,
                        cli,
                        installer.sha256_file(node),
                        installer.sha256_file(cli),
                    )
                )
                wrapper.write_bytes(
                    installer.managed_entrypoint_script(node, cli, guard)
                )
            os.chmod(guard, 0o700)
            os.chmod(wrapper, 0o700)
            clean = root / "clean"
            clean.mkdir(mode=0o700)
            daemon_socket = os.fspath(root / "daemon.sock")

            def run_wrapper(
                arguments: tuple[str, ...], *, allow_settings: bool = False
            ) -> subprocess.CompletedProcess[str]:
                environment = {
                    "HOME": os.fspath(root),
                    "PATH": "/usr/bin:/bin",
                    "ORCA_PRIME_AGENT_RESOURCE_GUARD": "1",
                }
                if allow_settings:
                    environment["ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS"] = "1"
                return subprocess.run(
                    [os.fspath(wrapper), *arguments],
                    cwd=clean,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                )

            for arguments in (
                ("--daemon-socket", daemon_socket, "agents"),
                ("--daemon-socket", daemon_socket, "attach", "saved-session"),
            ):
                with self.subTest(arguments=arguments, mode="daemon-socket-default-block"):
                    result = run_wrapper(arguments)
                    self.assertEqual(result.returncode, 78)
                    self.assertIn("effective-project settings review", result.stderr)

            # Explicit opt-in: now succeeds. Guard-flag placement changed in
            # round 6, 2026-08-18 (see resolve_upstream_public_command()):
            # upstream's own normalizeLeadingDaemonSocketOption() never
            # remaps a bare "--daemon-socket <value>" pair followed by
            # "agents" (only stop/rename), so upstream actually treats this
            # exact invocation as a real session start, not the "agents"
            # command -- guards therefore land right after the (unstripped,
            # from upstream's point of view) daemon-socket pair, not after
            # "agents saved-session" the way a form upstream genuinely
            # resolves to "agents" would place them (see the bare, no-prefix
            # "agents"/"attach" case in
            # test_wrapper_guards_runtime_commands_and_effective_project,
            # which is unaffected by this and still places guards after the
            # command).
            allowed = run_wrapper(
                ("--daemon-socket", daemon_socket, "agents", "saved-session"),
                allow_settings=True,
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            allowed_payload = installer.strict_json(allowed.stdout.encode("utf-8"))
            self.assertIsInstance(allowed_payload, dict)
            assert isinstance(allowed_payload, dict)
            self.assertEqual(
                allowed_payload["argv"][1:],
                [
                    "--daemon-socket",
                    daemon_socket,
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                    "agents",
                    "saved-session",
                ],
            )

            # A non-stop/rename public command (config) reached via a bare
            # "--daemon-socket <value>" pair is exactly the round-6 P1: real
            # upstream does NOT remap this to "config" (only stop/rename),
            # so upstream actually starts a real session here -- this
            # invocation now correctly receives resource-guard flags (it did
            # NOT before round 6, which was the bug: session_start was
            # wrongly computed as 0, so ORCA_PRIME_AGENT_RESOURCE_GUARD was
            # never even exported for the launch guard to see). Because
            # `clean` has no .prime/agent/settings.json, the separate
            # settings-file gate does not fire either way, so rc stays 0 --
            # the observable difference is the inserted guard flags below.
            config_result = run_wrapper(
                ("--daemon-socket", daemon_socket, "config", "get", "x")
            )
            self.assertEqual(config_result.returncode, 0, config_result.stderr)
            config_payload = installer.strict_json(
                config_result.stdout.encode("utf-8")
            )
            self.assertIsInstance(config_payload, dict)
            assert isinstance(config_payload, dict)
            self.assertEqual(
                config_payload["argv"][1:],
                [
                    "--daemon-socket",
                    daemon_socket,
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                    "config",
                    "get",
                    "x",
                ],
            )
            # The launch guard always consumes (os.environ.pop) its own
            # signal env var before exec'ing the real CLI, so it never
            # leaks through regardless of whether guards were inserted.
            self.assertIsNone(config_payload["resourceGuard"])

            # A bare "config" with NO daemon-socket prefix at all is
            # genuinely, unambiguously resolved by upstream as the "config"
            # public command (there is nothing here for
            # normalizeLeadingDaemonSocketOption() to even consider) -- this
            # must still receive NO resource-guard flags, exactly as before
            # round 6. As of round 8, "config" is no longer in the shell
            # entrypoint's public_commands (see managed_entrypoint_script()),
            # so session_start is now 1 here rather than 0 -- but `clean` has
            # no .prime/agent/settings.json, so the settings gate this test
            # isn't exercising still never fires, and "config" remains in
            # RUNTIME_NO_GUARD_COMMANDS on the Python launch-guard side (see
            # its own comment for why that's an intentionally separate
            # question from public_commands membership), so guarded_
            # arguments() still never splices RESOURCE_GUARDS into its argv
            # either. Both observable assertions below are therefore
            # unchanged by round 8; see
            # test_config_package_help_require_session_start_protection_by_default
            # for the settings-gate behavior change itself.
            bare_config_result = run_wrapper(("config", "get", "x"))
            self.assertEqual(bare_config_result.returncode, 0, bare_config_result.stderr)
            bare_config_payload = installer.strict_json(
                bare_config_result.stdout.encode("utf-8")
            )
            self.assertIsInstance(bare_config_payload, dict)
            assert isinstance(bare_config_payload, dict)
            self.assertEqual(
                bare_config_payload["argv"][1:], ["config", "get", "x"]
            )
            self.assertIsNone(bare_config_payload["resourceGuard"])

            # The genuinely upstream-recognized bare "--daemon-socket
            # <value> stop <id>" form is the one case upstream's own
            # normalizeLeadingDaemonSocketOption() DOES remap -- this must
            # still receive NO resource-guard flags and no effective-project
            # gate, unaffected by round 6.
            stop_result = run_wrapper(
                ("--daemon-socket", daemon_socket, "stop", "agent-id")
            )
            self.assertEqual(stop_result.returncode, 0, stop_result.stderr)
            stop_payload = installer.strict_json(stop_result.stdout.encode("utf-8"))
            self.assertIsInstance(stop_payload, dict)
            assert isinstance(stop_payload, dict)
            self.assertEqual(
                stop_payload["argv"][1:],
                ["--daemon-socket", daemon_socket, "stop", "agent-id"],
            )
            self.assertIsNone(stop_payload["resourceGuard"])

    def test_leading_option_parser_covers_update_bypass_and_daemon_socket_forms(
        self,
    ) -> None:
        # Regression for independent review round 5, 2026-08-18 (fixing
        # round 4's partial fix): the entrypoint's command-token detection
        # was ad hoc pattern matching on a fixed argv position/form
        # ("$1 == '--daemon-socket'" then take "$3", space-separated only),
        # not a real "skip every recognized leading option, take the first
        # non-option token" parse. That left THREE bugs: (P1-1) the
        # self-update block checked bare "${1-}" directly, never the
        # resolved command token at all, so
        # "--daemon-socket <sock> update" was never blocked; (P1-2) the
        # "--daemon-socket=<value>" single-token form and a
        # repeated/duplicate "--daemon-socket" flag were not recognized by
        # either the shell entrypoint or the Python launch guard, so those
        # forms bypassed the agents/attach effective-project gate; (P2-1)
        # guard-flag insertion for those same unrecognized forms landed in
        # the wrong place. Both generated scripts now share ONE parsing
        # function apiece (resolve_managed_command() / split_leading_options())
        # driven by the same LEADING_COMMAND_OPTIONS constant. Verified
        # here with real generated scripts run as real subprocesses -- not
        # by reading the script text.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "release"
            bin_dir = release / "bin"
            bin_dir.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root, 0o700)
            os.chmod(release, 0o700)
            node = bin_dir / "node"
            cli = release / "cli.js"
            node.write_text(
                "#!/usr/bin/python3\n"
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "  'argv': sys.argv[1:],\n"
                "  'resourceGuard': os.environ.get('ORCA_PRIME_AGENT_RESOURCE_GUARD'),\n"
                "}))\n",
                encoding="utf-8",
            )
            os.chmod(node, 0o700)
            cli.write_text("// argument sentinel\n", encoding="utf-8")
            os.chmod(cli, 0o600)
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            guard = bin_dir / "prime-agent-launch-guard.py"
            wrapper = bin_dir / "prime-agent"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
            ):
                guard.write_bytes(
                    installer.managed_launch_guard_script(
                        node,
                        cli,
                        installer.sha256_file(node),
                        installer.sha256_file(cli),
                    )
                )
                wrapper.write_bytes(
                    installer.managed_entrypoint_script(node, cli, guard)
                )
            os.chmod(guard, 0o700)
            os.chmod(wrapper, 0o700)
            clean = root / "clean"
            clean.mkdir(mode=0o700)
            daemon_socket = os.fspath(root / "daemon.sock")
            other_socket = os.fspath(root / "daemon2.sock")

            def run_wrapper(
                arguments: tuple[str, ...], *, allow_settings: bool = False
            ) -> subprocess.CompletedProcess[str]:
                environment = {
                    "HOME": os.fspath(root),
                    "PATH": "/usr/bin:/bin",
                    "ORCA_PRIME_AGENT_RESOURCE_GUARD": "1",
                }
                if allow_settings:
                    environment["ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS"] = "1"
                return subprocess.run(
                    [os.fspath(wrapper), *arguments],
                    cwd=clean,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                )

            # Self-update block: bare, space-separated daemon-socket, and
            # "="-form daemon-socket must ALL be blocked with rc 64.
            for arguments in (
                ("update",),
                ("--daemon-socket", daemon_socket, "update"),
                (f"--daemon-socket={daemon_socket}", "update"),
            ):
                with self.subTest(arguments=arguments, mode="update-block"):
                    result = run_wrapper(arguments)
                    self.assertEqual(result.returncode, 64, result.stderr)
                    self.assertIn("self-update is disabled", result.stderr)

            # agents/attach effective-project gate: bare, space-separated,
            # "="-form, and repeated/duplicate daemon-socket must ALL be
            # blocked with rc 78 absent the opt-in env var.
            for arguments in (
                ("agents",),
                ("attach", "saved-session"),
                ("--daemon-socket", daemon_socket, "agents"),
                (f"--daemon-socket={daemon_socket}", "agents"),
                (f"--daemon-socket={daemon_socket}", "attach", "saved-session"),
                (
                    "--daemon-socket",
                    daemon_socket,
                    "--daemon-socket",
                    other_socket,
                    "agents",
                ),
            ):
                with self.subTest(arguments=arguments, mode="agents-attach-block"):
                    result = run_wrapper(arguments)
                    self.assertEqual(result.returncode, 78, result.stderr)
                    self.assertIn("effective-project settings review", result.stderr)

            # Guard-flag placement: with explicit opt-in, both requests
            # succeed and resource-guard flags are present. Their exact
            # position changed in round 6, 2026-08-18 (see
            # resolve_upstream_public_command()): real upstream's own
            # normalizeLeadingDaemonSocketOption() never recognizes the "="
            # form at all, and never recognizes a repeated/second
            # "--daemon-socket" occurrence either -- both forms are actually
            # treated by upstream as a real session start (not the "agents"
            # command), so guards now land right after the leading
            # daemon-socket token(s) rather than after "agents extra", to
            # match where a real session-start invocation with those same
            # leading tokens would want them (see the bare, no-prefix
            # "agents"/"attach" case in
            # test_wrapper_guards_runtime_commands_and_effective_project,
            # which IS genuinely resolved as "agents" by upstream and still
            # places guards after the command there).
            equals_allowed = run_wrapper(
                (f"--daemon-socket={daemon_socket}", "agents", "extra"),
                allow_settings=True,
            )
            self.assertEqual(equals_allowed.returncode, 0, equals_allowed.stderr)
            equals_payload = installer.strict_json(
                equals_allowed.stdout.encode("utf-8")
            )
            self.assertIsInstance(equals_payload, dict)
            assert isinstance(equals_payload, dict)
            self.assertEqual(
                equals_payload["argv"][1:],
                [
                    f"--daemon-socket={daemon_socket}",
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                    "agents",
                    "extra",
                ],
            )

            repeated_allowed = run_wrapper(
                (
                    "--daemon-socket",
                    daemon_socket,
                    "--daemon-socket",
                    other_socket,
                    "agents",
                    "extra",
                ),
                allow_settings=True,
            )
            self.assertEqual(repeated_allowed.returncode, 0, repeated_allowed.stderr)
            repeated_payload = installer.strict_json(
                repeated_allowed.stdout.encode("utf-8")
            )
            self.assertIsInstance(repeated_payload, dict)
            assert isinstance(repeated_payload, dict)
            self.assertEqual(
                repeated_payload["argv"][1:],
                [
                    "--daemon-socket",
                    daemon_socket,
                    "--daemon-socket",
                    other_socket,
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                    "agents",
                    "extra",
                ],
            )

    def test_daemon_socket_session_start_matches_upstream_normalization(
        self,
    ) -> None:
        # Regression for independent dual review round 6, 2026-08-18, P1
        # (found independently by a Claude opus/max review and a separate
        # Codex QA pass): round 5's generic --daemon-socket leading-option
        # parsing correctly resolved managed_command for forms like
        # "--daemon-socket=<v> status", but the wrapper's OWN session_start
        # gate then treated that resolution as an already-resolved,
        # protection-free public command. Real upstream (see
        # dist/cli/public-command.js's normalizeLeadingDaemonSocketOption(),
        # vendored bundle chunk-CAY2X72A.js around lines 17324-17436, read
        # directly to confirm this) only ever remaps a bare, space-separated
        # "--daemon-socket <value>" pair, and only when followed by exactly
        # "stop" or "rename" -- the "=" form is never recognized at all, a
        # repeated occurrence is never recognized, and a bare pair followed
        # by any OTHER command (status, list, config, ...) is left
        # completely alone and upstream actually starts a real interactive
        # session in $PWD. This test proves the settings-file gate --
        # session_start's real security consequence, not just argv shape --
        # now fires for exactly those forms it previously silently skipped,
        # for more than one public command, in both the space and "="
        # forms, while the one form upstream genuinely recognizes
        # (--daemon-socket <value> stop/rename) and the no-prefix form both
        # remain fast-tracked exactly as before.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "release"
            bin_dir = release / "bin"
            bin_dir.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root, 0o700)
            os.chmod(release, 0o700)
            node = bin_dir / "node"
            cli = release / "cli.js"
            node.write_text(
                "#!/usr/bin/python3\n"
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "  'argv': sys.argv[1:],\n"
                "  'resourceGuard': os.environ.get('ORCA_PRIME_AGENT_RESOURCE_GUARD'),\n"
                "}))\n",
                encoding="utf-8",
            )
            os.chmod(node, 0o700)
            cli.write_text("// argument sentinel\n", encoding="utf-8")
            os.chmod(cli, 0o600)
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            guard = bin_dir / "prime-agent-launch-guard.py"
            wrapper = bin_dir / "prime-agent"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
            ):
                guard.write_bytes(
                    installer.managed_launch_guard_script(
                        node,
                        cli,
                        installer.sha256_file(node),
                        installer.sha256_file(cli),
                    )
                )
                wrapper.write_bytes(
                    installer.managed_entrypoint_script(node, cli, guard)
                )
            os.chmod(guard, 0o700)
            os.chmod(wrapper, 0o700)
            clean = root / "clean"
            project = root / "project"
            clean.mkdir(mode=0o700)
            (project / ".prime/agent").mkdir(parents=True, mode=0o700)
            (project / ".prime/agent/settings.json").write_text(
                "{}", encoding="utf-8"
            )
            daemon_socket = os.fspath(root / "daemon.sock")
            other_socket = os.fspath(root / "daemon2.sock")

            def run_wrapper(
                arguments: tuple[str, ...],
                *,
                cwd: Path,
                allow_settings: bool = False,
            ) -> subprocess.CompletedProcess[str]:
                environment = {
                    "HOME": os.fspath(root),
                    "PATH": "/usr/bin:/bin",
                    "ORCA_PRIME_AGENT_RESOURCE_GUARD": "1",
                }
                if allow_settings:
                    environment["ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS"] = "1"
                return subprocess.run(
                    [os.fspath(wrapper), *arguments],
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                )

            # (a) Baseline/sanity: a bare public command with NO
            # daemon-socket prefix at all is genuinely, unambiguously
            # resolved by upstream, so the settings gate correctly never
            # applies to it, in project cwd, unaffected by round 6.
            for bare_arguments in (("status",), ("list",)):
                with self.subTest(arguments=bare_arguments, mode="bare-command-unblocked"):
                    result = run_wrapper(bare_arguments, cwd=project)
                    self.assertEqual(result.returncode, 0, result.stderr)

            # (b)-(d) THE FIX: a bare-space-form, an "="-form, and a
            # repeated-flag daemon-socket prefix in front of a non-stop/
            # rename public command must now ALL be treated as a protected
            # session start -- the settings gate must fire in project cwd,
            # for more than one command. Before round 6 every one of these
            # incorrectly returned rc 0 (the bug this test proves fixed).
            blocked_cases = (
                ("--daemon-socket", daemon_socket, "status"),
                (f"--daemon-socket={daemon_socket}", "status"),
                ("--daemon-socket", daemon_socket, "--daemon-socket", other_socket, "status"),
                ("--daemon-socket", daemon_socket, "list"),
                (f"--daemon-socket={daemon_socket}", "list"),
            )
            for arguments in blocked_cases:
                with self.subTest(arguments=arguments, mode="session-start-now-protected"):
                    result = run_wrapper(arguments, cwd=project)
                    self.assertEqual(result.returncode, 78, result.stderr)
                    self.assertIn(
                        "project .prime/agent/settings.json", result.stderr
                    )

            # Explicit opt-in bypasses the settings gate for these same
            # forms, proving the block above is really session_start's
            # gate firing (and end-to-end plumbing all the way through),
            # not some unrelated failure.
            opted_in = run_wrapper(
                ("--daemon-socket", daemon_socket, "status"),
                cwd=project,
                allow_settings=True,
            )
            self.assertEqual(opted_in.returncode, 0, opted_in.stderr)

            # (e) The one form upstream's own normalizeLeadingDaemonSocketOption()
            # genuinely recognizes (bare space form + stop/rename) remains
            # fast-tracked -- no settings gate -- exactly as before round 6.
            stop_result = run_wrapper(
                ("--daemon-socket", daemon_socket, "stop", "agent-id"), cwd=project
            )
            self.assertEqual(stop_result.returncode, 0, stop_result.stderr)

            # (f)-(g) End-to-end proof at the argv level, in a cwd with no
            # project settings file to short-circuit on: the daemon-socket
            # -prefixed, non-stop/rename form now receives resource-guard
            # flags (it did not before round 6, because
            # ORCA_PRIME_AGENT_RESOURCE_GUARD was never even exported), and
            # the equivalent bare, no-prefix form still receives none.
            guarded = run_wrapper(
                ("--daemon-socket", daemon_socket, "status"), cwd=clean
            )
            self.assertEqual(guarded.returncode, 0, guarded.stderr)
            guarded_payload = installer.strict_json(guarded.stdout.encode("utf-8"))
            self.assertIsInstance(guarded_payload, dict)
            assert isinstance(guarded_payload, dict)
            self.assertEqual(
                guarded_payload["argv"][1:],
                [
                    "--daemon-socket",
                    daemon_socket,
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                    "status",
                ],
            )

            unguarded = run_wrapper(("status",), cwd=clean)
            self.assertEqual(unguarded.returncode, 0, unguarded.stderr)
            unguarded_payload = installer.strict_json(
                unguarded.stdout.encode("utf-8")
            )
            self.assertIsInstance(unguarded_payload, dict)
            assert isinstance(unguarded_payload, dict)
            self.assertEqual(unguarded_payload["argv"][1:], ["status"])

    def test_config_package_help_require_session_start_protection_by_default(
        self,
    ) -> None:
        # Regression for independent review round 7/8, 2026-08-18, P1 (the
        # highest-severity finding across every round to date): "config",
        # "package", and "help <argument>" were classified session_start=0
        # in managed_entrypoint_script()'s public_commands, skipping BOTH
        # the $PWD/.prime/agent/settings.json gate and
        # ORCA_PRIME_AGENT_RESOURCE_GUARD, even though upstream's real
        # handleConfigCommand()/handlePackageCommand() can still call
        # SettingsManager.create(process.cwd(), agentDir) (and "config"
        # additionally packageManager.resolve() with no onMissing guard),
        # and "help <argument>" falls through to a real, unprotected
        # session start for any argument upstream's own fuzzy help matcher
        # does not recognize. A prior round of independent review
        # reproduced this end-to-end with the real generated wrapper, real
        # generated launch guard, real pinned Node, and the real
        # prime-agent 0.7.2 bundle: a hostile .prime/agent/settings.json's
        # configured command ran via plain `prime-agent config`.
        #
        # This test exercises the SAME generated wrapper + launch guard as
        # real subprocesses (matching this file's established convention),
        # with a lightweight Python stand-in for "node" that reports argv
        # and the resource-guard env var, rather than the real upstream
        # bundle -- see the separate, real-upstream-bundle end-to-end
        # verification for proof that the underlying upstream RCE path
        # this closes is genuine, not merely a shape-level argv test.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "release"
            bin_dir = release / "bin"
            bin_dir.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root, 0o700)
            os.chmod(release, 0o700)
            node = bin_dir / "node"
            cli = release / "cli.js"
            node.write_text(
                "#!/usr/bin/python3\n"
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "  'argv': sys.argv[1:],\n"
                "  'resourceGuard': os.environ.get('ORCA_PRIME_AGENT_RESOURCE_GUARD'),\n"
                "}))\n",
                encoding="utf-8",
            )
            os.chmod(node, 0o700)
            cli.write_text("// argument sentinel\n", encoding="utf-8")
            os.chmod(cli, 0o600)
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            guard = bin_dir / "prime-agent-launch-guard.py"
            wrapper = bin_dir / "prime-agent"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
            ):
                guard.write_bytes(
                    installer.managed_launch_guard_script(
                        node,
                        cli,
                        installer.sha256_file(node),
                        installer.sha256_file(cli),
                    )
                )
                wrapper.write_bytes(
                    installer.managed_entrypoint_script(node, cli, guard)
                )
            os.chmod(guard, 0o700)
            os.chmod(wrapper, 0o700)
            clean = root / "clean"
            project = root / "project"
            clean.mkdir(mode=0o700)
            (project / ".prime/agent").mkdir(parents=True, mode=0o700)
            (project / ".prime/agent/settings.json").write_text(
                "{}", encoding="utf-8"
            )

            def run_wrapper(
                arguments: tuple[str, ...],
                *,
                cwd: Path,
                allow_settings: bool = False,
            ) -> subprocess.CompletedProcess[str]:
                environment = {
                    "HOME": os.fspath(root),
                    "PATH": "/usr/bin:/bin",
                    "ORCA_PRIME_AGENT_RESOURCE_GUARD": "1",
                }
                if allow_settings:
                    environment["ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS"] = "1"
                return subprocess.run(
                    [os.fspath(wrapper), *arguments],
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                )

            # (a) THE FIX: config, package, and help-with-an-argument must
            # now be blocked by the settings gate in a project carrying
            # .prime/agent/settings.json, absent the opt-in env var --
            # before round 8 every one of these incorrectly returned rc 0.
            blocked_cases = (
                ("config",),
                ("config", "get", "x"),
                ("package",),
                ("package", "list"),
                ("help", "mcp-servers"),
                ("help", "auth"),
                ("help", "tools", "list"),
            )
            for arguments in blocked_cases:
                with self.subTest(arguments=arguments, mode="now-protected"):
                    result = run_wrapper(arguments, cwd=project)
                    self.assertEqual(result.returncode, 78, result.stderr)
                    self.assertIn(
                        "project .prime/agent/settings.json", result.stderr
                    )

            # (b) Bare `help` -- no further argument at all -- is the one
            # form confirmed always safe upstream (isHelpCommandRequest()
            # returns true unconditionally for a zero-length path, checked
            # directly against dist/bundle/chunk-PMFPRFOT.js) and must stay
            # fast-tracked even in this same project.
            bare_help = run_wrapper(("help",), cwd=project)
            self.assertEqual(bare_help.returncode, 0, bare_help.stderr)

            # (c) Explicit opt-in restores the previous, unprotected
            # behavior for all three -- proving (a) is really session_
            # start's settings gate firing, not some unrelated failure.
            for arguments in (("config",), ("package",), ("help", "auth")):
                with self.subTest(arguments=arguments, mode="explicit-opt-in"):
                    result = run_wrapper(
                        arguments, cwd=project, allow_settings=True
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

            # (d) In a CLEAN cwd (no project settings file at all), config/
            # package/help<matched-topic> must still succeed, AND -- the
            # round-3/4 argv-corruption bug class this fix must not reopen
            # -- their argv must arrive at the underlying CLI COMPLETELY
            # UNCHANGED: confirms config/package remain in RUNTIME_NO_GUARD_
            # COMMANDS on the Python launch-guard side even though they
            # left public_commands on the shell side, so guarded_arguments()
            # never splices RESOURCE_GUARDS into their argv (a position
            # never verified safe for these two specific commands).
            #
            # "help", "auth" moved OUT of this list in round 10, 2026-08-18
            # (independent review, P1): "auth" is not a real command name
            # upstream's isHelpCommandRequest() recognizes (exact or fuzzy),
            # so this was actually a MISS case that upstream falls through
            # to a real, unprotected session start for -- see
            # test_help_argument_resource_guards_depend_on_upstream_match
            # below for full round-10 MISS-case coverage (this is exactly
            # the test-coverage gap round 9 flagged: this fixture never
            # built a no-settings.json project with a planted extension, so
            # this assertion's "argv is unchanged" claim for a MISS case
            # went unnoticed as actually being the bug, not the fix).
            # "help", "package" replaces it here as a genuine MATCH case
            # (upstream's own isHelpCommandRequest(["package"]) is
            # trivially true via getCommandSpec(path.slice(0,1))), keeping
            # this fixture's own "argv survives completely unchanged"
            # coverage for a command that legitimately belongs in this list.
            for arguments in (
                ("config", "get", "x"),
                ("package", "list"),
                ("help", "package"),
            ):
                with self.subTest(arguments=arguments, mode="clean-cwd-unguarded"):
                    result = run_wrapper(arguments, cwd=clean)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = installer.strict_json(result.stdout.encode("utf-8"))
                    self.assertIsInstance(payload, dict)
                    assert isinstance(payload, dict)
                    self.assertEqual(payload["argv"][1:], list(arguments))
                    # The launch guard always pops its own signal env var
                    # before exec'ing the real CLI, regardless of whether
                    # guards were inserted.
                    self.assertIsNone(payload["resourceGuard"])

            # (e) Commands UNAFFECTED by this fix (still in public_commands)
            # remain session_start=0 -- no settings-gate block -- in the
            # SAME project directory that now blocks config/package/help.
            for cmd in ("status", "list", "doctor", "schedule", "shutdown"):
                with self.subTest(command=cmd, mode="unaffected-still-public"):
                    result = run_wrapper((cmd,), cwd=project)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_help_argument_resource_guards_depend_on_upstream_match(
        self,
    ) -> None:
        # Regression for independent review round 10, 2026-08-18, P1 (round
        # 9 found round 8's fix incomplete): round 8 made "help <argument>"
        # session_start=1 (settings gate applies), but
        # managed_launch_guard_script()'s guarded_arguments() still treated
        # EVERY "help <argument>" identically via RUNTIME_NO_GUARD_COMMANDS
        # -- no RESOURCE_GUARDS ever spliced in, match or miss. That is only
        # safe for a MATCH (upstream's printRequestedHelp() always returns
        # HANDLED before ever reaching extension loading); a MISS falls
        # through upstream's own continueWith(args) to a REAL, unprotected
        # session start with "help"/the argument as positional chat
        # messages -- and, crucially, this needs NO settings.json to exist
        # at all, unlike round 8's config/package finding: this test
        # exercises that exact no-settings.json gap round 9 flagged the
        # existing coverage as missing. See the separate real-upstream-
        # bundle end-to-end verification for proof the underlying RCE this
        # closes is genuine (a planted .prime/agent/extensions/evil.js
        # actually executes pre-fix and does not post-fix), not merely an
        # argv-shape test.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "release"
            bin_dir = release / "bin"
            bin_dir.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root, 0o700)
            os.chmod(release, 0o700)
            node = bin_dir / "node"
            cli = release / "cli.js"
            node.write_text(
                "#!/usr/bin/python3\n"
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "  'argv': sys.argv[1:],\n"
                "  'resourceGuard': os.environ.get('ORCA_PRIME_AGENT_RESOURCE_GUARD'),\n"
                "}))\n",
                encoding="utf-8",
            )
            os.chmod(node, 0o700)
            cli.write_text("// argument sentinel\n", encoding="utf-8")
            os.chmod(cli, 0o600)
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            guard = bin_dir / "prime-agent-launch-guard.py"
            wrapper = bin_dir / "prime-agent"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
            ):
                guard.write_bytes(
                    installer.managed_launch_guard_script(
                        node,
                        cli,
                        installer.sha256_file(node),
                        installer.sha256_file(cli),
                    )
                )
                wrapper.write_bytes(
                    installer.managed_entrypoint_script(node, cli, guard)
                )
            os.chmod(guard, 0o700)
            os.chmod(wrapper, 0o700)
            # Deliberately NO .prime/agent/settings.json anywhere -- proving
            # RESOURCE_GUARDS, not the settings gate, is what protects a
            # help-miss here.
            clean = root / "clean-no-settings-json"
            clean.mkdir(mode=0o700)
            self.assertFalse((clean / ".prime/agent/settings.json").exists())

            def run_wrapper(
                arguments: tuple[str, ...],
            ) -> subprocess.CompletedProcess[str]:
                environment = {
                    "HOME": os.fspath(root),
                    "PATH": "/usr/bin:/bin",
                }
                return subprocess.run(
                    [os.fspath(wrapper), *arguments],
                    cwd=clean,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                )

            def argv_of(result: subprocess.CompletedProcess[str]) -> list[object]:
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = installer.strict_json(result.stdout.encode("utf-8"))
                self.assertIsInstance(payload, dict)
                assert isinstance(payload, dict)
                self.assertIsNone(payload["resourceGuard"])
                argv = payload["argv"]
                self.assertIsInstance(argv, list)
                assert isinstance(argv, list)
                return argv[1:]

            # MISS cases: upstream's own isHelpCommandRequest() (verified
            # directly against the pinned v0.7.2 sources) does not
            # recognize any of these as a real command, exact or fuzzy --
            # RESOURCE_GUARDS must be spliced in, appended after the whole
            # "help <argument>" (or before a trailing "--", so parseArgs()
            # doesn't swallow them into a positional message).
            miss_cases = (
                (("help", "zzzzzzzzzzzz"), ["help", "zzzzzzzzzzzz"]),
                (("help", "tols"), ["help", "tols"]),
                (("help", "mcp"), ["help", "mcp"]),
                (("help", "auth"), ["help", "auth"]),
                (("help", "--", "zzzzzzzzzzzz"), ["help", "--", "zzzzzzzzzzzz"]),
                # Regression for independent review round 11, 2026-08-18, P1:
                # round 10's fuzzy-matcher transcription measured string
                # length with plain len() (Unicode code points), while
                # upstream measures JS UTF-16 code units -- these disagree
                # for any astral (non-BMP) character, so an emoji-suffixed
                # near-miss like this one computed a fuzzy-MATCH in the old
                # Python port (guards skipped) but a real MISS in the actual
                # upstream Node CLI (a genuine unprotected session start).
                # Round 12 removed fuzzy matching entirely in favor of exact
                # membership checks, which have no length semantics at all,
                # so this and any other near-miss now unconditionally guards.
                (
                    ("help", "status\U0001F600\U0001F600"),
                    ["help", "status\U0001F600\U0001F600"],
                ),
                (
                    ("help", "config\U0001F600\U0001F600"),
                    ["help", "config\U0001F600\U0001F600"],
                ),
            )
            for arguments, base in miss_cases:
                with self.subTest(arguments=arguments, mode="miss-guarded"):
                    observed = argv_of(run_wrapper(arguments))
                    if "--" in base:
                        separator = base.index("--")
                        expected = (
                            base[:separator]
                            + ["--no-extensions", "--no-skills", "--no-prompt-templates"]
                            + base[separator:]
                        )
                    else:
                        expected = base + [
                            "--no-extensions",
                            "--no-skills",
                            "--no-prompt-templates",
                        ]
                    self.assertEqual(observed, expected)

            # MATCH cases: upstream's own isHelpCommandRequest() recognizes
            # every one of these (exact top-level or child command path) --
            # printRequestedHelp() always returns HANDLED before extension
            # loading, so argv must arrive COMPLETELY UNCHANGED. Appending
            # RESOURCE_GUARDS here would corrupt the literal command PATH
            # formatCommandHelp()/getCommandSpec() require an exact-length
            # match against, turning a legitimate help lookup into an
            # "Unknown command" error -- exactly the regression this test
            # guards against.
            match_cases = (
                ("help", "package"),
                ("help", "package", "install"),
                ("help", "session"),
                ("help", "session", "export"),
                ("help", "model", "list"),
                ("help", "schedule", "add"),
            )
            for arguments in match_cases:
                with self.subTest(arguments=arguments, mode="match-unguarded"):
                    observed = argv_of(run_wrapper(arguments))
                    self.assertEqual(observed, list(arguments))

    def test_is_help_command_request_uses_exact_match_only(self) -> None:
        # Regression for independent review round 11, 2026-08-18, P1: round
        # 10's fuzzy-matcher transcription had a real bug (Unicode
        # code-point vs. JS UTF-16 code-unit length mismatch) that let an
        # emoji-suffixed argument fuzzy-match a real topic in Python while
        # missing upstream's own real matcher, skipping RESOURCE_GUARDS for
        # what was actually an unprotected session start. Round 12 removed
        # fuzzy matching entirely; this directly proves that removal at the
        # function level (executing the actual generated launch-guard
        # script's source in a fresh namespace, the same content
        # managed_launch_guard_script() writes to disk -- is_help_command_
        # request() lives inside that generated script, not as an
        # install_prime_agent module-level attribute), not just via the
        # wrapper-subprocess fixture in
        # test_help_argument_resource_guards_depend_on_upstream_match above.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            (tool_root / "release" / "bin").mkdir(parents=True, mode=0o700)
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", tool_root / "release"),
                mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
            ):
                source = installer.managed_launch_guard_script(
                    tool_root / "release" / "bin" / "node",
                    tool_root / "release" / "cli.js",
                    "0" * 64,
                    "0" * 64,
                ).decode("utf-8")
        namespace: dict[str, object] = {"__name__": "generated_launch_guard"}
        exec(compile(source, "<generated-launch-guard>", "exec"), namespace)  # noqa: S102

        self.assertFalse(
            "help_edit_distance" in namespace,
            "fuzzy-matching helper must be fully removed, not just unused",
        )
        self.assertFalse(
            "help_find_command_suggestion" in namespace,
            "fuzzy-matching helper must be fully removed, not just unused",
        )
        self.assertFalse(
            "help_child_command_names" in namespace,
            "fuzzy-matching helper must be fully removed, not just unused",
        )
        is_help_command_request = namespace["is_help_command_request"]
        # Exact matches (unchanged from round 10): bare help, top-level and
        # child command paths, and upstream's own removed-command names.
        for path in (
            [],
            ["package"],
            ["package", "install"],
            ["session", "export"],
            ["app"],  # HELP_REMOVED_COMMAND_NAMES
        ):
            with self.subTest(path=path, expect=True):
                self.assertTrue(is_help_command_request(path))
        # Near-misses that round 10's fuzzy matcher would have passed
        # through unguarded (small edit distance to a real topic) must now
        # be treated as misses -- including the specific class of bug round
        # 11 found (astral/non-BMP characters appended to a real topic name,
        # which round 10's code-point-length fuzzy threshold miscalculated).
        for path in (
            ["statu"],  # 1-edit typo of "status"
            ["statuss"],
            ["zzzzzzzzzzzz"],
            ["status\U0001F600\U0001F600"],
            ["config\U0001F600\U0001F600"],
            ["package\U0001F600\U0001F600\U0001F600"],
        ):
            with self.subTest(path=path, expect=False):
                self.assertFalse(is_help_command_request(path))

    def test_session_export_requires_session_start_protection_by_default(
        self,
    ) -> None:
        # Regression for independent review round 10, 2026-08-18, P1 (round
        # 9 found round 8's fix incomplete): "session" was still classified
        # session_start=0 in managed_entrypoint_script()'s public_commands,
        # so `session export ""` skipped BOTH the settings gate and
        # RESOURCE_GUARD_ENV entirely. Upstream's rewriteNestedCommand()
        # unconditionally sets result.export = args[++i] for the internal
        # "--export" flag "session export" rewrites to, and an empty string
        # is falsy at main.js's later `if (parsed.export)` early-exit gate,
        # so control falls through into a real, unprotected session start
        # -- a completely realistic trigger via plain shell expansion of an
        # unset variable (`session export "$UNSET_VAR"`), no adversarial
        # argv construction needed. "session" now gets the same
        # settings-gate + RESOURCE_GUARD_ENV treatment as an ordinary
        # session-start command (config/package's round-8 treatment), and
        # managed_launch_guard_script()'s guarded_arguments() places
        # RESOURCE_GUARDS after the whole "session export <value>" (or
        # before a trailing "--") -- verified directly against upstream's
        # rewriteNestedCommand()/splitOperandsAndOptions() to not disturb
        # the "session"/"export" token adjacency those functions require,
        # so a legitimate `session export <path>` still reaches upstream's
        # real exportFromFile() and exits before ever touching
        # SettingsManager or extension loading, guards or not.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "release"
            bin_dir = release / "bin"
            bin_dir.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root, 0o700)
            os.chmod(release, 0o700)
            node = bin_dir / "node"
            cli = release / "cli.js"
            node.write_text(
                "#!/usr/bin/python3\n"
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "  'argv': sys.argv[1:],\n"
                "  'resourceGuard': os.environ.get('ORCA_PRIME_AGENT_RESOURCE_GUARD'),\n"
                "}))\n",
                encoding="utf-8",
            )
            os.chmod(node, 0o700)
            cli.write_text("// argument sentinel\n", encoding="utf-8")
            os.chmod(cli, 0o600)
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            guard = bin_dir / "prime-agent-launch-guard.py"
            wrapper = bin_dir / "prime-agent"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
            ):
                guard.write_bytes(
                    installer.managed_launch_guard_script(
                        node,
                        cli,
                        installer.sha256_file(node),
                        installer.sha256_file(cli),
                    )
                )
                wrapper.write_bytes(
                    installer.managed_entrypoint_script(node, cli, guard)
                )
            os.chmod(guard, 0o700)
            os.chmod(wrapper, 0o700)
            clean = root / "clean"
            project = root / "project"
            clean.mkdir(mode=0o700)
            (project / ".prime/agent").mkdir(parents=True, mode=0o700)
            (project / ".prime/agent/settings.json").write_text(
                "{}", encoding="utf-8"
            )

            def run_wrapper(
                arguments: tuple[str, ...],
                *,
                cwd: Path,
                allow_settings: bool = False,
            ) -> subprocess.CompletedProcess[str]:
                environment = {
                    "HOME": os.fspath(root),
                    "PATH": "/usr/bin:/bin",
                }
                if allow_settings:
                    environment["ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS"] = "1"
                return subprocess.run(
                    [os.fspath(wrapper), *arguments],
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                )

            # (a) THE FIX: "session ..." (any operand, empty or not) is now
            # blocked by the settings gate in a project carrying
            # .prime/agent/settings.json, absent the opt-in env var. Before
            # round 10 this incorrectly returned rc 0.
            for arguments in (
                ("session", "export", ""),
                ("session", "export", "/some/real/path"),
                ("session",),
            ):
                with self.subTest(arguments=arguments, mode="now-protected"):
                    result = run_wrapper(arguments, cwd=project)
                    self.assertEqual(result.returncode, 78, result.stderr)
                    self.assertIn(
                        "project .prime/agent/settings.json", result.stderr
                    )

            # (b) Explicit opt-in restores the previous, unprotected
            # settings-gate behavior.
            opted_in = run_wrapper(
                ("session", "export", ""), cwd=project, allow_settings=True
            )
            self.assertEqual(opted_in.returncode, 0, opted_in.stderr)

            # (c) In a CLEAN cwd (no settings.json at all -- the round-9 gap
            # that a settings-file gate alone can never close), both the
            # exploit form and legitimate usage must now carry
            # RESOURCE_GUARDS, appended after the whole "session export
            # <value>", with the "session"/"export" adjacency
            # rewriteNestedCommand() requires left completely intact.
            for arguments in (
                ("session", "export", ""),
                ("session", "export", "/some/real/path"),
            ):
                with self.subTest(arguments=arguments, mode="clean-cwd-guarded"):
                    result = run_wrapper(arguments, cwd=clean)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = installer.strict_json(result.stdout.encode("utf-8"))
                    self.assertIsInstance(payload, dict)
                    assert isinstance(payload, dict)
                    self.assertEqual(
                        payload["argv"][1:],
                        [
                            *arguments,
                            "--no-extensions",
                            "--no-skills",
                            "--no-prompt-templates",
                        ],
                    )
                    self.assertIsNone(payload["resourceGuard"])

    def test_print_flag_anywhere_forces_session_start_protection(self) -> None:
        # Regression for independent review round 10, 2026-08-18, P2:
        # upstream's own shouldStartDaemonEarly() (dist/cli/daemon-launch.js)
        # spawns a detached background daemon whenever
        # `args.includes("--print") || args.includes("-p")` is true --
        # checked with a literal, position- and "--"-agnostic array-
        # membership test, BEFORE this wrapper's own public/no-guard
        # command classification could possibly matter (maybeStartDaemonEarly()
        # runs before runPublicCommand()/parseArgs() in upstream's own
        # cli-main.js). Reproduced with "status -p", "list -p",
        # "doctor --print", "stop -p abc", "send bot -p",
        # "shutdown --force -p", and "schedule add ... -- -p run" all
        # spawning a daemon (bare "status" does not), inheriting this
        # wrapper's own launch cwd. The spawned daemon's own argv is
        # hardcoded by upstream to "--mode daemon --daemon-socket <path>"
        # only -- RESOURCE_GUARDS can never reach it no matter what this
        # wrapper does (a documented, accepted residual gap, P2 not P1) --
        # but the $PWD/.prime/agent/settings.json gate below is entirely
        # this wrapper's own, and now fires for any invocation upstream
        # would treat this way, closing the "hostile settings.json
        # short-circuits the daemon early-spawn before this wrapper's own
        # public-command classification would otherwise have gated it"
        # vector.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "release"
            bin_dir = release / "bin"
            bin_dir.mkdir(parents=True, mode=0o700)
            os.chmod(tool_root, 0o700)
            os.chmod(release, 0o700)
            node = bin_dir / "node"
            cli = release / "cli.js"
            node.write_text(
                "#!/usr/bin/python3\n"
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "  'argv': sys.argv[1:],\n"
                "  'resourceGuard': os.environ.get('ORCA_PRIME_AGENT_RESOURCE_GUARD'),\n"
                "}))\n",
                encoding="utf-8",
            )
            os.chmod(node, 0o700)
            cli.write_text("// argument sentinel\n", encoding="utf-8")
            os.chmod(cli, 0o600)
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            guard = bin_dir / "prime-agent-launch-guard.py"
            wrapper = bin_dir / "prime-agent"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", tool_root / "state"),
            ):
                guard.write_bytes(
                    installer.managed_launch_guard_script(
                        node,
                        cli,
                        installer.sha256_file(node),
                        installer.sha256_file(cli),
                    )
                )
                wrapper.write_bytes(
                    installer.managed_entrypoint_script(node, cli, guard)
                )
            os.chmod(guard, 0o700)
            os.chmod(wrapper, 0o700)
            clean = root / "clean"
            project = root / "project"
            clean.mkdir(mode=0o700)
            (project / ".prime/agent").mkdir(parents=True, mode=0o700)
            (project / ".prime/agent/settings.json").write_text(
                "{}", encoding="utf-8"
            )

            def run_wrapper(
                arguments: tuple[str, ...], *, cwd: Path
            ) -> subprocess.CompletedProcess[str]:
                environment = {"HOME": os.fspath(root), "PATH": "/usr/bin:/bin"}
                return subprocess.run(
                    [os.fspath(wrapper), *arguments],
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                )

            # (a) THE FIX: every one of these previously reached the
            # underlying CLI (and, upstream, spawned an early daemon) even
            # in a project carrying a hostile settings.json. All must now
            # be blocked by the settings gate absent the opt-in env var.
            print_cases = (
                ("status", "-p"),
                ("list", "-p"),
                ("doctor", "--print"),
                ("stop", "-p", "abc"),
                ("send", "bot", "-p"),
                ("shutdown", "--force", "-p"),
                ("schedule", "add", "a", "b", "--", "-p", "run"),
            )
            for arguments in print_cases:
                with self.subTest(arguments=arguments, mode="print-now-protected"):
                    result = run_wrapper(arguments, cwd=project)
                    self.assertEqual(result.returncode, 78, result.stderr)
                    self.assertIn(
                        "project .prime/agent/settings.json", result.stderr
                    )

            # (b) Sanity/control: the SAME base commands without -p/--print
            # remain unaffected (still session_start=0, no settings-gate
            # block) in the identical project directory -- proving (a) is
            # really the -p/--print detection firing, not some unrelated
            # tightening of these commands.
            control_cases = (
                ("status",),
                ("list",),
                ("doctor",),
                ("stop", "abc"),
                ("send", "bot", "hi"),
                ("shutdown", "--force"),
            )
            for arguments in control_cases:
                with self.subTest(arguments=arguments, mode="control-unaffected"):
                    result = run_wrapper(arguments, cwd=project)
                    self.assertEqual(result.returncode, 0, result.stderr)

            # (c) In a CLEAN cwd, argv reaching the underlying CLI stays
            # COMPLETELY UNCHANGED for these -- they remain in
            # RUNTIME_NO_GUARD_COMMANDS (their own dispatch never accepts
            # or needs RESOURCE_GUARDS), so forcing session_start=1 here
            # must not corrupt their argv even though RESOURCE_GUARD_ENV is
            # now exported for them.
            for arguments in print_cases:
                with self.subTest(arguments=arguments, mode="clean-cwd-argv-intact"):
                    result = run_wrapper(arguments, cwd=clean)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = installer.strict_json(result.stdout.encode("utf-8"))
                    self.assertIsInstance(payload, dict)
                    assert isinstance(payload, dict)
                    self.assertEqual(payload["argv"][1:], list(arguments))
                    self.assertIsNone(payload["resourceGuard"])

    def test_verify_command_state_detects_ancestor_swap_to_directory_without_command(
        self,
    ) -> None:
        # Regression for independent review round 5, 2026-08-18, P1-3:
        # verify_command_state() used to resolve BIN_LINK's mere EXISTENCE
        # via Path.is_symlink()/Path.exists() -- purely lexical checks that
        # silently follow whatever an ancestor currently resolves to.
        # Unlike test_verify_link_rejects_interposed_ancestor_symlink's
        # honestly-shaped-decoy scenario (where the attacker directory DOES
        # contain its own bin/prime-agent, so the old lexical
        # is_symlink() check still found something and fell through to
        # verify_link()'s own, already-fixed ancestor check), this is the
        # review's exact still-open scenario: the attacker directory
        # contains NOTHING at all. Both is_symlink() and exists() then see
        # "nothing here", so verify_command_state() used to silently return
        # False ("disabled"/"absent") -- and _uninstall_locked() would
        # report command_disabled=true/already_disabled=true -- while the
        # REAL managed link, reachable only through the true (pre-swap)
        # ancestor chain, was untouched and still live. Must now fail
        # closed instead.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            target = ssd / "target"
            target.parent.mkdir(mode=0o700)
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(target, 0o700)
            user_home = root / "user"
            real_bin = user_home / ".local/bin"
            real_bin.mkdir(parents=True, mode=0o700)
            link = real_bin / "prime-agent"

            with (
                mock.patch.object(installer, "SSD_ROOT", ssd),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(installer, "BIN_LINK", link),
                mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
            ):
                installer.atomic_symlink(target, link)
                receipt = {"bin_target": os.fspath(target)}
                # Sanity: the honestly created link verifies as enabled
                # before the ancestor is swapped.
                self.assertTrue(installer.verify_command_state(receipt))

                real_local = user_home / ".local"
                relocated = real_local.with_name(".local-relocated")
                real_local.rename(relocated)
                attacker_dir = root / "attacker-empty"
                attacker_dir.mkdir(mode=0o700)
                real_local.symlink_to(attacker_dir, target_is_directory=True)

                # The attacker directory has NO bin/prime-agent at all --
                # pre-fix, this silently returned False instead of failing
                # closed.
                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "link parent is missing|unsafe link parent|"
                    "cannot inspect link parent",
                ):
                    installer.verify_command_state(receipt)

            # The real (relocated) link must still be exactly what was
            # honestly created -- untouched by any of this.
            real_relocated_link = relocated / "bin/prime-agent"
            self.assertTrue(real_relocated_link.is_symlink())
            self.assertEqual(os.readlink(real_relocated_link), os.fspath(target))

    def test_bin_link_present_detects_ancestor_swap_to_empty_directory(self) -> None:
        # Regression for independent dual review round 6, 2026-08-18, P2:
        # quarantine_partial_release(), finalize_pending_install(),
        # _install_locked(), and plan() all used to resolve BIN_LINK's mere
        # EXISTENCE via the plain lexical `BIN_LINK.exists() or
        # BIN_LINK.is_symlink()` -- unlike verify_command_state(), fixed for
        # this exact issue in round 5. Same scenario as
        # test_verify_command_state_detects_ancestor_swap_to_directory_without_command:
        # a same-UID actor swaps ~/.local for a symlink to an empty
        # attacker directory (containing no bin/prime-agent at all), so the
        # old lexical checks silently see "nothing here" while the REAL
        # managed link, reachable only through the true (pre-swap) ancestor
        # chain, is still live -- letting quarantine_partial_release()
        # proceed, or finalize_pending_install()/_install_locked() treat
        # the command as safely disabled/absent, over activation state that
        # is still actually present. bin_link_present() now uses the SAME
        # dir_fd-chained ancestor walk verify_command_state() already uses
        # and must fail closed here instead.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ssd = root / "ssd"
            target = ssd / "target"
            target.parent.mkdir(mode=0o700)
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(target, 0o700)
            user_home = root / "user"
            real_bin = user_home / ".local/bin"
            real_bin.mkdir(parents=True, mode=0o700)
            link = real_bin / "prime-agent"

            with (
                mock.patch.object(installer, "SSD_ROOT", ssd),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(installer, "BIN_LINK", link),
            ):
                # Sanity: genuinely absent (nothing created yet) still
                # reports False, exactly like the lexical check it
                # replaces.
                self.assertFalse(installer.bin_link_present())

                installer.atomic_symlink(target, link)
                self.assertTrue(installer.bin_link_present())

                real_local = user_home / ".local"
                relocated = real_local.with_name(".local-relocated")
                real_local.rename(relocated)
                attacker_dir = root / "attacker-empty"
                attacker_dir.mkdir(mode=0o700)
                real_local.symlink_to(attacker_dir, target_is_directory=True)

                # The attacker directory has NO bin/prime-agent at all --
                # pre-fix, the lexical check this replaces silently
                # returned False here instead of failing closed.
                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "link parent is missing|unsafe link parent|"
                    "cannot inspect link parent",
                ):
                    installer.bin_link_present()

            # The real (relocated) link must still be exactly what was
            # honestly created -- untouched by any of this.
            real_relocated_link = relocated / "bin/prime-agent"
            self.assertTrue(real_relocated_link.is_symlink())
            self.assertEqual(os.readlink(real_relocated_link), os.fspath(target))

    def test_lifecycle_lock_reacquire_after_inode_swap_is_detected(self) -> None:
        # Regression for independent review round 5, 2026-08-18, P2-2:
        # flock(2) binds exclusivity to the inode held open at acquire
        # time, not to the path. Real repro of the review's exact
        # scenario: A acquires the lock, a same-UID actor renames a fresh
        # file over lifecycle.lock's well-known path while A still holds
        # its original fd, and C then independently acquires its OWN
        # "exclusive" lock against the swapped path -- demonstrating that
        # C's acquisition genuinely succeeds on its own terms (this is
        # flock()'s real per-inode behavior, not something the acquiring
        # side alone can prevent) while A's own re-check against the
        # identity it captured at acquire time must now detect the swap
        # and fail closed, instead of A silently continuing as if it still
        # held exclusive protection over the well-known path.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
            ):
                lock_a = installer.exclusive_lifecycle_lock(create=True)
                path_a, identity_a = lock_a.__enter__()
                try:
                    self.assertEqual(path_a, tool_root / "lifecycle.lock")
                    self.assertIsInstance(identity_a, tuple)
                    self.assertEqual(len(identity_a), 2)

                    # Same-UID actor renames a fresh file over the lock path.
                    fresh = tool_root / ".fresh-lock"
                    fresh.write_bytes(b"")
                    os.chmod(fresh, 0o600)
                    os.replace(fresh, path_a)

                    # C: an independent acquisition against the swapped
                    # path succeeds on its own terms.
                    lock_c = installer.exclusive_lifecycle_lock(create=False)
                    path_c, identity_c = lock_c.__enter__()
                    try:
                        self.assertEqual(path_c, path_a)
                        self.assertNotEqual(identity_c, identity_a)
                    finally:
                        lock_c.__exit__(None, None, None)

                    # A's own re-check against the identity captured at its
                    # acquire time must now detect the swap and fail closed.
                    with self.assertRaisesRegex(
                        installer.PrimeInstallError,
                        "identity changed while held",
                    ):
                        installer.assert_lifecycle_lock_path_identity(
                            path_a, identity_a
                        )
                finally:
                    lock_a.__exit__(None, None, None)

    def test_finalize_pending_install_fails_closed_on_stale_lifecycle_lock_identity(
        self,
    ) -> None:
        # Regression for independent review round 5, 2026-08-18, P2-2:
        # finalize_pending_install() is a critical use point reached while
        # an ancestor caller's exclusive lifecycle lock is (or should
        # still be) held. Confirms a caller-supplied identity that no
        # longer matches the real, current lifecycle lock (as if the lock
        # file had been swapped for a fresh inode while held) is detected
        # and fails closed BEFORE any managed state is touched, while the
        # correct, current identity still lets the exact same operation
        # succeed -- pre-fix, finalize_pending_install() accepted no such
        # parameter at all and could not detect this.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            state = tool_root / "state"
            probe_home = tool_root / "probe-home"
            session_dir = tool_root / "sessions"
            user_home = root / "user"
            receipt_path = tool_root / "receipts" / f"v{installer.VERSION}.json"
            pending = tool_root / "pending-install.json"
            for path in (release / "bin", user_home, receipt_path.parent):
                path.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(release, 0o700)
            settings = self.create_managed_home(
                state, session_dir=tool_root / "sessions"
            )
            self.create_managed_home(probe_home, session_dir=tool_root / "sessions")
            session_dir.mkdir(mode=0o700)
            entrypoint = release / "bin" / "prime-agent"
            entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(entrypoint, 0o700)
            launch_guard = release / "bin" / "prime-agent-launch-guard.py"
            launch_guard.write_text("#!/usr/bin/python3\n", encoding="utf-8")
            os.chmod(launch_guard, 0o700)
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            unrelated = root / "unrelated-file"
            unrelated.write_bytes(b"")
            with mock.patch.object(installer, "SSD_ROOT", root):
                digest, entries = installer.tree_digest(release)
            state_link = user_home / ".prime"
            bin_link = user_home / ".local/bin/prime-agent"
            receipt = {
                "schema": installer.RECEIPT_SCHEMA,
                "version": installer.VERSION,
                "tag_commit": installer.TAG_COMMIT,
                "volume_uuid": "TEST-UUID",
                "release_dir": os.fspath(release),
                "state_dir": os.fspath(state),
                "state_link": os.fspath(state_link),
                "bin_link": os.fspath(bin_link),
                "bin_target": os.fspath(entrypoint),
                "launch_guard": os.fspath(launch_guard),
                "lifecycle_lock": os.fspath(lifecycle_lock),
                "node_target": os.fspath(release / "toolchain/bin/node"),
                "npm_target": os.fspath(
                    release / "toolchain/lib/node_modules/npm/bin/npm-cli.js"
                ),
                "probe_home": os.fspath(probe_home),
                "probe_agent_dir": os.fspath(probe_home / "agent"),
                "session_dir": os.fspath(session_dir),
                "asset_sha256": installer.ASSETS,
                "node_asset_sha256": installer.NODE_ASSET_SHA256,
                "upstream_lock_sha256": installer.LOCK_SHA256,
                "license_sha256": installer.LICENSE_SHA256,
                "production_lock_sha256": installer.GENERATED_LOCK_SHA256,
                "patched_manifest_names": sorted(("prime-agent", *installer.WORKSPACE_PACKAGES)),
                "closure": {
                    "lock_sha256": installer.GENERATED_LOCK_SHA256,
                    "packages_checked": installer.GENERATED_LOCK_PACKAGE_COUNT,
                    "registry_packages_checked": installer.GENERATED_LOCK_PACKAGE_COUNT - 4,
                },
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "lifecycle_scripts_executed": False,
                "daemon_started": False,
                "credentials_configured": False,
                "command_default_enabled": False,
                "project_settings_default_allowed": False,
                "project_executable_resources_default_allowed": False,
                "telemetry_default_enabled": False,
                "telemetry_settings": os.fspath(settings),
                "release_tree_sha256": digest,
                "release_tree_entries": entries,
                "orca_support": {"test": "support"},
            }
            installer.atomic_write(
                pending,
                installer.canonical_json(
                    {"schema": installer.JOURNAL_SCHEMA, "receipt": receipt}
                ),
                0o600,
            )
            real_identity = (
                lifecycle_lock.stat().st_dev,
                lifecycle_lock.stat().st_ino,
            )
            stale_identity = (unrelated.stat().st_dev, unrelated.stat().st_ino)
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "RELEASE_DIR", release),
                mock.patch.object(installer, "STATE_DIR", state),
                mock.patch.object(installer, "PROBE_HOME", probe_home),
                mock.patch.object(installer, "USER_HOME", user_home),
                mock.patch.object(installer, "STATE_LINK", state_link),
                mock.patch.object(installer, "BIN_LINK", bin_link),
                mock.patch.object(installer, "RECEIPT_PATH", receipt_path),
                mock.patch.object(installer, "PENDING_PATH", pending),
                mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID"),
                mock.patch.object(
                    installer, "verify_orca_support", return_value={"test": "support"}
                ),
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "lifecycle lock identity changed while held",
                ):
                    installer.finalize_pending_install(stale_identity)
                self.assertTrue(pending.is_file())
                self.assertFalse(receipt_path.exists())
                self.assertFalse(state_link.exists())

                first = installer.finalize_pending_install(real_identity)
            self.assertEqual(first["version"], installer.VERSION)
            self.assertTrue(state_link.is_symlink())
            self.assertTrue(receipt_path.is_file())
            self.assertFalse(pending.exists())

    def test_install_locked_reaches_finalize_with_correct_lock_identity(self) -> None:
        # Regression for independent dual review round 6, 2026-08-18, P1
        # (found independently by a Claude opus/max review and a separate
        # Codex QA pass -- the highest-priority finding of that round):
        # _install_locked(lock_identity) takes the managed lifecycle lock's
        # identity as its only parameter and must pass that SAME value,
        # unchanged, to `return finalize_pending_install(lock_identity)` at
        # its very end. A local variable of the exact same name was
        # reassigned partway through -- to RELEASE_DIR/package-lock.json's
        # (st_dev, st_ino) identity, for a DIFFERENT verify-then-use check
        # -- silently shadowing the parameter for the rest of the function,
        # so that final call actually passed package-lock.json's identity.
        # finalize_pending_install() then compared that against the REAL
        # lifecycle lock's current identity and deterministically raised
        # "managed lifecycle lock identity changed while held" on every
        # single fresh install, AFTER the pending journal had already been
        # durably written.
        #
        # This is deliberately NOT the same shape as
        # test_finalize_pending_install_fails_closed_on_stale_lifecycle_lock_identity
        # above: that test (and its sibling for verify()) hand-constructs an
        # already-pending journal and calls finalize_pending_install()
        # directly, so it never executes a single line of _install_locked()'s
        # own body and could not have caught this -- exactly why it shipped
        # un-caught (per the round-6 review). This test instead runs the
        # REAL, unmodified install() -> _install_locked() code path end to
        # end. Every step that is genuinely external (HTTPS downloads, the
        # pinned node/npm subprocesses, npm itself) is replaced with a fake
        # that still exercises every in-process check the surrounding,
        # UNMODIFIED code performs (directory creation, manifest/lock
        # writes, verify_unchanged_private_ssd_file() re-checks, tree_digest,
        # sync_private_tree, receipt identity validation, and finally
        # finalize_pending_install()'s own re-derivation and comparison of
        # the lifecycle lock's current identity against whatever
        # _install_locked() passed it) -- so the exact local-variable
        # shadowing bug, if reintroduced, would make this test fail again.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            # A minimal generated production lock that satisfies
            # validate_generated_lock(): a root entry plus exactly the four
            # local-asset rows (prime-agent + the three workspace
            # packages), no registry dependencies at all.
            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_text("// fake cli\n", encoding="utf-8")
                    os.chmod(cli, 0o600)

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {Path("dist/bundle/cli.js"): installer.sha256_bytes(b"// fake cli\n")}
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            # A plain `with (...)` block with this many context managers
            # trips CPython's compiler limit on statically nested blocks;
            # ExitStack avoids that while patching the exact same set.
            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))
                result = installer.install()

            # install() -> _install_locked() returns finalize_pending_install()'s
            # result directly (the published receipt dict itself, not an
            # {"ok": ...}-wrapped action result) -- reaching this line at
            # all, with the pending journal now gone and a durable receipt
            # published (asserted below), is itself proof
            # finalize_pending_install() did not raise "managed lifecycle
            # lock identity changed while held".
            self.assertEqual(result["version"], installer.VERSION)
            self.assertEqual(result["schema"], installer.RECEIPT_SCHEMA)
            self.assertTrue((user_home / ".prime").is_symlink())
            self.assertTrue(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").is_file()
            )
            self.assertFalse((tool_root / "pending-install.json").exists())
            # The command link must stay disabled by default -- install()
            # only ever reaches finalize_pending_install(), never enable().
            self.assertFalse((user_home / ".local/bin/prime-agent").exists())
            self.assertFalse((user_home / ".local/bin/prime-agent").is_symlink())

    def test_install_locked_tightens_real_npm_entrypoint_mode_before_digest_capture(
        self,
    ) -> None:
        # Regression for a real bug this round's own fix introduced,
        # discovered by tests/sandbox_e2e.py's real, non-mocked lifecycle
        # replay (round 24, 2026-08-19) -- not by any mocked unit test,
        # exactly the same discovery path round 18's package-lock.json
        # finding took (see tighten_generated_private_file_mode()'s
        # docstring): a real `npm ci` materializes node_modules/prime-agent/
        # dist/bundle/cli.js at whatever mode the PUBLISHED TARBALL's own
        # stored permissions specify -- observed 0o644 (world-readable) for
        # the real prime-agent release -- not the private, owner-only mode
        # every other file this installer itself writes always has from the
        # moment it becomes visible. capture_private_ssd_asset_digest() (this
        # round's new Part 3 fix) reads via read_private_ssd_file(), which
        # requires `st_mode & 0o077 == 0`; every mocked full-install test
        # above wrote its fake cli.js already at a private 0o600, which
        # accidentally bypassed this exact gap the same way round 18's own
        # note describes ("all 96 prior unit tests mocked lock-file
        # generation in a way that bypassed real npm's actual
        # default-umask output mode").
        #
        # This test is otherwise IDENTICAL to
        # test_install_locked_reaches_finalize_with_correct_lock_identity
        # above, with exactly one change: the fake `npm ci` materializes
        # cli.js at a real-world-accurate 0o644 instead of 0o600. Before
        # this round's chmod-before-capture fix (chmod'ing
        # pre_move_entrypoint to 0o700 immediately, before
        # capture_private_ssd_asset_digest() ever reads it, rather than
        # only after the node_modules -> lib/node_modules move as the
        # original code did), this reproduced "unsafe private file" and
        # aborted every real (non-mocked) install.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_text("// fake cli\n", encoding="utf-8")
                    # The one deliberate difference from the happy-path test
                    # above: a real, world-readable npm-package file mode,
                    # not this installer's own already-private convention.
                    os.chmod(cli, 0o644)

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {Path("dist/bundle/cli.js"): installer.sha256_bytes(b"// fake cli\n")}
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))
                result = installer.install()

            self.assertEqual(result["version"], installer.VERSION)
            self.assertEqual(result["schema"], installer.RECEIPT_SCHEMA)
            self.assertTrue(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").is_file()
            )
            self.assertFalse((tool_root / "pending-install.json").exists())
            # The published entrypoint must have been tightened to a
            # private, owner-only mode -- not left at npm's own permissive
            # 0o644 -- regardless of that mode change happening before the
            # node_modules move (this round) rather than after it
            # (pre-round-24).
            published_entrypoint = (
                release / "lib/node_modules/prime-agent/dist/bundle/cli.js"
            )
            self.assertEqual(
                stat.S_IMODE(published_entrypoint.lstat().st_mode), 0o700
            )

    def test_install_locked_detects_patched_asset_swap_before_first_npm_invocation(
        self,
    ) -> None:
        # Regression for independent review round 16, 2026-08-18, P1: round
        # 14 re-verified the two DOWNLOADED release assets (safe_extract_
        # main_asset(), extract_node_toolchain()) immediately before parsing
        # them, but make_patched_asset()'s output -- a THIRD, locally BUILT
        # tarball -- was never re-verified between publication and the
        # point `npm install --package-lock-only` / `npm ci` actually
        # consume it. A same-UID actor who swaps one of these files' on-disk
        # content anywhere in that window used to get it silently used, and
        # because tree_digest() (the receipt's installed-tree fingerprint)
        # runs AFTER install, verify() would have kept passing forever
        # afterward.
        #
        # This test simulates that swap deterministically (matching this
        # file's established convention for TOCTOU tests): a same-UID
        # overwrite of an EARLIER-published workspace asset's on-disk
        # content, injected as a side effect of make_patched_asset()
        # creating the LAST asset (the main "prime-agent" one) -- i.e.
        # entirely before either npm invocation has run at all -- via a
        # real, unmocked run through install() -> _install_locked(). Only
        # genuinely external effects (HTTPS downloads, the pinned node/npm
        # subprocesses, and make_patched_asset()'s own tar-extraction
        # machinery) are faked; run_npm() itself is wired to fail the test
        # outright if it is ever reached, so this proves the swap is caught
        # BEFORE any npm subprocess would have read it, not merely that npm
        # later happens to fail for some other reason.
        #
        # Verified to FAIL against pre-fix (round-15 HEAD) code in an
        # isolated scratch copy: pre-fix, make_patched_asset()'s digest was
        # never re-checked anywhere, so this same swap was silently used and
        # install() proceeded (until failing much later for unrelated
        # reasons, or succeeding outright against a fuller fake).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            swapped_asset_name = str(
                installer.WORKSPACE_PACKAGES["@earendil-works/pi-ai"]["patched_asset"]
            )

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                if managed_name == "prime-agent":
                    # The three workspace assets are all created before the
                    # main "prime-agent" asset (see _install_locked()'s
                    # workspace_order) -- by the time this call fires, a
                    # same-UID racer has had the entire workspace loop's
                    # duration to act on an already-published asset. This
                    # overwrites in place (same inode), the harder case an
                    # identity-only check would miss.
                    swapped_path = assets_dir / swapped_asset_name
                    with open(swapped_path, "r+b") as handle:
                        handle.seek(0)
                        handle.write(b"attacker-controlled-content")
                        handle.truncate()
                published_stat = patched.lstat()
                content_digests = {}
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            def fail_if_run_npm_called(*args, **kwargs):
                self.fail(
                    "run_npm must not be invoked once a patched asset has "
                    "been swapped -- the pre-invocation re-check must fail "
                    "closed first"
                )

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "run_npm", side_effect=fail_if_run_npm_called
                    )
                )

                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "asset changed before use"
                ):
                    installer.install()

            # Fails closed strictly before any pending journal or receipt
            # is ever written.
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_install_locked_detects_patched_asset_swap_before_npm_ci(self) -> None:
        # Regression for independent review round 16, 2026-08-18, P1;
        # complements test_install_locked_detects_patched_asset_swap_before_first_npm_invocation
        # by covering the SECOND window: a same-UID swap injected AFTER
        # `npm install --package-lock-only` has already produced the lock
        # AND validate_generated_lock() has already accepted it (including
        # this round's own new content-digest check inside that function --
        # see test_validate_generated_lock_local_asset_content_drift_is_detected
        # for that check in isolation), but BEFORE `npm ci` -- the
        # invocation that actually installs -- reads the same patched
        # tarball again.
        #
        # This deliberately targets the narrowest possible window: the swap
        # is injected as a side effect of validate_generated_lock() itself
        # returning (wrapping the REAL function, not replacing it), so it
        # lands strictly AFTER that function's own content check has
        # already passed and strictly BEFORE _install_locked()'s dedicated
        # pre-`npm ci` re-check (verify_patched_assets_unchanged()) runs --
        # proving THAT specific re-check independently closes this window
        # on its own, not merely benefiting from validate_generated_lock()
        # having already looked at the (still-legitimate, at that point)
        # content. `npm ci` is wired to fail the test outright if it is
        # ever reached.
        #
        # Verified to FAIL against pre-fix (round-15 HEAD) code in an
        # isolated scratch copy: pre-fix, nothing re-checked any patched
        # asset between validate_generated_lock() and `npm ci`, so this
        # swap was silently used by the simulated `npm ci` step.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)
            swapped_asset_name = installer.MAIN_PATCHED_ASSET

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    self.fail(
                        "npm ci must not run once a patched asset has been "
                        "swapped -- the pre-invocation re-check must fail "
                        "closed first"
                    )

            real_validate_generated_lock = installer.validate_generated_lock

            def swap_immediately_after_lock_validation(raw, generated_lock, patched_asset_sha256):
                result = real_validate_generated_lock(
                    raw, generated_lock, patched_asset_sha256
                )
                # A same-UID actor wins the exact window between
                # validate_generated_lock() accepting the (still
                # legitimate) content and _install_locked()'s dedicated
                # pre-`npm ci` re-check running. In-place overwrite (same
                # inode) -- the harder case an identity-only check would
                # miss.
                swapped_path = release / "assets" / swapped_asset_name
                with open(swapped_path, "r+b") as handle:
                    handle.seek(0)
                    handle.write(b"attacker-controlled-content-for-ci")
                    handle.truncate()
                return result

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = {}
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))
                enter(
                    mock.patch.object(
                        installer,
                        "validate_generated_lock",
                        side_effect=swap_immediately_after_lock_validation,
                    )
                )

                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "asset changed before use"
                ):
                    installer.install()

            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_verify_fails_closed_on_stale_lifecycle_lock_identity(self) -> None:
        # Regression for independent review round 5, 2026-08-18, P2-2:
        # verify() is reached while an ancestor caller's exclusive
        # lifecycle lock is still held (_recover_locked(),
        # _enable_locked()). Confirms a stale caller-supplied identity is
        # detected and fails closed at this critical use point, strictly
        # before any of the heavier release/state checks that follow it in
        # verify() ever run -- while the correct identity lets execution
        # proceed past this check (proven by it then failing on a
        # DIFFERENT, later check this fixture deliberately leaves unmet).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            tool_root.mkdir(mode=0o700)
            lifecycle_lock = tool_root / "lifecycle.lock"
            lifecycle_lock.write_bytes(b"")
            os.chmod(lifecycle_lock, 0o600)
            real_identity = (
                lifecycle_lock.stat().st_dev,
                lifecycle_lock.stat().st_ino,
            )
            unrelated = root / "unrelated-file"
            unrelated.write_bytes(b"")
            stale_identity = (unrelated.stat().st_dev, unrelated.stat().st_ino)
            evidence = {"volume_uuid": "TEST-UUID", "orca_support": {"test": "support"}}
            receipt = {
                "volume_uuid": "TEST-UUID",
                "orca_support": {"test": "support"},
                "lifecycle_lock": os.fspath(lifecycle_lock),
                "release_dir": os.fspath(root / "nonexistent-release"),
            }
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
                mock.patch.object(installer, "preflight", return_value=evidence),
                mock.patch.object(installer, "load_receipt", return_value=receipt),
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "lifecycle lock identity changed while held",
                ):
                    installer.verify(stale_identity)
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "private directory is unavailable"
                ):
                    installer.verify(real_identity)

    def test_verify_unchanged_private_ssd_file_detects_swap_between_verify_and_use(
        self,
    ) -> None:
        # Regression for independent review round 5, 2026-08-18, P2-3:
        # _install_locked() used to read+validate RELEASE_DIR/package.json
        # and RELEASE_DIR/package-lock.json once, then let independently
        # spawned `npm` subprocesses re-read those SAME paths from disk on
        # their own, with no identity binding between the verifying read
        # and the later consuming read. Real repro: publish a file,
        # capture (raw bytes, dev+inode) exactly as _install_locked() now
        # does immediately after publication/validation, then have a
        # same-UID actor replace the file at that exact path with a
        # DIFFERENT-but-still-valid-looking file -- both via a rename (new
        # inode) and via an in-place overwrite (same inode, different
        # bytes) -- and confirm the fix detects both instead of letting a
        # downstream consumer silently read the swapped content.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "release"
            release.mkdir(mode=0o700)
            with mock.patch.object(installer, "SSD_ROOT", root):
                # Case 1: rename-swap (different inode, different content).
                manifest_path = release / "package-a.json"
                original = installer.canonical_json(
                    {"name": "orca-managed-prime-agent", "version": "1.0.0"}
                )
                installer.atomic_create_private_file(manifest_path, original, 0o600)
                manifest_stat = manifest_path.lstat()
                identity = (manifest_stat.st_dev, manifest_stat.st_ino)

                # Sanity: immediately after publication, with nothing
                # changed, the re-check passes.
                installer.verify_unchanged_private_ssd_file(
                    manifest_path, original, identity
                )

                swapped = installer.canonical_json(
                    {"name": "orca-managed-prime-agent", "version": "9.9.9"}
                )
                swap_path = release / ".swap-package-a.json"
                swap_path.write_bytes(swapped)
                os.chmod(swap_path, 0o600)
                os.replace(swap_path, manifest_path)

                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "changed before use"
                ):
                    installer.verify_unchanged_private_ssd_file(
                        manifest_path, original, identity
                    )

                # Case 2: in-place overwrite (same inode, different
                # content) -- the identity check alone would miss this;
                # the content re-check must catch it.
                manifest_path_b = release / "package-b.json"
                original_b = installer.canonical_json(
                    {"name": "orca-managed-prime-agent", "version": "1.0.0"}
                )
                installer.atomic_create_private_file(manifest_path_b, original_b, 0o600)
                stat_b = manifest_path_b.lstat()
                identity_b = (stat_b.st_dev, stat_b.st_ino)
                installer.verify_unchanged_private_ssd_file(
                    manifest_path_b, original_b, identity_b
                )
                overwritten = installer.canonical_json(
                    {"name": "orca-managed-prime-agent", "version": "2.0.0"}
                )
                with open(manifest_path_b, "r+b") as handle:
                    handle.seek(0)
                    handle.write(overwritten)
                    handle.truncate()
                after_stat_b = manifest_path_b.lstat()
                self.assertEqual(
                    (after_stat_b.st_dev, after_stat_b.st_ino), identity_b
                )
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "changed before use"
                ):
                    installer.verify_unchanged_private_ssd_file(
                        manifest_path_b, original_b, identity_b
                    )

    def test_tighten_generated_private_file_mode_fixes_real_npm_umask_output(
        self,
    ) -> None:
        # Regression for round 18, 2026-08-19, P1: a real `npm install
        # --package-lock-only` writes package-lock.json at whatever mode
        # the subprocess's own ambient umask allows -- empirically 0o644
        # under the common umask 0o022 -- because npm has no notion of
        # this installer's private-file discipline. _install_locked() used
        # to hand that file straight to
        # verify_unchanged_private_ssd_file(), whose read_private_file()
        # requires `st_mode & 0o077 == 0`, so every real (non-mocked)
        # install failed closed with "unsafe private file" immediately
        # after the lock was generated. None of the 96 pre-existing unit
        # tests caught this because their fake `npm install` fixtures
        # write the lock file and then immediately `os.chmod(..., 0o600)`
        # it directly, bypassing real npm's actual default-umask output
        # mode entirely.
        #
        # This fixture instead spawns a REAL `/bin/sh` subprocess with the
        # exact umask the bug report empirically measured (022), and lets
        # the KERNEL apply that umask to an unrestricted 0o666 open request
        # the same way any real subprocess (including real npm) actually
        # would -- rather than a lazy `os.chmod(path, 0o644)` stand-in that
        # would only prove the test's own assumption about npm's mode, not
        # exercise the real umask mechanism that produces it.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "release"
            release.mkdir(mode=0o700)
            lock_path = release / "package-lock.json"

            # Step 1: create the file the way a real subprocess with a
            # real umask actually would -- an unrestricted create request,
            # narrowed only by the shell subprocess's own umask, not by
            # this test process's ambient umask (which is unrelated and
            # must not make this test flaky on machines configured
            # differently).
            create = subprocess.run(
                ["/bin/sh", "-c", 'umask 022 && : > "$1"', "sh", os.fspath(lock_path)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(
                create.returncode, 0, f"fixture shell failed: {create.stderr}"
            )
            self.assertTrue(lock_path.is_file())
            observed_created_mode = stat.S_IMODE(lock_path.lstat().st_mode)
            self.assertEqual(
                observed_created_mode,
                0o644,
                "fixture did not reproduce npm's real umask-022 output mode "
                f"(observed {oct(observed_created_mode)}); the rest of this "
                "test would not actually exercise the reported bug",
            )

            # Step 2: write the real generated-lock content into the
            # already-created file, exactly like npm writing its own
            # output into the file it just created -- this must not by
            # itself change the mode captured above (opening an EXISTING
            # path for writing never touches its permission bits, only
            # O_CREAT does).
            generated_lock_raw = installer.canonical_json(
                {"lockfileVersion": 3, "packages": {}}
            )
            with open(lock_path, "r+b") as handle:
                handle.write(generated_lock_raw)
                handle.truncate()
            self.assertEqual(
                stat.S_IMODE(lock_path.lstat().st_mode), 0o644
            )

            before_stat = lock_path.lstat()
            before_identity = (before_stat.st_dev, before_stat.st_ino)

            # RELEASE_DIR's own identity, captured exactly as
            # _install_locked() now captures it: immediately after
            # creation, strictly before any external subprocess runs.
            release_stat = release.lstat()
            release_identity = (release_stat.st_dev, release_stat.st_ino)

            # Sanity: this is really the bug -- the exact, unmodified
            # security gate _install_locked() hands this file to
            # (verify_unchanged_private_ssd_file(), via
            # read_private_file()'s `st_mode & 0o077 == 0` requirement)
            # really does reject a real npm-umask-022 file. If this
            # assertion itself ever stopped failing, the rest of this test
            # would no longer be proving anything about the reported bug.
            with mock.patch.object(installer, "SSD_ROOT", root):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "unsafe private file"
                ):
                    installer.verify_unchanged_private_ssd_file(
                        lock_path, generated_lock_raw, before_identity
                    )

            # Step 3: the actual fix, called exactly the way
            # _install_locked() calls it -- immediately after npm
            # generates the file and before any security re-check reads
            # it. Bound to the SAME dir_fd-chained ancestor walk
            # _install_locked() itself relies on (via
            # open_verified_generated_file_parent()), rooted at SSD_ROOT --
            # mock SSD_ROOT to this fixture's own root so that walk
            # resolves "release"'s real on-disk ancestor chain instead of
            # the real production /Volumes/Extreme SSD path.
            with mock.patch.object(installer, "SSD_ROOT", root):
                returned_identity = installer.tighten_generated_private_file_mode(
                    lock_path, expected_parent_identity=release_identity
                )

            after_stat = lock_path.lstat()
            self.assertEqual(
                stat.S_IMODE(after_stat.st_mode),
                0o600,
                "tighten_generated_private_file_mode() did not tighten the "
                "real npm-umask-022 output to 0o600",
            )
            # Identity (dev, ino) must be the SAME inode npm created --
            # this is an in-place chmod, not a replace.
            self.assertEqual(
                (after_stat.st_dev, after_stat.st_ino), before_identity
            )
            self.assertEqual(returned_identity, before_identity)
            # Content must be completely untouched by the chmod.
            self.assertEqual(lock_path.read_bytes(), generated_lock_raw)

            # Step 4: confirm the FIX actually fixes the real, unmodified
            # downstream security gate -- not that the gate happens to be
            # lenient. Same call as the sanity check above, same file,
            # same expected bytes and identity; only the mode changed.
            with mock.patch.object(installer, "SSD_ROOT", root):
                installer.verify_unchanged_private_ssd_file(
                    lock_path, generated_lock_raw, returned_identity
                )

    def test_tighten_generated_private_file_mode_refuses_symlink(self) -> None:
        # Regression safety net alongside round 18's fix: chmod-by-path
        # follows a symlink, so if a same-UID actor swapped
        # RELEASE_DIR/package-lock.json for a symlink in the instant
        # between npm's write and this call, a bare `os.chmod(path, mode)`
        # would silently tighten (or fabricate a false sense of privacy
        # for) whatever the symlink points at instead of failing closed --
        # exactly the class of gap this file's established
        # O_NOFOLLOW-open-then-fstat-identity-check discipline (see
        # read_private_file()) exists to refuse everywhere else. Confirms
        # tighten_generated_private_file_mode() refuses a symlink outright
        # and never touches the link's target.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            victim = root / "victim-outside-release"
            victim.write_bytes(b"do-not-touch")
            os.chmod(victim, 0o644)
            link_path = root / "release-package-lock.json"
            link_path.symlink_to(victim)

            # link_path's parent IS root/SSD_ROOT itself in this fixture,
            # so root's own identity is the "expected parent" here.
            root_stat = root.lstat()
            root_identity = (root_stat.st_dev, root_stat.st_ino)

            # Bound to the SAME dir_fd-chained ancestor walk
            # _install_locked() itself relies on (via
            # open_verified_generated_file_parent()), rooted at SSD_ROOT --
            # mock SSD_ROOT to this fixture's own root so the walk resolves
            # this fixture's real on-disk ancestor chain instead of the
            # real production /Volumes/Extreme SSD path.
            with mock.patch.object(installer, "SSD_ROOT", root):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "unsafe generated file"
                ):
                    installer.tighten_generated_private_file_mode(
                        link_path, expected_parent_identity=root_identity
                    )

            # The symlink target must be completely untouched -- neither
            # its mode nor its content.
            self.assertEqual(stat.S_IMODE(victim.lstat().st_mode), 0o644)
            self.assertEqual(victim.read_bytes(), b"do-not-touch")

    def test_tighten_generated_private_file_mode_refuses_ancestor_symlink_swap(
        self,
    ) -> None:
        # Regression for independent Codex sol/max round-19 review,
        # 2026-08-19, P1: round 18's fix (and round 19's Claude opus/max
        # review, which tested only leaf-path symlink substitution and
        # lstat-to-open inode swaps AT THE LEAF) only protected the LEAF
        # path component -- Path.lstat() (used to capture "before") follows
        # every INTERMEDIATE symlink in a path, and a plain os.open(path,
        # O_NOFOLLOW) only refuses the FINAL component being a symlink.
        # Neither protects an ancestor DIRECTORY. Reproduces Codex's exact,
        # deterministic repro: RELEASE_DIR itself ("managed/release") is
        # swapped for a symlink pointing at a sibling directory
        # ("managed/outside") that holds its own, completely unrelated
        # package-lock.json -- a same-UID racer has the entire `npm
        # install --package-lock-only` subprocess's runtime window to make
        # this swap before tighten_generated_private_file_mode() runs.
        # Before the fix, this call silently chmod'ed the UNRELATED file in
        # "outside" to 0o600; the fix must instead refuse the ancestor
        # symlink outright (open_verified_generated_file_parent()'s
        # dir_fd-chained, O_NOFOLLOW-protected walk refuses to open a
        # symlinked ancestor component at all) and must never touch
        # "outside" in any way.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            managed = root / "managed"
            managed.mkdir(mode=0o700)
            outside = managed / "outside"
            outside.mkdir(mode=0o700)
            victim = outside / "package-lock.json"
            victim.write_bytes(b"unrelated-lock-do-not-touch")
            os.chmod(victim, 0o644)
            victim_before = victim.lstat()
            victim_before_identity = (victim_before.st_dev, victim_before.st_ino)

            # The same-UID racer's swap: RELEASE_DIR itself
            # ("managed/release") no longer exists as a real directory --
            # it is now a symlink to the sibling "outside" directory.
            release_symlink = managed / "release"
            release_symlink.symlink_to(outside, target_is_directory=True)

            lock_path = release_symlink / "package-lock.json"
            # Sanity: the lexical path really does resolve, through the
            # ancestor symlink, to the unrelated victim file -- if this
            # ever stopped being true the rest of this test would not be
            # exercising the reported bug.
            self.assertEqual(
                lock_path.resolve(strict=True), victim.resolve(strict=True)
            )

            # This fixture never creates a genuine, non-symlinked release
            # directory at all -- "managed/release" is a symlink from the
            # start, simulating the moment right after an attacker's swap.
            # Any placeholder identity works here: the pre-existing
            # symlink refusal inside the dir_fd-chained walk itself must
            # raise while resolving the "release" component, strictly
            # BEFORE the walk ever returns a descriptor for the new
            # post-walk identity comparison to run against -- captured
            # from an unrelated real directory purely so this call has a
            # well-typed value to pass.
            placeholder_stat = managed.lstat()
            placeholder_identity = (placeholder_stat.st_dev, placeholder_stat.st_ino)

            with mock.patch.object(installer, "SSD_ROOT", root):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "cannot inspect link parent|link parent is missing|"
                    "unsafe link parent",
                ):
                    installer.tighten_generated_private_file_mode(
                        lock_path, expected_parent_identity=placeholder_identity
                    )

            # The unrelated file in "outside" must be completely untouched
            # -- neither its mode nor its content -- through the ancestor
            # symlink.
            victim_after = victim.lstat()
            self.assertEqual(
                stat.S_IMODE(victim_after.st_mode),
                0o644,
                "an unrelated file reached only through a swapped ancestor "
                "directory must never be chmod'ed",
            )
            self.assertEqual(
                (victim_after.st_dev, victim_after.st_ino), victim_before_identity
            )
            self.assertEqual(victim.read_bytes(), b"unrelated-lock-do-not-touch")

    def test_tighten_generated_private_file_mode_refuses_ancestor_rename_swap(
        self,
    ) -> None:
        # Regression for independent Codex sol/max round-21 review,
        # 2026-08-19, P1: round 20's fix (open_verified_generated_file_parent()'s
        # dir_fd-chained, O_NOFOLLOW-protected ancestor walk) refuses a
        # SYMLINKED ancestor outright, but every per-component check it
        # performs (real directory, not a symlink, owned by this UID, safe
        # mode) genuinely passes on a same-UID RENAME-swap that replaces
        # RELEASE_DIR with a DIFFERENT, legitimately-owned,
        # correctly-permissioned REAL directory under the same name -- the
        # walk validates properties-in-the-moment, not identity continuity
        # with the RELEASE_DIR this installer itself created before npm
        # ever ran. Reproduces Codex's exact, live-escalation repro: rename
        # the real release directory aside, rename a prepared sibling real
        # directory (containing an unrelated mode-0644 package-lock.json)
        # into the release directory's name, and confirm
        # tighten_generated_private_file_mode() now refuses instead of
        # returning successfully and chmod'ing the unrelated victim file.
        #
        # Verified to FAIL against pre-fix (round-21 HEAD, commit
        # f291e95d97) code in an isolated scratch copy: pre-fix,
        # tighten_generated_private_file_mode() took no
        # expected_parent_identity parameter at all and validated only the
        # replacement directory's current type/owner/mode, so this exact
        # scenario returned successfully, returned the VICTIM's inode, and
        # chmod'ed the unrelated file to 0o600.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "release"
            release.mkdir(mode=0o700)
            lock_path = release / "package-lock.json"
            lock_path.write_bytes(b"real-generated-lock-do-not-touch")
            os.chmod(lock_path, 0o644)

            # Capture RELEASE_DIR's identity exactly like _install_locked()
            # does, immediately after creating it and strictly before any
            # external subprocess (npm) runs.
            release_stat = release.lstat()
            release_identity = (release_stat.st_dev, release_stat.st_ino)

            # The same-UID racer's swap: RELEASE_DIR itself ("release") is
            # renamed aside -- taking the real generated lock with it --
            # and a DIFFERENT, prepared, legitimately-owned,
            # correctly-permissioned REAL directory ("decoy") -- not a
            # symlink -- is renamed into its place, containing its own,
            # completely unrelated, mode-0644 package-lock.json.
            aside = root / "release-real-aside"
            os.rename(release, aside)
            decoy = root / "decoy"
            decoy.mkdir(mode=0o700)
            (decoy / "package-lock.json").write_bytes(b"unrelated-lock-do-not-touch")
            os.chmod(decoy / "package-lock.json", 0o644)
            victim_before = (decoy / "package-lock.json").lstat()
            victim_before_identity = (victim_before.st_dev, victim_before.st_ino)
            os.rename(decoy, release)
            # decoy's directory entry now lives at `release` (the rename
            # moved the whole directory, contents included) -- reference
            # the unrelated file through ITS NEW location for every
            # post-rename check below, not through the old `decoy` path,
            # which no longer names anything on disk.
            victim = release / "package-lock.json"

            # Sanity: the swap is real -- "release" now really is a
            # different directory (different inode) than the one
            # release_identity was captured from, and every per-component
            # property check the existing walk performs on it (real
            # directory, not a symlink, owned by this UID, mode 0o700)
            # genuinely passes -- if any of these ever stopped being true,
            # the rest of this test would not be exercising round 21's
            # reported gap.
            swapped_stat = release.lstat()
            self.assertNotEqual(
                (swapped_stat.st_dev, swapped_stat.st_ino), release_identity
            )
            self.assertFalse(release.is_symlink())
            self.assertEqual(swapped_stat.st_uid, os.getuid())
            self.assertEqual(stat.S_IMODE(swapped_stat.st_mode), 0o700)

            with mock.patch.object(installer, "SSD_ROOT", root):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "ancestor directory identity changed",
                ):
                    installer.tighten_generated_private_file_mode(
                        release / "package-lock.json",
                        expected_parent_identity=release_identity,
                    )

            # The unrelated file in the decoy (now occupying "release")
            # must be completely untouched -- neither its mode nor its
            # content.
            victim_after = victim.lstat()
            self.assertEqual(
                stat.S_IMODE(victim_after.st_mode),
                0o644,
                "an unrelated file reached only through a same-UID "
                "rename-swap of RELEASE_DIR itself must never be chmod'ed",
            )
            self.assertEqual(
                (victim_after.st_dev, victim_after.st_ino), victim_before_identity
            )
            self.assertEqual(victim.read_bytes(), b"unrelated-lock-do-not-touch")

    def test_assert_release_dir_identity_detects_rename_swap(self) -> None:
        # Direct unit coverage for assert_release_dir_identity() in
        # isolation, complementing the full _install_locked() integration
        # regression below
        # (test_install_locked_detects_release_dir_rename_swap_before_post_ci_use):
        # confirms the checkpoint itself both accepts a still-genuine
        # RELEASE_DIR and refuses a same-UID rename-swap that replaced it
        # with a different, legitimately-owned, correctly-permissioned
        # real directory.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "release"
            release.mkdir(mode=0o700)
            release_stat = release.lstat()
            release_identity = (release_stat.st_dev, release_stat.st_ino)

            with mock.patch.object(installer, "RELEASE_DIR", release):
                # The identity has not drifted -- must return quietly.
                installer.assert_release_dir_identity(release_identity)

                aside = root / "release-real-aside"
                os.rename(release, aside)
                decoy = root / "decoy"
                decoy.mkdir(mode=0o700)
                os.rename(decoy, release)

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "release directory identity changed",
                ):
                    installer.assert_release_dir_identity(release_identity)

    def test_install_locked_detects_release_dir_rename_swap_before_post_ci_use(
        self,
    ) -> None:
        # Regression for independent Codex sol/max round-21 review,
        # 2026-08-19, P1 residual: round 21's reported gap
        # (tighten_generated_private_file_mode(), fixed above) was one of
        # TWO points in _install_locked() that trust RELEASE_DIR's identity
        # across a long external `npm` subprocess window without pinning
        # it to the identity captured before that subprocess ever ran.
        # This is the SECOND: everything after `npm ci` (materializing
        # node_modules, moving it to lib/node_modules, chmod'ing the
        # entrypoint, and writing the launch guard + command wrapper
        # scripts) resolved RELEASE_DIR purely by lexical path, with no
        # identity check at all, immediately after the SECOND long
        # external subprocess window (`npm ci`) closed. A same-UID racer
        # who renames RELEASE_DIR aside -- taking its real, freshly
        # `npm ci`'d contents with it -- and renames a different,
        # legitimately-owned, correctly-permissioned real directory into
        # its place at any point during that subprocess's run would
        # otherwise have every one of those steps silently operate on the
        # decoy instead. assert_release_dir_identity(), called immediately
        # after `npm ci` returns and before any of those steps run, must
        # refuse instead.
        #
        # Verified to FAIL against pre-fix (round-21 HEAD, commit
        # f291e95d97) code in an isolated scratch copy: pre-fix, nothing
        # re-checked RELEASE_DIR's own identity between the two npm
        # invocations and the node_modules move, so this swap was silently
        # used -- the fake `npm ci` below installs the swap as a side
        # effect of the SAME call _install_locked() uses to materialize
        # node_modules, so pre-fix, execution reached the node_modules
        # move/chmod/launch-guard-write block operating on the decoy.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            decoy_marker = {}

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_text("// fake cli\n", encoding="utf-8")
                    os.chmod(cli, 0o600)
                    # The same-UID racer's swap, won at the very end of
                    # this subprocess's long run: RELEASE_DIR itself is
                    # renamed aside (taking the just-materialized,
                    # legitimate node_modules with it), and a different,
                    # legitimately-owned, correctly-permissioned real
                    # directory is renamed into its place. The decoy is
                    # deliberately built STRUCTURALLY WELL-FORMED -- its
                    # own real node_modules/prime-agent/dist/bundle/cli.js,
                    # distinguishable only by content -- so that, without
                    # the fix, every downstream step (the
                    # installed_package.is_dir() check, the node_modules
                    # move, the entrypoint chmod, and the launch-guard /
                    # command-wrapper publication) would silently accept
                    # and use it instead of merely failing on an
                    # incidentally-missing directory; this is the same
                    # standard of proof as Codex's original chmod repro,
                    # applied to this second window.
                    aside = cwd.parent / "release-real-aside"
                    os.rename(cwd, aside)
                    decoy = cwd.parent / "decoy-release"
                    decoy_bundle = decoy / "node_modules/prime-agent/dist/bundle"
                    decoy_bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        decoy,
                        decoy / "node_modules",
                        decoy / "node_modules/prime-agent",
                        decoy / "node_modules/prime-agent/dist",
                        decoy_bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    decoy_cli = decoy_bundle / "cli.js"
                    decoy_cli.write_text("// DECOY cli -- do not trust\n", encoding="utf-8")
                    os.chmod(decoy_cli, 0o600)
                    marker_stat = decoy_cli.lstat()
                    decoy_marker["identity"] = (
                        marker_stat.st_dev, marker_stat.st_ino
                    )
                    os.rename(decoy, cwd)
                    # decoy's directory entry now lives at `cwd` (== the
                    # real RELEASE_DIR path) -- reference the decoy cli.js
                    # through ITS NEW location for the post-install
                    # assertions below, not through the old `decoy` path,
                    # which no longer names anything on disk.
                    decoy_marker["path"] = (
                        cwd / "node_modules/prime-agent/dist/bundle/cli.js"
                    )

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = {}
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "release directory identity changed",
                ):
                    installer.install()

            # The decoy's own cli.js -- the file the post-`npm ci` steps
            # would have moved into lib/node_modules and chmod'ed to 0o700
            # had the swap gone undetected -- must be completely untouched:
            # still at 0o600 (never chmod'ed to 0o700), never moved out of
            # node_modules/ into lib/node_modules/, and still holding its
            # own decoy content, not silently accepted as if it were the
            # real npm-ci output.
            marker = decoy_marker["path"]
            marker_stat = marker.lstat()
            self.assertEqual(stat.S_IMODE(marker_stat.st_mode), 0o600)
            self.assertEqual(
                (marker_stat.st_dev, marker_stat.st_ino),
                decoy_marker["identity"],
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "// DECOY cli -- do not trust\n")
            self.assertFalse((release / "lib/node_modules").exists())
            self.assertFalse((release / "bin/prime-agent").exists())
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_install_locked_detects_node_binary_content_swap_between_npm_invocations(
        self,
    ) -> None:
        # Regression for independent Codex sol/max round-23 review,
        # 2026-08-19, P1-1 (highest priority): "pinned node/npm toolchain
        # binaries never re-verified between extraction and execution ...
        # A racer swapping RELEASE_DIR/toolchain/bin/node between the two
        # npm invocations never touches RELEASE_DIR, so round 22's guard
        # passes by construction. Install completes, receipt written,
        # attacker cli.js at 0700, launch guard's baked-in NODE= points at
        # the attacker binary, verify() passes. Measured real window:
        # 3m34s." Round 20/22's RELEASE_DIR-identity guards only ever
        # proved the DIRECTORY itself was not swapped; nothing re-verified
        # a FILE reachable through it, so a same-UID in-place content
        # overwrite of the pinned node binary -- same inode, same lexical
        # path, only the bytes change -- passed every existing check.
        #
        # extract_node_toolchain() now captures node_sha256/npm_cli_sha256
        # directly from the digest-verified tarball's bytes as they are
        # streamed to disk (see that function's own docstring), and
        # _install_locked() re-verifies both, via
        # verify_unchanged_private_ssd_asset_digest(), immediately before
        # EVERY subsequent exec point -- including immediately before the
        # SECOND `npm` invocation (`npm ci`), which is exactly the
        # checkpoint this test targets.
        #
        # Simulates the same-UID racer's in-place content swap (same
        # inode -- the harder case an identity-only check would miss) as a
        # side effect of the FIRST npm invocation (`npm install
        # --package-lock-only`) returning, landing strictly inside the
        # window between the two real npm subprocess invocations the
        # round-23 report measured at 3m34s, entirely before the SECOND
        # invocation's pre-flight re-check runs. `npm ci` is wired to fail
        # the test outright if it is ever reached, proving the swap is
        # caught BEFORE that subprocess would have received the
        # attacker-controlled node path, not merely that installation
        # fails later for some unrelated reason.
        #
        # Verified to FAIL against pre-fix HEAD (commit 4d73ffd192) in an
        # isolated scratch copy: pre-fix, extract_node_toolchain() returned
        # a bare (node, npm_cli) 2-tuple with no digest at all, and nothing
        # in _install_locked() ever re-read either file's content between
        # extraction and either npm invocation, so this swap went
        # completely undetected and the simulated `npm ci` branch below
        # would have been reached with the attacker-controlled node path.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                    # The same-UID racer's swap, won during this exact
                    # subprocess's long real-world run (measured: 3m34s):
                    # the pinned node binary's CONTENT is overwritten in
                    # place -- same inode, same lexical path -- not
                    # RELEASE_DIR itself, and every structural property
                    # (private, owner-only, non-symlinked regular file)
                    # left completely intact.
                    node = Path(node_path)
                    with open(node, "r+b") as handle:
                        handle.seek(0)
                        handle.write(
                            b"#!/bin/sh\necho attacker-controlled-node\n"
                        )
                        handle.truncate()
                elif args and args[0] == "ci":
                    self.fail(
                        "npm ci must not run once the pinned node binary's "
                        "content has been swapped -- the pre-invocation "
                        "re-check must fail closed first"
                    )

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = {}
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))

                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "asset changed before use"
                ):
                    installer.install()

            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_install_locked_detects_entrypoint_content_swap_after_npm_ci(
        self,
    ) -> None:
        # Regression for independent Codex sol/max round-23 review,
        # 2026-08-19, P1-2: "assert_release_dir_identity() binds the
        # directory's inode, not its contents: Overwriting node_modules/
        # prime-agent/dist/bundle/cli.js between npm ci returning and [the
        # later publication step] passes the new guard trivially. npm ci
        # output is never content-verified; tree_digest() records rather
        # than compares." RELEASE_DIR's identity checks (round 20/22, still
        # in place) only prove the DIRECTORY was never swapped; they say
        # nothing about a same-UID actor rewriting a FILE reachable through
        # it, and tree_digest() (write_pending_install()/
        # finalize_pending_install()) only ever RECORDS whatever is on disk
        # at its own, much LATER call time -- after the node_modules move,
        # the entrypoint chmod, and the launch-guard/command-wrapper
        # publication all already ran -- rather than comparing against a
        # value captured before this window, so it would silently treat a
        # swap that happened before its first call as the legitimate
        # baseline forever after.
        #
        # capture_private_ssd_asset_digest() now anchors the entrypoint's
        # content to the EARLIEST point it can be known -- immediately
        # after `npm ci` returns, strictly before the node_modules move --
        # and _install_locked() re-verifies that anchor, via
        # verify_unchanged_private_ssd_asset_digest(), immediately after
        # the move and again immediately before it is baked into the
        # launch guard as a permanent future trust anchor. This test wraps
        # the REAL capture_private_ssd_asset_digest() (not a stub) so the
        # captured digest genuinely reflects the real, legitimate content
        # at capture time, then overwrites cli.js's bytes in place (same
        # inode -- the harder case an identity-only check would miss) as a
        # side effect of that same call returning -- landing exactly in
        # the window the round-23 report describes, strictly before
        # _install_locked()'s own post-move re-check runs.
        #
        # Verified to FAIL against pre-fix HEAD (commit 4d73ffd192) in an
        # isolated scratch copy: pre-fix, nothing re-checked the
        # entrypoint's content between `npm ci` returning and publication
        # at all (only RELEASE_DIR's own identity was re-asserted), so this
        # swap would have been moved into lib/node_modules, chmod'ed to
        # 0o700, and baked into the launch guard's CLI= constant as if it
        # were the genuine npm-ci output.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_text("// genuine npm-ci cli.js\n", encoding="utf-8")
                    os.chmod(cli, 0o600)

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {Path("dist/bundle/cli.js"): installer.sha256_bytes(b"// genuine npm-ci cli.js\n")}
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            real_capture_private_ssd_asset_digest = (
                installer.capture_private_ssd_asset_digest
            )
            swap_fired = {"done": False}

            def swap_entrypoint_immediately_after_digest_capture(path):
                result = real_capture_private_ssd_asset_digest(path)
                # The same-UID racer's swap, won in the exact window
                # between this capture (the earliest point cli.js's
                # legitimate content can be known -- immediately after
                # `npm ci` returned) and _install_locked()'s own
                # post-move/pre-publication re-check. In-place overwrite
                # (same inode) -- the harder case an identity-only check
                # would miss.
                with open(path, "r+b") as handle:
                    handle.seek(0)
                    handle.write(
                        b"// ATTACKER-CONTROLLED cli.js -- do not trust\n"
                    )
                    handle.truncate()
                swap_fired["done"] = True
                return result

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))
                enter(
                    mock.patch.object(
                        installer,
                        "capture_private_ssd_asset_digest",
                        side_effect=swap_entrypoint_immediately_after_digest_capture,
                    )
                )

                with self.assertRaisesRegex(
                    installer.PrimeInstallError, "asset changed before use"
                ):
                    installer.install()

            # The hook must actually have fired -- otherwise this test
            # would trivially pass without exercising the swap at all.
            self.assertTrue(swap_fired["done"])
            self.assertFalse((release / "bin/prime-agent-launch-guard.py").exists())
            self.assertFalse((release / "bin/prime-agent").exists())
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_install_locked_detects_entrypoint_content_swap_during_npm_ci_window(
        self,
    ) -> None:
        # Regression for independent Claude opus/max round-25 review,
        # 2026-08-19, R25-P1-A: "The entrypoint's trust baseline is taken
        # from untrusted post-npm-ci disk state ... Round 24's own
        # docstring asserts the capture point is 'the EARLIEST point its
        # final, trustworthy value can be known'. That is empirically
        # false. A same-UID racer who overwrites node_modules/prime-agent/
        # dist/bundle/cli.js DURING npm ci's own runtime (rather than
        # after it returns) has their bytes adopted as the baseline.
        # Repro: install COMPLETES, attacker's cli.js is published,
        # CLI_SHA256 in the launch guard equals the attacker's digest, and
        # receipt['entrypoint_sha256'] records it -- trusted permanently
        # by every future verify() and invocation."
        #
        # This is deliberately a DIFFERENT scenario from
        # test_install_locked_detects_entrypoint_content_swap_after_npm_ci
        # above: that test wraps the REAL capture_private_ssd_asset_digest()
        # so a LEGITIMATE value is captured first, then swaps the on-disk
        # bytes as a side effect of that same call returning -- proving a
        # swap AFTER the capture point is caught by the later post-move
        # re-verify. This test instead never lets a legitimate value exist
        # on disk at all: fake_run_npm's "ci" branch materializes the
        # ATTACKER's bytes directly, simulating a same-UID racer who won
        # the swap during `npm ci`'s own multi-minute subprocess window,
        # so the very FIRST disk read after `npm ci` returns already
        # observes attacker-controlled content. Pre-fix (round 24, commit
        # 4ecd34b2bd), capture_private_ssd_asset_digest() would have
        # captured exactly this attacker digest and trusted it outright as
        # `entrypoint_sha256` -- nothing compared it against anything
        # independent of what was on disk at that moment, so this test
        # would observe install() completing successfully with the
        # attacker's digest baked into the receipt and launch guard.
        #
        # The round-25 fix threads make_patched_asset()'s content_digests
        # return value (captured from the digest-verified ORIGINAL tarball,
        # strictly before `npm ci` ever ran) through as the entrypoint's
        # trust baseline, and compares the freshly observed post-`npm ci`
        # digest against THAT pinned value instead of trusting it outright
        # -- closing the window regardless of when the swap happened. This
        # test's fake_make_patched_asset returns a content_digests entry
        # for dist/bundle/cli.js that reflects the GENUINE tarball content
        # (a different byte string than what fake_run_npm actually
        # materializes), so the fix's comparison must fail.
        #
        # Verified to FAIL against pre-fix HEAD (commit 4ecd34b2bd) in an
        # isolated scratch copy: pre-fix, install() completes successfully
        # for this exact fixture (no comparison against any pinned
        # baseline existed), so this test's assertRaisesRegex block would
        # itself fail with "PrimeInstallError not raised".
        #
        # Round 27, 2026-08-19: this same swap is now caught by
        # assert_locally_patched_package_matches_pinned_digests() (the P1
        # fix's complete, per-file sweep of all four locally patched
        # packages, which now runs BEFORE the round-24/25 entrypoint-
        # specific capture/compare this test originally targeted) rather
        # than by the entrypoint-specific comparison itself -- the error
        # message below reflects that broader check firing first. The
        # round-24/25 mechanism this test used to isolate is still
        # independently exercised, unchanged, by
        # test_install_locked_detects_entrypoint_content_swap_after_npm_ci
        # above (which wraps the real capture_private_ssd_asset_digest()
        # rather than pre-materializing tampered content, so it fails
        # closed on the round-24/25 comparison specifically even now that
        # the round-27 sweep also runs).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            GENUINE_CLI_CONTENT = b"// genuine, tarball-pinned cli.js\n"
            ATTACKER_CLI_CONTENT = (
                b"// ATTACKER cli.js, planted DURING npm ci's own runtime\n"
            )

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    # Never a legitimate value at any point -- the
                    # attacker's bytes are the ONLY content this path ever
                    # holds, simulating a same-UID racer who won the swap
                    # WHILE `npm ci` was still running, before it returned
                    # control to this installer at all.
                    cli.write_bytes(ATTACKER_CLI_CONTENT)
                    os.chmod(cli, 0o600)

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                # The pinned, tarball-derived baseline for the "prime-agent"
                # asset's entrypoint -- deliberately the GENUINE content,
                # never the attacker's, exactly as safe_extract_main_asset()
                # would have captured it from the real, digest-verified
                # tarball strictly before `npm ci` ever ran.
                content_digests = (
                    {Path("dist/bundle/cli.js"): installer.sha256_bytes(GENUINE_CLI_CONTENT)}
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    r"prime-agent materialized file content does not match the "
                    r"digest-verified original tarball: dist/bundle/cli\.js",
                ):
                    installer.install()

            self.assertFalse((release / "lib/node_modules").exists())
            self.assertFalse((release / "bin/prime-agent-launch-guard.py").exists())
            self.assertFalse((release / "bin/prime-agent").exists())
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_install_locked_detects_undeclared_sibling_package_planted_during_npm_ci(
        self,
    ) -> None:
        # Regression for independent Claude opus/max round-25 review,
        # 2026-08-19, R25-P1-B: "run_npm()'s before/after bracketing is
        # blind to a swap that is undone inside npm's window ... This is
        # independent of P1-A: with the racer planting the genuine cli.js
        # bytes plus one malicious sibling module, the install still
        # completes and publishes the malicious module -- so per-file
        # cli.js digesting alone would not close it." A same-UID local
        # attacker with filesystem write access for `npm ci`'s own
        # multi-minute runtime can plant an entirely new, undeclared
        # package directory directly under node_modules/ without ever
        # touching RELEASE_DIR's own directory entry, so none of
        # assert_release_dir_identity()/assert_release_dir_fd_identity()'s
        # several call sites would ever raise for it, and (per this
        # finding) closing P1-A alone would not catch it either, since the
        # genuine, correctly-pinned cli.js is published unmodified
        # alongside the planted sibling.
        #
        # This test's fake_run_npm's "ci" branch materializes the
        # LEGITIMATE prime-agent tree (genuine cli.js content, matching
        # fake_make_patched_asset's pinned baseline exactly, so R25-P1-A's
        # check passes cleanly and this test isolates R25-P1-B alone)
        # PLUS one extra, entirely undeclared top-level package directory
        # -- "evil-sibling-package" -- that is not, and never was, part of
        # the verified lock's declared closure.
        #
        # assert_materialized_node_modules_matches_lock() (round 25, P1-B)
        # compares the SET of top-level node_modules/ directory names
        # `npm ci` actually materialized against the set
        # declared_top_level_node_modules_packages() derives from the SAME
        # lock file content this install is already pinned to, and fails
        # closed on anything materialized that was never declared.
        #
        # Verified to FAIL against pre-fix HEAD (commit 4ecd34b2bd, and
        # against this round's own R25-P1-A-only fix in isolation) in an
        # isolated scratch copy: neither release ever compared the
        # materialized node_modules/ directory listing against the
        # declared lock closure, so this planted sibling package would
        # have been silently moved into lib/node_modules/ and published as
        # part of a "successful" install.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            GENUINE_CLI_CONTENT = b"// genuine, tarball-pinned cli.js\n"

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_bytes(GENUINE_CLI_CONTENT)
                    os.chmod(cli, 0o600)
                    # The same-UID racer's plant, won at any point during
                    # this exact subprocess's long run: an entirely new,
                    # undeclared top-level package directory, never part
                    # of the verified lock's closure, sitting directly
                    # under node_modules/ alongside the genuine,
                    # correctly-pinned prime-agent tree.
                    evil = cwd / "node_modules/evil-sibling-package"
                    evil.mkdir(parents=True, mode=0o700)
                    (evil / "package.json").write_text(
                        installer.canonical_json(
                            {"name": "evil-sibling-package", "version": "1.0.0"}
                        ).decode("utf-8"),
                        encoding="utf-8",
                    )
                    os.chmod(evil / "package.json", 0o600)
                    (evil / "index.js").write_text(
                        "// attacker-controlled sibling module\n", encoding="utf-8"
                    )
                    os.chmod(evil / "index.js", 0o600)

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {Path("dist/bundle/cli.js"): installer.sha256_bytes(GENUINE_CLI_CONTENT)}
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    r"npm materialized undeclared node_modules package\(s\)",
                ):
                    installer.install()

            self.assertFalse((release / "lib/node_modules").exists())
            self.assertFalse((release / "bin/prime-agent-launch-guard.py").exists())
            self.assertFalse((release / "bin/prime-agent").exists())
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_install_locked_detects_tampered_non_entrypoint_sibling_file(
        self,
    ) -> None:
        # Regression for independent Claude opus/max round-27 review,
        # 2026-08-19, P1: "The pinned entrypoint digest mechanism ... covers
        # only dist/bundle/cli.js -- a 1,813-byte ESM forwarding stub that
        # is 0.013% of the 39-file/13,804,347-byte dist/bundle/ directory it
        # ships alongside. cli.js statically imports one chunk ... and
        # dynamically imports the real CLI main module at runtime -- both
        # execute unconditionally on every invocation, and neither is
        # covered by anything ... Reproduced live: tampering with the
        # sibling chunk files during npm ci's window while leaving cli.js
        # itself genuine still completes install with a clean launch guard
        # and receipt -- permanent RCE on every future managed invocation."
        #
        # This test's fake_make_patched_asset returns a content_digests map
        # for "prime-agent" that pins BOTH dist/bundle/cli.js AND a second,
        # non-entrypoint file (dist/bundle/chunk-real.js) that cli.js
        # imports -- exactly like the real, digest-verified original
        # tarball would. fake_run_npm's "ci" branch materializes cli.js
        # with the GENUINE, correctly-pinned content, but materializes
        # chunk-real.js with DIFFERENT, attacker-controlled bytes --
        # isolating the P1 repro: a same-UID racer who leaves the
        # entrypoint alone and only tampers with what it imports.
        #
        # Verified to FAIL against pre-fix HEAD (commit c1ca06e61a) in an
        # isolated scratch copy: pre-fix, content_digests for the three
        # entries other than dist/bundle/cli.js were computed by
        # make_patched_asset() but never threaded anywhere, and
        # assert_materialized_node_modules_matches_lock() only ever checked
        # directory PRESENCE -- so this exact fixture (genuine cli.js,
        # tampered sibling chunk) completed install() successfully with the
        # attacker's chunk-real.js content published unmodified into
        # lib/node_modules, no comparison against any pinned baseline for
        # that file ever performed.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            GENUINE_CLI_CONTENT = b"// genuine, tarball-pinned cli.js\n"
            GENUINE_CHUNK_CONTENT = b"// genuine, tarball-pinned chunk-real.js\n"
            ATTACKER_CHUNK_CONTENT = (
                b"// ATTACKER chunk-real.js, tampered during npm ci's own runtime\n"
            )

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    # The entrypoint itself is genuine -- the repro is
                    # specifically that leaving cli.js untouched and only
                    # tampering with what it imports must still be caught.
                    cli = bundle / "cli.js"
                    cli.write_bytes(GENUINE_CLI_CONTENT)
                    os.chmod(cli, 0o600)
                    # The same-UID racer's plant: a sibling chunk cli.js
                    # imports, tampered in place during npm ci's own window.
                    chunk = bundle / "chunk-real.js"
                    chunk.write_bytes(ATTACKER_CHUNK_CONTENT)
                    os.chmod(chunk, 0o600)

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                # The pinned, tarball-derived baseline for BOTH of
                # prime-agent's dist/bundle/ files -- exactly as
                # safe_extract_main_asset() would have captured them from
                # the real, digest-verified tarball strictly before `npm
                # ci` ever ran. Deliberately includes chunk-real.js's
                # GENUINE content, never the attacker's.
                content_digests = (
                    {
                        Path("dist/bundle/cli.js"): installer.sha256_bytes(
                            GENUINE_CLI_CONTENT
                        ),
                        Path("dist/bundle/chunk-real.js"): installer.sha256_bytes(
                            GENUINE_CHUNK_CONTENT
                        ),
                    }
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    r"prime-agent materialized file content does not match the "
                    r"digest-verified original tarball: dist/bundle/chunk-real\.js",
                ):
                    installer.install()

            self.assertFalse((release / "lib/node_modules").exists())
            self.assertFalse((release / "bin/prime-agent-launch-guard.py").exists())
            self.assertFalse((release / "bin/prime-agent").exists())
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_install_locked_detects_planted_non_directory_top_level_entry(
        self,
    ) -> None:
        # Regression for independent Claude opus/max round-27 review,
        # 2026-08-19, P2-1: "assert_materialized_node_modules_matches_lock()
        # skips non-directory top-level entries entirely (`if not
        # entry.is_dir(): continue` ...) -- a planted top-level FILE (e.g.
        # node_modules/some-package.js) is an undeclared entry planted
        # during npm ci and is never added to the 'materialized' set, so
        # it's silently invisible to the check that's supposed to catch
        # exactly this. ... Node's CJS LOAD_AS_FILE(DIR/X) resolution
        # precedes LOAD_AS_DIRECTORY(DIR/X), so a same-named .js file
        # shadows a directory package for any package lacking an 'exports'
        # field."
        #
        # fake_run_npm's "ci" branch materializes the legitimate prime-agent
        # tree (genuine cli.js, matching fake_make_patched_asset's pinned
        # baseline) PLUS one extra, non-directory top-level entry --
        # "evil-shadow.js" -- planted directly under node_modules/,
        # simulating a same-UID racer's file-based shadow plant.
        #
        # Verified to FAIL against pre-fix HEAD (commit c1ca06e61a) in an
        # isolated scratch copy: pre-fix,
        # assert_materialized_node_modules_matches_lock()'s top-level walk
        # unconditionally skipped any non-directory entry
        # (`if not entry.is_dir(): continue`), so this planted file was
        # never added to the "materialized" set and never appeared in the
        # unexpected-entries diff -- install() completed successfully with
        # the planted file published unmodified into lib/node_modules.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            GENUINE_CLI_CONTENT = b"// genuine, tarball-pinned cli.js\n"

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_bytes(GENUINE_CLI_CONTENT)
                    os.chmod(cli, 0o600)
                    # The same-UID racer's plant: a non-directory entry
                    # directly at the top level of node_modules/, never
                    # part of the verified lock's declared closure and
                    # never one of KNOWN_NON_DIRECTORY_NODE_MODULES_ENTRIES.
                    shadow = cwd / "node_modules/evil-shadow.js"
                    shadow.write_text(
                        "// attacker-controlled shadow file\n", encoding="utf-8"
                    )
                    os.chmod(shadow, 0o600)

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {Path("dist/bundle/cli.js"): installer.sha256_bytes(GENUINE_CLI_CONTENT)}
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    r"unexpected non-directory entry where a declared package "
                    r"name was expected: evil-shadow\.js",
                ):
                    installer.install()

            self.assertFalse((release / "lib/node_modules").exists())
            self.assertFalse((release / "bin/prime-agent-launch-guard.py").exists())
            self.assertFalse((release / "bin/prime-agent").exists())
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_install_locked_detects_undeclared_package_in_nested_node_modules(
        self,
    ) -> None:
        # Regression for independent Claude opus/max round-27 review,
        # 2026-08-19, P2-2: "Nested node_modules/ (a package's own
        # sub-dependencies) is never walked -- 9 real nested node_modules/
        # directories materialize in a real install, and the lock declares
        # 12 nested rows, but
        # assert_materialized_node_modules_matches_lock() only checks depth
        # 1. The same attack ... one directory deeper completely bypasses
        # the round-26 check." (Empirically confirmed via a real darwin-
        # arm64 `npm ci` replay of this installer's own pinned lock, round
        # 27, 2026-08-19: exactly 9 materialized nested node_modules/
        # containers against 12 declared nested rows, and the materialized
        # contents of every one matched its declared rows exactly.)
        #
        # This test plants the undeclared package inside a REGISTRY
        # (non-locally-patched) declared package's own nested node_modules/
        # -- "some-registry-dep" -- deliberately NOT inside one of the four
        # locally patched packages, so this isolates P2-2 cleanly: a plant
        # inside prime-agent's own tree would also be caught by this same
        # round's P1 fix (assert_locally_patched_package_matches_pinned_
        # digests()' fully recursive walk), which would make it impossible
        # to tell which mechanism actually caught it.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            registry_dep_row = {
                "name": "some-registry-dep",
                "version": "1.0.0",
                "resolved": (
                    "https://registry.npmjs.org/some-registry-dep/-/"
                    "some-registry-dep-1.0.0.tgz"
                ),
                "integrity": "sha512-dGVzdA==",
            }
            nested_subdep_row = {
                "name": "its-legit-subdep",
                "version": "2.0.0",
                "resolved": (
                    "https://registry.npmjs.org/its-legit-subdep/-/"
                    "its-legit-subdep-2.0.0.tgz"
                ),
                "integrity": "sha512-dGVzdA==",
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                    "node_modules/some-registry-dep": registry_dep_row,
                    "node_modules/some-registry-dep/node_modules/its-legit-subdep": (
                        nested_subdep_row
                    ),
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            GENUINE_CLI_CONTENT = b"// genuine, tarball-pinned cli.js\n"

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_bytes(GENUINE_CLI_CONTENT)
                    os.chmod(cli, 0o600)
                    # The declared registry dependency and its ONE declared
                    # nested sub-dependency -- both legitimate, matching the
                    # lock exactly.
                    legit_nested = (
                        cwd
                        / "node_modules/some-registry-dep/node_modules"
                        / "its-legit-subdep"
                    )
                    legit_nested.mkdir(parents=True, mode=0o700)
                    os.chmod(cwd / "node_modules/some-registry-dep", 0o700)
                    os.chmod(
                        cwd / "node_modules/some-registry-dep/node_modules", 0o700
                    )
                    # The same-UID racer's plant: an ENTIRELY UNDECLARED
                    # package sitting one directory deeper than the
                    # round-25/26 check ever looked -- inside a legitimately
                    # declared package's own nested node_modules/, planted
                    # during npm ci's own multi-minute runtime, without
                    # touching RELEASE_DIR's own directory entry or any
                    # top-level node_modules/ entry at all.
                    evil_nested = (
                        cwd
                        / "node_modules/some-registry-dep/node_modules"
                        / "evil-nested-package"
                    )
                    evil_nested.mkdir(parents=True, mode=0o700)
                    (evil_nested / "index.js").write_text(
                        "// attacker-controlled nested module\n", encoding="utf-8"
                    )
                    os.chmod(evil_nested / "index.js", 0o600)

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {Path("dist/bundle/cli.js"): installer.sha256_bytes(GENUINE_CLI_CONTENT)}
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "GENERATED_LOCK_PACKAGE_COUNT",
                        len(local_assets) + 2,
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    r"npm materialized undeclared nested node_modules package\(s\) "
                    r"not present in the verified lock inside "
                    r"node_modules/some-registry-dep/node_modules: "
                    r"\['evil-nested-package'\]",
                ):
                    installer.install()

            self.assertFalse((release / "lib/node_modules").exists())
            self.assertFalse((release / "bin/prime-agent-launch-guard.py").exists())
            self.assertFalse((release / "bin/prime-agent").exists())
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_install_locked_detects_late_tamper_after_digest_sweep_before_baseline_lock(
        self,
    ) -> None:
        # Regression for independent Claude opus/max round-29 review,
        # 2026-08-19, P1-1: "The round-28 full-tree digest sweep
        # (assert_locally_patched_package_matches_pinned_digests(), called
        # once per package) verifies content correctly, but its result is
        # trusted across a real, externally-observable window before the
        # release baseline is established. os.mkdir('lib', ...) runs AFTER
        # the sweep and is a public signal an attacker can poll for.
        # tree_digest()'s later call is where the baseline actually gets
        # locked in via write_pending_install() -- and at that point it
        # RECORDS whatever it finds, it does not COMPARE against the
        # sweep's already-verified state. Measured real window: ~6.8
        # seconds ... Reproduced end-to-end on a real install: a racer
        # thread polling for RELEASE_DIR/lib's appearance wins the race,
        # tampers a file inside one of the 4 locally-patched packages,
        # install completes, receipt is written with the attacker's
        # content, verify() reports OK, the launch guard executes the
        # attacker's code."
        #
        # This test wraps the REAL
        # assert_locally_patched_package_matches_pinned_digests() (not a
        # stub) so BOTH per-package sweep calls this round's fix performs
        # (the original, pre-move call right after `npm ci` returns, and
        # the NEW post-move re-sweep immediately before
        # write_pending_install()) genuinely run and genuinely pass -- then,
        # as a side effect of the SECOND (post-move) "prime-agent" sweep
        # call returning successfully, tampers a NON-entrypoint sibling
        # file (chunk-real.js) within that same package's tree in place.
        # This deliberately isolates tree_digest()'s own new
        # `pinned_relative_digests` comparison (the fix that actually
        # closes this round's P1-1 finding) from the re-sweep fix: by
        # tampering strictly AFTER the LAST sweep call has already passed,
        # the only remaining check that can catch it is
        # write_pending_install()'s tree_digest() call. Isolated from
        # cli.js on purpose (as in the round-27 sibling-file test) so this
        # test cannot be accidentally satisfied by the unrelated
        # entrypoint-specific capture/compare mechanism.
        #
        # Verified to FAIL against pre-fix HEAD (commit ea8004f820) in an
        # isolated scratch copy: pre-fix, assert_locally_patched_package_
        # matches_pinned_digests() was called only ONCE per package (no
        # post-move re-sweep existed), and write_pending_install()'s first
        # tree_digest() call unconditionally RECORDED whatever was on disk
        # at its own, later call time with no comparison against anything
        # the sweep had already verified -- so this exact tamper, landing
        # after the (only) sweep call passed and before that recording,
        # would have been silently baked into receipt['release_tree_sha256']
        # as the legitimate baseline, and install() would have completed
        # successfully.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            GENUINE_CLI_CONTENT = b"// genuine, tarball-pinned cli.js\n"
            GENUINE_CHUNK_CONTENT = b"// genuine, tarball-pinned chunk-real.js\n"
            ATTACKER_CHUNK_CONTENT = (
                b"// ATTACKER chunk-real.js, planted AFTER every digest "
                b"sweep passed, BEFORE the release baseline was locked in\n"
            )

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_bytes(GENUINE_CLI_CONTENT)
                    os.chmod(cli, 0o600)
                    # Genuine at materialization time -- the racer's own
                    # tamper (below, via the wrapped sweep) happens
                    # strictly AFTER every real sweep call already
                    # verified this exact content.
                    chunk = bundle / "chunk-real.js"
                    chunk.write_bytes(GENUINE_CHUNK_CONTENT)
                    os.chmod(chunk, 0o600)

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {
                        Path("dist/bundle/cli.js"): installer.sha256_bytes(
                            GENUINE_CLI_CONTENT
                        ),
                        Path("dist/bundle/chunk-real.js"): installer.sha256_bytes(
                            GENUINE_CHUNK_CONTENT
                        ),
                    }
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            real_sweep = installer.assert_locally_patched_package_matches_pinned_digests
            call_count = {"prime-agent": 0}
            tamper_fired = {"done": False}

            def sweep_then_tamper_prime_agent(package_dir, pinned_digests, package_label):
                result = real_sweep(package_dir, pinned_digests, package_label)
                if package_label == "prime-agent":
                    call_count["prime-agent"] += 1
                    # Tamper only after the SECOND call (this round's new
                    # post-move re-sweep, immediately before
                    # write_pending_install()) -- not the first (the
                    # original, pre-move call). This isolates
                    # tree_digest()'s own comparison as the ONLY remaining
                    # check that can still catch the tamper: no further
                    # sweep call runs after this one.
                    if call_count["prime-agent"] == 2 and not tamper_fired["done"]:
                        chunk_path = package_dir / "dist/bundle/chunk-real.js"
                        with open(chunk_path, "r+b") as handle:
                            handle.seek(0)
                            handle.write(ATTACKER_CHUNK_CONTENT)
                            handle.truncate()
                        tamper_fired["done"] = True
                return result

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))
                enter(
                    mock.patch.object(
                        installer,
                        "assert_locally_patched_package_matches_pinned_digests",
                        side_effect=sweep_then_tamper_prime_agent,
                    )
                )

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    r"release tree content does not match its digest-verified "
                    r"pre-npm-ci pinned baseline: "
                    r"lib/node_modules/prime-agent/dist/bundle/chunk-real\.js",
                ):
                    installer.install()

            self.assertEqual(call_count["prime-agent"], 2)
            self.assertTrue(tamper_fired["done"])
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def _round32_tamper_generated_exec_target_harness(
        self, *, target_relative: str, error_regex: str
    ) -> None:
        """Shared harness for the two round-32 regression tests just below.

        Regression for independent Claude opus/max round-31 review, 2026-08-19,
        P1: "round 30's pinned-digest mechanism ... covers only the 4
        locally-patched packages' files plus node/npm-cli, and OMITS the two
        files the installer itself generates and that actually get executed
        on every managed prime-agent invocation: the launch guard
        (bin/prime-agent-launch-guard.py) and the command wrapper
        (bin/prime-agent). Both get read-back verified at creation time via
        atomic_create_private_file()'s own mechanism, but ... their content
        only becomes the permanent, trusted release baseline later, when
        tree_digest() reaches them during write_pending_install(), with zero
        comparison performed ... A same-UID racer overwriting either file in
        the window between creation and tree_digest() reaching it ... gets
        the tampered bytes adopted as the permanent
        receipt['release_tree_sha256'] ... permanent RCE on every future
        managed invocation, verify() reports OK forever."

        Reuses round 29/30's own real-`install()`-plus-wrapped-function
        regression idiom (see
        test_install_locked_detects_late_tamper_after_digest_sweep_before_baseline_lock
        just above), but targets `atomic_create_private_file()` itself --
        the single choke point BOTH the launch guard and the command
        wrapper are published through -- rather than the per-package digest
        sweep round 29/30's own tests target: this wraps the REAL
        `atomic_create_private_file()` (not a stub) so every file this
        install genuinely publishes is genuinely published, then, as a side
        effect of the call that publishes `target_relative` specifically,
        overwrites that file's on-disk bytes IN PLACE (same inode, same
        mode -- an in-place same-UID content swap, not a replace) --
        exactly modeling a same-UID racer who wins the window between that
        publication and write_pending_install()'s later tree_digest() call
        reaching the same path.

        Verified to FAIL against pre-fix HEAD (commit 035ef5dad5) in an
        isolated scratch copy for BOTH target files: pre-fix,
        `release_relative_pinned_digests` (built in
        _install_locked_within_release_dir(), just before
        write_pending_install()) contained only node's, npm-cli's, and the
        four locally patched packages' own tarball-declared members --
        never `bin/prime-agent-launch-guard.py` or `bin/prime-agent` -- so
        tree_digest()'s pinned-digest comparison had no entry to compare
        either path against and simply recorded the already-tampered bytes
        as the permanent baseline; `installer.install()` completed
        successfully with a clean receipt in both cases pre-fix, instead of
        raising.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            GENUINE_CLI_CONTENT = b"// genuine, tarball-pinned cli.js\n"
            ATTACKER_CONTENT = (
                b"#!/bin/sh\n"
                b"# ATTACKER-controlled content, planted AFTER "
                b"atomic_create_private_file() published the genuine bytes and "
                b"BEFORE write_pending_install()'s tree_digest() call locked in "
                b"the release baseline.\n"
                b"exec /bin/sh -c 'echo pwned'\n"
            )

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_bytes(GENUINE_CLI_CONTENT)
                    os.chmod(cli, 0o600)

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {
                        Path("dist/bundle/cli.js"): installer.sha256_bytes(
                            GENUINE_CLI_CONTENT
                        ),
                    }
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            real_atomic_create_private_file = installer.atomic_create_private_file
            tamper_fired = {"done": False}
            target_path = release / target_relative

            def tamper_after_publish(path, raw, mode=0o600):
                real_atomic_create_private_file(path, raw, mode)
                if path == target_path and not tamper_fired["done"]:
                    # In-place same-UID content swap -- same inode, same
                    # mode -- exactly modeling a same-UID racer overwriting
                    # the just-published file rather than replacing it, so
                    # this cannot be mistaken for a structural
                    # (identity/symlink) tamper the existing checks would
                    # already catch a different way.
                    with open(path, "r+b") as handle:
                        handle.seek(0)
                        handle.write(ATTACKER_CONTENT)
                        handle.truncate()
                    tamper_fired["done"] = True

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))
                enter(
                    mock.patch.object(
                        installer,
                        "atomic_create_private_file",
                        side_effect=tamper_after_publish,
                    )
                )

                with self.assertRaisesRegex(installer.PrimeInstallError, error_regex):
                    installer.install()

            self.assertTrue(tamper_fired["done"])
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_install_locked_detects_launch_guard_tamper_before_baseline_lock(
        self,
    ) -> None:
        # Regression for independent Claude opus/max round-31 review,
        # 2026-08-19, P1 (round 32 fix): a same-UID racer who overwrites the
        # generated launch guard (bin/prime-agent-launch-guard.py) in the
        # window between atomic_create_private_file() publishing it and
        # write_pending_install()'s tree_digest() call later reaching that
        # same path -- see _round32_tamper_generated_exec_target_harness()'s
        # own docstring for the full finding and reproduction methodology.
        #
        # Verified to FAIL against pre-fix HEAD (commit 035ef5dad5): the
        # launch guard was not in release_relative_pinned_digests, so this
        # exact tamper was silently baked into release_tree_sha256 and
        # installer.install() completed successfully instead of raising.
        self._round32_tamper_generated_exec_target_harness(
            target_relative="bin/prime-agent-launch-guard.py",
            error_regex=(
                r"release tree content does not match its digest-verified "
                r"pre-npm-ci pinned baseline: "
                r"bin/prime-agent-launch-guard\.py$"
            ),
        )

    def test_install_locked_detects_command_wrapper_tamper_before_baseline_lock(
        self,
    ) -> None:
        # Regression for independent Claude opus/max round-31 review,
        # 2026-08-19, P1 (round 32 fix): the same finding as the launch
        # guard test just above, applied to the generated command wrapper
        # (bin/prime-agent) instead -- see
        # _round32_tamper_generated_exec_target_harness()'s own docstring
        # for the full finding and reproduction methodology. Isolated from
        # the launch guard specifically: only the wrapper is tampered here
        # (the guard stays genuine), and the expected error's `$` anchor
        # confirms tree_digest() reports the wrapper's own shorter relative
        # path ("bin/prime-agent"), not the guard's longer one that shares
        # its exact lexical prefix ("bin/prime-agent-launch-guard.py") and
        # sorts immediately after it in tree_digest()'s own walk order.
        #
        # Verified to FAIL against pre-fix HEAD (commit 035ef5dad5): the
        # command wrapper was not in release_relative_pinned_digests, so
        # this exact tamper was silently baked into release_tree_sha256 and
        # installer.install() completed successfully instead of raising.
        self._round32_tamper_generated_exec_target_harness(
            target_relative="bin/prime-agent",
            error_regex=(
                r"release tree content does not match its digest-verified "
                r"pre-npm-ci pinned baseline: "
                r"bin/prime-agent$"
            ),
        )

    def test_install_locked_detects_npm_cli_content_swap_during_npm_ci_window(
        self,
    ) -> None:
        # Regression for independent Claude opus/max round-29 review,
        # 2026-08-19, P1-2: "toolchain npm-cli.js is re-verified only
        # BEFORE npm ci runs, never after (the existing code comment even
        # says 'the pinned toolchain BINARIES', plural, contradicting what
        # is actually implemented). receipt['npm_cli_sha256'] is written
        # but never read or compared anywhere -- dead evidence. Reproduced
        # end-to-end: tampering npm-cli.js during run_npm()'s ci
        # subprocess branch (measured real window ~77 seconds, the full
        # npm ci runtime) survives to a completed install; verify() then
        # executes the attacker's npm-cli.js when it runs later."
        #
        # This test's fake_run_npm's "ci" branch materializes a genuine
        # prime-agent tree (so nothing unrelated fails first) AND
        # overwrites npm-cli.js's on-disk bytes in place as a side effect
        # of that same call -- simulating a same-UID racer who tampered it
        # sometime during `npm ci`'s own subprocess window, exactly as the
        # finding describes. npm-cli.js was extracted genuine (via
        # self.fake_extract_node_toolchain) and re-verified genuine
        # immediately BEFORE this "ci" call (both run_npm() invocations
        # are preceded by a re-verify, unchanged by this round's fix); the
        # only question this test isolates is whether anything re-checks
        # it AFTER this call returns.
        #
        # Verified to FAIL against pre-fix HEAD (commit ea8004f820) in an
        # isolated scratch copy: pre-fix, `node` was re-verified via
        # verify_unchanged_private_ssd_asset_digest() immediately after
        # `npm ci` returned, but npm-cli.js was not -- this exact tamper
        # would have gone completely undetected all the way to a completed
        # install with a clean receipt.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            GENUINE_CLI_CONTENT = b"// genuine, tarball-pinned cli.js\n"

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_bytes(GENUINE_CLI_CONTENT)
                    os.chmod(cli, 0o600)
                    # The same-UID racer's swap, won sometime during this
                    # exact `npm ci` subprocess's own runtime -- npm_cli
                    # was already extracted, genuine, and re-verified
                    # BEFORE this call started; this simulates its content
                    # changing while this call is "running".
                    npm_cli_path = (
                        installer.RELEASE_DIR
                        / "toolchain/lib/node_modules/npm/bin/npm-cli.js"
                    )
                    with open(npm_cli_path, "r+b") as handle:
                        handle.seek(0)
                        handle.write(
                            b"// ATTACKER npm-cli.js, planted during npm ci\n"
                        )
                        handle.truncate()

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {Path("dist/bundle/cli.js"): installer.sha256_bytes(GENUINE_CLI_CONTENT)}
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    r"managed asset changed before use: .*npm-cli\.js",
                ):
                    installer.install()

            self.assertFalse((release / "lib/node_modules").exists())
            self.assertFalse((release / "bin/prime-agent-launch-guard.py").exists())
            self.assertFalse((release / "bin/prime-agent").exists())
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_install_locked_detects_undeclared_package_in_subdirectory_anchored_nested_node_modules(
        self,
    ) -> None:
        # Regression for independent Claude opus/max round-29 review,
        # 2026-08-19, P2-1: "_walk_nested_node_modules_containers() only
        # descends into <package>/node_modules (a package's own
        # root-level node_modules), missing subdirectory-anchored
        # containers like <package>/<subdir>/node_modules. Verified with
        # real Node that Node's CJS module resolution hits a
        # subdirectory-anchored node_modules FIRST, before falling back to
        # a hoisted copy higher up -- so this is a real resolution-order
        # gap, not just a completeness nitpick. The real materialized tree
        # currently has zero such containers, so extending the walk to
        # catch them would produce zero false positives."
        #
        # This test plants the undeclared package inside a REGISTRY
        # (non-locally-patched) declared package's own SUBDIRECTORY-
        # anchored node_modules/ ("some-registry-dep/subdir/node_modules")
        # -- a shape no real package-lock.json can ever declare, and one
        # directory deeper than even round 27's root-anchored nested check
        # ever looked -- deliberately NOT inside one of the four locally
        # patched packages, so this isolates P2-1 cleanly from this
        # round's own P1-1 fix.
        #
        # Verified to FAIL against pre-fix HEAD (commit ea8004f820) in an
        # isolated scratch copy: pre-fix, _walk_nested_node_modules_
        # containers() only ever checked `package_dir / "node_modules"` (a
        # direct child of the package's own root) -- a node_modules/
        # anchored one level deeper, under an arbitrary subdirectory, was
        # never discovered or walked at all, so this exact plant would
        # have gone completely uncaught and install() would have completed
        # successfully.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            registry_dep_row = {
                "name": "some-registry-dep",
                "version": "1.0.0",
                "resolved": (
                    "https://registry.npmjs.org/some-registry-dep/-/"
                    "some-registry-dep-1.0.0.tgz"
                ),
                "integrity": "sha512-dGVzdA==",
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                    "node_modules/some-registry-dep": registry_dep_row,
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            GENUINE_CLI_CONTENT = b"// genuine, tarball-pinned cli.js\n"

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_bytes(GENUINE_CLI_CONTENT)
                    os.chmod(cli, 0o600)
                    # The declared registry dependency itself -- a plain,
                    # legitimate package with no nested node_modules/ of
                    # its own at all.
                    some_registry_dep = cwd / "node_modules/some-registry-dep"
                    some_registry_dep.mkdir(parents=True, mode=0o700)
                    os.chmod(some_registry_dep, 0o700)
                    # The same-UID racer's plant: an ENTIRELY UNDECLARED
                    # package sitting under a SUBDIRECTORY of a
                    # legitimately declared package -- not at that
                    # package's own root -- a shape no real
                    # package-lock.json can ever declare, and one level
                    # deeper than even the round-27 nested check ever
                    # looked.
                    evil_nested = (
                        some_registry_dep / "subdir/node_modules/evil-nested-package"
                    )
                    evil_nested.mkdir(parents=True, mode=0o700)
                    os.chmod(some_registry_dep / "subdir", 0o700)
                    os.chmod(some_registry_dep / "subdir/node_modules", 0o700)
                    os.chmod(evil_nested, 0o700)
                    (evil_nested / "index.js").write_text(
                        "// attacker-controlled subdirectory-anchored "
                        "nested module\n",
                        encoding="utf-8",
                    )
                    os.chmod(evil_nested / "index.js", 0o600)

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {Path("dist/bundle/cli.js"): installer.sha256_bytes(GENUINE_CLI_CONTENT)}
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "GENERATED_LOCK_PACKAGE_COUNT",
                        len(local_assets) + 1,
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    r"npm materialized undeclared nested node_modules package\(s\) "
                    r"not present in the verified lock inside "
                    r"node_modules/some-registry-dep/subdir/node_modules: "
                    r"\['evil-nested-package'\]",
                ):
                    installer.install()

            self.assertFalse((release / "lib/node_modules").exists())
            self.assertFalse((release / "bin/prime-agent-launch-guard.py").exists())
            self.assertFalse((release / "bin/prime-agent").exists())
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_assert_locally_patched_package_matches_pinned_digests_rejects_symlink_swap_during_digest_read(
        self,
    ) -> None:
        # Regression for independent Claude opus/max round-29 review,
        # 2026-08-19, P2-2: "The digest sweep's sha256_file() call uses two
        # independent, symlink-following filesystem resolutions (scandir's
        # cached stat, then a plain path.open('rb') for the digest read)
        # instead of this file's own established discipline elsewhere
        # (read_private_file()'s single-open O_NOFOLLOW + fstat identity
        # confirmation, used to close exactly this class of bug in earlier
        # rounds). Demonstrated: swapping a pinned regular file for a
        # symlink between the two resolutions is accepted, and the
        # symlink's target content can then be changed afterward without
        # being caught."
        #
        # This test calls
        # assert_locally_patched_package_matches_pinned_digests() directly
        # against a real temp directory, and hooks
        # installer.sha256_file_verified (the digest-computation call this
        # function makes AFTER its own os.scandir()-based entry-type
        # checks already ran and passed) to perform the swap: the pinned
        # regular file is replaced with a symlink to a DIFFERENT file
        # whose content ALSO happens to match the pinned digest at that
        # exact moment -- the harder case a simple content-mismatch check
        # would already catch -- right before the real digest read runs,
        # landing exactly in the window the round-29 report describes.
        #
        # Verified to FAIL against pre-fix HEAD (commit ea8004f820) in an
        # isolated scratch copy: pre-fix, hooking installer.sha256_file
        # (the plain, path-based digest helper that call site used before
        # this round) the same way lets the swapped-to symlink's target
        # content be read and compared successfully via a plain
        # `path.open("rb")` that silently follows it, so the sweep would
        # have returned normally (no error) despite the pinned path no
        # longer being the regular file it was at scandir time.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            package_dir = root / "package"
            package_dir.mkdir(mode=0o700)
            genuine_content = b"// genuine, pinned content\n"
            # Deliberately OUTSIDE package_dir: this function's own walk
            # scans every entry inside package_dir and rejects anything
            # not present in `pinned_digests` as an "undeclared file" --
            # the symlink's target must not itself be materialized as a
            # sibling package member, or that unrelated check would fire
            # first and this test would not isolate the symlink-swap gap
            # at all.
            outside_dir = root / "outside"
            outside_dir.mkdir(mode=0o700)
            target_path = outside_dir / "victim-target.js"
            target_path.write_bytes(genuine_content)
            os.chmod(target_path, 0o644)
            pinned_path = package_dir / "pinned-file.js"
            pinned_path.write_bytes(genuine_content)
            os.chmod(pinned_path, 0o644)
            pinned_digests = {
                Path("pinned-file.js"): installer.sha256_bytes(genuine_content),
            }

            real_digest = installer.sha256_file_verified
            swap_fired = {"done": False}

            def swap_then_digest(path, **kwargs):
                if (
                    Path(path).name == "pinned-file.js"
                    and not swap_fired["done"]
                ):
                    # The same-UID racer's swap, won in the exact window
                    # between this function's own os.scandir()-based
                    # entry-type checks (already passed by the time this
                    # is called) and the digest read itself: the pinned
                    # regular file is replaced with a symlink to a
                    # DIFFERENT file whose content ALSO currently matches
                    # the pinned digest.
                    Path(path).unlink()
                    Path(path).symlink_to(target_path)
                    swap_fired["done"] = True
                return real_digest(path, **kwargs)

            with mock.patch.object(
                installer, "sha256_file_verified", side_effect=swap_then_digest
            ):
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, r"unsafe file for digest"
                ):
                    installer.assert_locally_patched_package_matches_pinned_digests(
                        package_dir, pinned_digests, "test-package"
                    )
            self.assertTrue(swap_fired["done"])
            # The swapped-to symlink's TARGET content still genuinely
            # matches the pinned digest -- proving this was rejected
            # because it is a symlink (an identity/type violation), not
            # because of a content mismatch a simpler check would already
            # have caught.
            self.assertEqual(
                installer.sha256_bytes(target_path.read_bytes()),
                pinned_digests[Path("pinned-file.js")],
            )

    def _round34_p1_1_completeness_harness(
        self, *, tamper, error_regex: str
    ) -> None:
        """Shared harness for the three round-34 P1-1 regression tests just
        below.

        Regression for independent Claude opus/max round-33 review,
        2026-08-19, P1-1: "tree_digest()'s pinned-digest comparison only
        fires inside the `elif stat.S_ISREG(...)` branch. The `if
        stat.S_ISLNK(...)` branch and the `elif stat.S_ISDIR(...)` branch
        never consult pinned_relative_digests, and the function never
        verifies that every key in pinned_relative_digests was actually
        observed as a regular file during the walk. So a pinned path that
        gets replaced with a symlink, replaced with a directory, or simply
        deleted silently skips the comparison entirely -- whatever's there
        (or isn't) gets folded into the permanent baseline with zero
        check."

        Calls the real tree_digest() twice against a real temp directory:
        once while the pinned path is still the genuine regular file (a
        sanity check that the pinned map is otherwise accepted), then again
        after `tamper` has replaced/removed it, with the exact same
        `pinned_relative_digests` map both times -- isolating the
        completeness assertion as the ONLY thing that can still catch it
        (there is no separate sweep function for a bare tree_digest()
        caller the way the four locally patched packages have
        assert_locally_patched_package_matches_pinned_digests()).

        A content-identical decoy file, elsewhere in the same directory, is
        available to `tamper` for the symlink scenario specifically -- so a
        symlink substitution whose target happens to match the pinned
        digest byte-for-byte is still caught, proving this is a TYPE
        (regular-file) check, not merely a weaker content check that would
        have been fooled by matching bytes.

        Verified against pre-fix HEAD (commit a3430db740) via a standalone
        scratch script exercising the same three scenarios directly against
        that commit's tree_digest(): all three completed with `NO_RAISE`
        (tree_digest() returned a digest/count pair successfully, silently
        folding the tampered/omitted path into the baseline with no
        comparison at all) pre-fix, and all three raise the completeness
        error below post-fix.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            genuine_content = b"genuine, pinned content\n"
            pinned_path = root / "pinned-file.js"
            pinned_path.write_bytes(genuine_content)
            os.chmod(pinned_path, 0o600)
            pinned_digest = installer.sha256_bytes(genuine_content)
            # Same content as the pinned file, but a DIFFERENT path -- only
            # used by the symlink scenario, to prove the catch is type-based
            # rather than incidentally a content mismatch.
            decoy_path = root / "decoy-same-content.js"
            decoy_path.write_bytes(genuine_content)
            os.chmod(decoy_path, 0o600)
            # Round 38: also pinned (to the same, genuinely-matching digest,
            # since `tamper` never touches `decoy_path` itself in any of the
            # three scenarios below) so tree_digest()'s new deny-unknown
            # check has nothing to say about it -- this harness isolates the
            # round-34 completeness assertion specifically, not the
            # unrelated round-38 deny-unknown check.
            pinned_relative_digests = {
                "pinned-file.js": pinned_digest,
                "decoy-same-content.js": pinned_digest,
            }
            with mock.patch.object(installer, "SSD_ROOT", root):
                # Sanity: while the pinned path is still the genuine regular
                # file, tree_digest() accepts the pinned map normally.
                installer.tree_digest(
                    root, pinned_relative_digests=pinned_relative_digests
                )
                tamper(pinned_path, decoy_path)
                with self.assertRaisesRegex(
                    installer.PrimeInstallError, error_regex
                ):
                    installer.tree_digest(
                        root, pinned_relative_digests=pinned_relative_digests
                    )

    def test_tree_digest_completeness_catches_pinned_path_replaced_with_symlink(
        self,
    ) -> None:
        # See _round34_p1_1_completeness_harness()'s own docstring for the
        # full finding. This scenario: the pinned regular file is deleted
        # and replaced, in its exact place, with a RELATIVE symlink to a
        # content-identical decoy elsewhere in the same tree -- so if this
        # were caught only by a content mismatch (as the pre-round-34 code
        # would have computed one, had it even tried), it would NOT be
        # caught; the only thing that can catch it is the completeness
        # assertion refusing a pinned path that is no longer a regular file
        # at all. tree_digest()'s own separate escaped-symlink check
        # (`runtime symlink escaped release`) does not fire either, since
        # the decoy is a relative symlink target that stays inside the same
        # release tree -- isolating this test to the completeness
        # assertion specifically, not the unrelated escape check.
        #
        # Verified to FAIL against pre-fix HEAD (commit a3430db740) via a
        # standalone scratch script: tree_digest() returned a digest/count
        # pair successfully, silently folding the symlink in as if
        # `pinned_relative_digests` had never named this path at all.
        def tamper(pinned_path: Path, decoy_path: Path) -> None:
            pinned_path.unlink()
            pinned_path.symlink_to(Path(decoy_path.name))

        self._round34_p1_1_completeness_harness(
            tamper=tamper,
            error_regex=(
                r"release tree is missing pinned path\(s\), or a pinned "
                r"path is no longer a plain regular file.*pinned-file\.js"
            ),
        )

    def test_tree_digest_completeness_catches_pinned_path_replaced_with_directory(
        self,
    ) -> None:
        # See _round34_p1_1_completeness_harness()'s own docstring for the
        # full finding. This scenario: the pinned regular file is deleted
        # and a DIRECTORY (containing an attacker-controlled file) is
        # created in its exact place -- the `elif stat.S_ISDIR(...)` branch
        # never consulted `pinned_relative_digests` before this round's fix,
        # so this substitution was folded into the baseline with zero check
        # either.
        #
        # Verified to FAIL against pre-fix HEAD (commit a3430db740) via a
        # standalone scratch script: tree_digest() returned a digest/count
        # pair successfully, silently recording the substituted directory
        # (and its attacker-controlled member) as part of the baseline.
        def tamper(pinned_path: Path, decoy_path: Path) -> None:
            del decoy_path
            pinned_path.unlink()
            pinned_path.mkdir(mode=0o700)
            (pinned_path / "attacker-payload.js").write_bytes(b"attacker payload\n")
            os.chmod(pinned_path / "attacker-payload.js", 0o600)

        self._round34_p1_1_completeness_harness(
            tamper=tamper,
            error_regex=(
                r"release tree is missing pinned path\(s\), or a pinned "
                r"path is no longer a plain regular file.*pinned-file\.js"
            ),
        )

    def test_tree_digest_completeness_catches_pinned_path_deleted_entirely(
        self,
    ) -> None:
        # See _round34_p1_1_completeness_harness()'s own docstring for the
        # full finding. This scenario: the pinned regular file is simply
        # DELETED, with nothing put in its place -- the walk never observes
        # this relative path at all, so pre-round-34 there was no check of
        # any kind (not even a wrong-type one) that could have noticed it
        # was gone; tree_digest() unconditionally recorded a baseline that
        # simply omits it.
        #
        # Verified to FAIL against pre-fix HEAD (commit a3430db740) via a
        # standalone scratch script: tree_digest() returned a digest/count
        # pair successfully, with the deleted path silently absent from the
        # recorded baseline instead of being refused.
        def tamper(pinned_path: Path, decoy_path: Path) -> None:
            del decoy_path
            pinned_path.unlink()

        self._round34_p1_1_completeness_harness(
            tamper=tamper,
            error_regex=(
                r"release tree is missing pinned path\(s\), or a pinned "
                r"path is no longer a plain regular file.*pinned-file\.js"
            ),
        )

    def test_install_locked_detects_unpinned_toolchain_npm_lib_cli_tamper(
        self,
    ) -> None:
        # Regression for independent Claude opus/max round-33 review,
        # 2026-08-19, P1-2: "release_relative_pinned_digests only pins
        # toolchain/bin/node and toolchain/lib/node_modules/npm/bin/
        # npm-cli.js -- but the real npm-cli.js is a 2-line forwarding stub
        # (`require('../lib/cli.js')`), and the actual npm library code
        # that runs is in toolchain/lib/node_modules/npm/lib/cli.js and its
        # dependencies, which are NOT pinned. verify() itself executes this
        # via exact_tool_version(), which real npm CLI code confirmed to
        # actually run. ... tampering the unpinned lib/cli.js while leaving
        # the pinned npm-cli.js stub untouched still results in the
        # tampered code executing inside verify(), with node npm-cli.js
        # --version still returning the correct version string (since
        # npm-cli.js itself, the only pinned file, is unchanged)."
        #
        # This wraps `self.fake_extract_node_toolchain` (which already
        # writes a THIRD toolchain file, lib/node_modules/npm/lib/cli.js,
        # modeling real npm's stub/library split -- see that fake's own
        # docstring) with a side effect that, immediately after the fake
        # returns genuine content and a genuine, tarball-derived digest map
        # for all three files, overwrites lib/cli.js's on-disk bytes IN
        # PLACE -- leaving npm-cli.js (the pinned stub) and its own digest
        # completely untouched -- exactly modeling a same-UID racer who
        # wins the window between extraction and write_pending_install()'s
        # later tree_digest() call, without any of this installer's own
        # node/npm-cli-specific re-verification calls (all of which only
        # ever touch the two entry-point files) having any way to observe
        # it.
        #
        # Verified to FAIL against pre-fix HEAD (commit a3430db740) via a
        # standalone scratch script using an arity-matched (4-tuple)
        # extract_node_toolchain fake, exercising the same real install()
        # path: installer.install() completed successfully (no exception),
        # with the tampered lib/cli.js content and a clean receipt both
        # left on disk -- because pre-fix, `release_relative_pinned_
        # digests` never contained a "toolchain/lib/node_modules/npm/lib/
        # cli.js" key at all, so tree_digest() had nothing to compare that
        # path against and simply recorded the tampered bytes as part of
        # the trusted baseline.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            GENUINE_CLI_CONTENT = b"// genuine, tarball-pinned cli.js\n"
            ATTACKER_NPM_LIB_CLI_CONTENT = (
                b"// ATTACKER npm lib/cli.js, planted AFTER extract_node_"
                b"toolchain() returned genuine content -- the pinned "
                b"npm-cli.js stub itself is left completely untouched.\n"
            )

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_bytes(GENUINE_CLI_CONTENT)
                    os.chmod(cli, 0o600)

            def fake_make_patched_asset(
                original_asset,
                original_sha256,
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {Path("dist/bundle/cli.js"): installer.sha256_bytes(GENUINE_CLI_CONTENT)}
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            real_fake_extract_node_toolchain = self.fake_extract_node_toolchain
            tamper_fired = {"done": False}

            def extract_then_tamper_npm_lib_cli(asset, destination, expected_sha256):
                result = real_fake_extract_node_toolchain(
                    asset, destination, expected_sha256
                )
                if not tamper_fired["done"]:
                    npm_lib_cli_path = destination / "lib/node_modules/npm/lib/cli.js"
                    npm_cli_path = destination / "lib/node_modules/npm/bin/npm-cli.js"
                    before_npm_cli_bytes = npm_cli_path.read_bytes()
                    with open(npm_lib_cli_path, "r+b") as handle:
                        handle.seek(0)
                        handle.write(ATTACKER_NPM_LIB_CLI_CONTENT)
                        handle.truncate()
                    # The pinned stub itself must be completely untouched by
                    # this tamper -- otherwise this test would not isolate
                    # the P1-2 finding (an UNPINNED sibling file executing)
                    # from the already-fixed round-29 finding (the PINNED
                    # npm-cli.js itself being swapped).
                    assert npm_cli_path.read_bytes() == before_npm_cli_bytes
                    tamper_fired["done"] = True
                # The returned digest map is the ORIGINAL, genuine,
                # tarball-derived one from the fake -- exactly modeling a
                # same-UID racer whose on-disk swap this installer's own
                # extraction-time bookkeeping never observes.
                return result

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=extract_then_tamper_npm_lib_cli,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    r"release tree content does not match its digest-verified "
                    r"pre-npm-ci pinned baseline: "
                    r"toolchain/lib/node_modules/npm/lib/cli\.js",
                ):
                    installer.install()

            self.assertTrue(tamper_fired["done"])
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    # ------------------------------------------------------------------
    # Round 36, 2026-08-20 (independent Claude opus/max round-35 review):
    # P1-1 (npm-cache-derived registry package content pinning), P1-2a
    # (launch guard exec isolation), and P1-2b (post-move re-check of
    # assert_materialized_node_modules_matches_lock()).
    # ------------------------------------------------------------------

    def _build_fake_registry_tarball(
        self, files: dict[str, bytes], *, top: str = "package"
    ) -> tuple[bytes, str]:
        """Build a real, valid gzip tar in the shape a real npm registry
        tarball has -- a single top-level directory, `top` (defaulting to
        "package", the convention plain `npm publish`/`npm pack` use, and
        the same one safe_extract_main_asset() hardcodes for the four
        locally patched packages' own tarballs) -- and return (raw_bytes,
        integrity) where `integrity` is the real "sha512-<base64>" SRI
        string for those exact bytes -- the same format package-lock.json
        rows declare, and the same format verified_registry_package_tarball()
        decodes to compute npm's own cache content-address path.
        """
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, content in files.items():
                info = tarfile.TarInfo(name=f"{top}/{name}")
                info.size = len(content)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(content))
        raw = buffer.getvalue()
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode(
            "ascii"
        )
        return raw, integrity

    def test_declared_registry_package_lock_rows_excludes_local_assets_and_root(
        self,
    ) -> None:
        packages = {
            "": {"name": "orca-managed-prime-agent", "version": installer.VERSION},
            "node_modules/local-0": {
                "name": "prime-agent",
                "version": installer.VERSION,
                "resolved": f"file:assets/{installer.MAIN_PATCHED_ASSET}",
            },
            "node_modules/local-1": {
                "name": "@earendil-works/pi-ai",
                "version": installer.VERSION,
                "resolved": "file:assets/prime-agent-ai-0.7.2-orca-pinned.tgz",
            },
            "node_modules/left-pad-fake": {
                "name": "left-pad-fake",
                "version": "1.0.0",
                "resolved": (
                    "https://registry.npmjs.org/left-pad-fake/-/"
                    "left-pad-fake-1.0.0.tgz"
                ),
                "integrity": "sha512-AAAA",
            },
            "node_modules/proxy-agent/node_modules/socks": {
                "name": "socks",
                "version": "2.0.0",
                "resolved": "https://registry.npmjs.org/socks/-/socks-2.0.0.tgz",
                "integrity": "sha512-BBBB",
            },
        }
        rows = installer.declared_registry_package_lock_rows(packages)
        self.assertEqual(
            set(rows),
            {
                "node_modules/left-pad-fake",
                "node_modules/proxy-agent/node_modules/socks",
            },
        )
        self.assertEqual(rows["node_modules/left-pad-fake"]["integrity"], "sha512-AAAA")

    def test_verified_registry_package_tarball_reads_cache_by_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory).resolve()
            raw, integrity = self._build_fake_registry_tarball(
                {"index.js": b"module.exports = 1;\n"}
            )
            hex_digest = base64.b64decode(integrity[len("sha512-"):]).hex()
            content_path = (
                cache
                / "_cacache/content-v2/sha512"
                / hex_digest[0:2]
                / hex_digest[2:4]
                / hex_digest[4:]
            )
            content_path.parent.mkdir(parents=True)
            content_path.write_bytes(raw)
            os.chmod(content_path, 0o644)
            observed = installer.verified_registry_package_tarball(
                cache, integrity, "left-pad-fake"
            )
            self.assertEqual(observed, raw)

    def test_verified_registry_package_tarball_rejects_content_mismatching_its_own_integrity(
        self,
    ) -> None:
        # A same-UID actor with write access to this installer's own
        # private npm cache directory plants DIFFERENT bytes at the exact
        # content-addressed path a genuine tarball with this integrity
        # would occupy -- the path convention alone must not be trusted;
        # only the fresh SHA-512 re-check over the bytes actually read
        # makes this safe (see verified_registry_package_tarball()'s own
        # docstring).
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory).resolve()
            raw, integrity = self._build_fake_registry_tarball(
                {"index.js": b"module.exports = 1;\n"}
            )
            hex_digest = base64.b64decode(integrity[len("sha512-"):]).hex()
            content_path = (
                cache
                / "_cacache/content-v2/sha512"
                / hex_digest[0:2]
                / hex_digest[2:4]
                / hex_digest[4:]
            )
            content_path.parent.mkdir(parents=True)
            content_path.write_bytes(raw + b"tampered-bytes")
            os.chmod(content_path, 0o644)
            with self.assertRaisesRegex(
                installer.PrimeInstallError,
                r"cached tarball content does not match its own pinned SRI integrity",
            ):
                installer.verified_registry_package_tarball(
                    cache, integrity, "left-pad-fake"
                )

    def test_verified_registry_package_tarball_missing_from_cache_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory).resolve()
            integrity = "sha512-" + base64.b64encode(
                hashlib.sha512(b"never downloaded").digest()
            ).decode("ascii")
            with self.assertRaisesRegex(
                installer.PrimeInstallError,
                r"tarball is not present in the managed npm cache",
            ):
                installer.verified_registry_package_tarball(
                    cache, integrity, "left-pad-fake"
                )

    def test_verified_registry_package_tarball_rejects_malformed_integrity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory).resolve()
            with self.assertRaisesRegex(
                installer.PrimeInstallError, r"unsafe pinned integrity value"
            ):
                installer.verified_registry_package_tarball(
                    cache, "not-even-sha512-shaped", "left-pad-fake"
                )

    def test_registry_package_content_digests_matches_real_extraction(self) -> None:
        files = {
            "index.js": b"module.exports = 'left-pad-fake';\n",
            "package.json": b'{"name":"left-pad-fake","version":"1.0.0"}',
        }
        raw, _ = self._build_fake_registry_tarball(files)
        digests = installer.registry_package_content_digests(raw, "left-pad-fake")
        expected = {
            Path(name): installer.sha256_bytes(content)
            for name, content in files.items()
        }
        self.assertEqual(digests, expected)

    def test_registry_package_content_digests_rejects_symlink_member(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo(name="package/evil-link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        raw = buffer.getvalue()
        with self.assertRaisesRegex(
            installer.PrimeInstallError, r"unsafe cached tar member"
        ):
            installer.registry_package_content_digests(raw, "evil-package")

    def test_registry_package_content_digests_accepts_non_package_top_level_directory(
        self,
    ) -> None:
        # Regression for a REAL false positive this round's own real,
        # non-mocked tests/sandbox_e2e.py replay found (not a mocked unit
        # test): @types/mime-types's real published tarball (fetched via
        # `npm pack @types/mime-types` and inspected directly with
        # `tar tvzf`, round 36, 2026-08-20) uses "mime-types/" as its
        # top-level directory -- the DefinitelyTyped `types-publisher`
        # tooling's own convention (the bare, unscoped package name), not
        # plain `npm publish`'s "package/" default. The pre-fix, hardcoded
        # `pure.parts[0] != "package"` check rejected every file in this
        # real package outright; the real install failed with "unsafe
        # cached tar member for node_modules/@types/mime-types: mime-types"
        # on its second real run (after the round-36 nested-node_modules
        # fix, before this one).
        files = {
            "index.d.ts": b"declare const x: string;\nexport = x;\n",
            "package.json": b'{"name":"@types/mime-types","version":"3.0.1"}',
        }
        raw, _ = self._build_fake_registry_tarball(files, top="mime-types")
        digests = installer.registry_package_content_digests(
            raw, "node_modules/@types/mime-types"
        )
        expected = {
            Path(name): installer.sha256_bytes(content)
            for name, content in files.items()
        }
        self.assertEqual(digests, expected)

    def test_registry_package_content_digests_rejects_inconsistent_top_level_directories(
        self,
    ) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name in ("package/index.js", "sneaky-other-dir/evil.js"):
                content = b"x"
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        raw = buffer.getvalue()
        with self.assertRaisesRegex(
            installer.PrimeInstallError, r"unsafe cached tar member"
        ):
            installer.registry_package_content_digests(raw, "evil-package")

    def test_registry_package_content_digests_tolerates_byte_identical_duplicate_destination(
        self,
    ) -> None:
        # Regression for a REAL false positive this round's own real,
        # non-mocked tests/sandbox_e2e.py replay found (not a mocked unit
        # test): agent-base@7.1.0 through 7.1.4's real published tarballs
        # (fetched via `npm pack agent-base@7.1.x` and inspected directly
        # with Python's own tarfile module, round 36, 2026-08-20) each
        # contain BOTH "package/./dist/index.js" and "package/dist/index.js"
        # -- two raw tar member names that PurePosixPath (and real npm's
        # own extraction target resolution) normalize to the identical
        # destination Path("dist/index.js") -- with byte-identical content.
        # The real install failed with "duplicate cached tar member for
        # node_modules/agent-base: package/dist/index.js" on its third real
        # run (after the round-36 nested-node_modules and non-"package"
        # top-level-directory fixes, before this one).
        content = b"module.exports = function Agent() {};\n"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name in ("package/./dist/index.js", "package/dist/index.js"):
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        raw = buffer.getvalue()
        digests = installer.registry_package_content_digests(raw, "agent-base")
        self.assertEqual(
            digests, {Path("dist/index.js"): installer.sha256_bytes(content)}
        )

    def test_registry_package_content_digests_rejects_genuinely_conflicting_duplicate_destination(
        self,
    ) -> None:
        # Companion to the byte-identical case just above: two raw tar
        # member names resolving to the SAME destination but with
        # DIFFERENT content is a genuine ambiguity (not the benign
        # redundant-"." artifact above) and must still fail closed.
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, content in (
                ("package/./dist/index.js", b"genuine content\n"),
                ("package/dist/index.js", b"DIFFERENT content\n"),
            ):
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        raw = buffer.getvalue()
        with self.assertRaisesRegex(
            installer.PrimeInstallError, r"conflicting cached tar members"
        ):
            installer.registry_package_content_digests(raw, "evil-package")

    def test_assert_package_matches_pinned_digests_tolerates_legitimate_nested_node_modules(
        self,
    ) -> None:
        # Regression for a REAL false positive this round's own real,
        # non-mocked tests/sandbox_e2e.py replay found (not a mocked unit
        # test): a real darwin-arm64 `npm ci` of this exact pinned closure
        # legitimately nests a private copy of a hoisting-conflicted
        # transitive dependency under a package's own node_modules/ --
        # concretely, node_modules/@aws-sdk/credential-provider-sso/
        # node_modules/@aws-sdk/token-providers/ -- and this function's
        # pre-fix, fully-recursive walk flagged every one of that nested
        # package's own files as "materialized an undeclared file not
        # present in the pinned pre-npm-ci digest map", because
        # credential-provider-sso's own `pinned_digests` (correctly) never
        # claimed to cover a DIFFERENT package's content. The real install
        # failed outright with this error on its first real run.
        #
        # Fixed by skipping descent into any subdirectory literally named
        # "node_modules" -- such a directory always belongs to a
        # separately-declared package with its OWN separate call to this
        # same function (see declared_registry_package_lock_rows()), never
        # to `package_dir`'s own pinned map; an UNDECLARED nested container
        # is still caught by assert_materialized_node_modules_matches_lock()
        # regardless (unit-tested separately, unaffected by this fix).
        with tempfile.TemporaryDirectory() as directory:
            package_dir = Path(directory).resolve() / "credential-provider-sso"
            package_dir.mkdir(mode=0o700)
            index_js = package_dir / "index.js"
            index_js.write_bytes(b"module.exports = {};\n")
            os.chmod(index_js, 0o600)
            # The legitimate, hoisting-conflict-resolving nested dependency
            # -- NOT declared anywhere in `pinned_digests` below, exactly
            # as real npm materializes it, and exactly as it would appear
            # in the real closure this test is a targeted stand-in for.
            nested_pkg = package_dir / "node_modules/token-providers"
            nested_pkg.mkdir(parents=True, mode=0o700)
            nested_index = nested_pkg / "index.js"
            nested_index.write_bytes(b"module.exports = 'nested';\n")
            os.chmod(nested_index, 0o600)
            os.chmod(package_dir / "node_modules", 0o700)

            pinned_digests = {
                Path("index.js"): installer.sha256_bytes(b"module.exports = {};\n"),
            }
            # Must not raise: the nested node_modules/ subtree is out of
            # scope for this call entirely.
            installer.assert_locally_patched_package_matches_pinned_digests(
                package_dir, pinned_digests, "node_modules/@aws-sdk/credential-provider-sso"
            )

    def _round36_registry_package_harness(self, *, tamper: bool) -> dict | None:
        """Shared harness for the two round-36 P1-1 regression tests below.

        Regression for independent Claude opus/max round-35 review,
        2026-08-20, P1-1: "Only 4 of 182 installed third-party registry
        packages are pinned ...; the other 176 packages ... are completely
        unprotected. Reproduced: tampering node_modules/undici/index.js ...
        during npm ci's window survives to become part of the
        permanently-trusted baseline; verify() reports ok:true."

        This harness adds ONE registry dependency row ("left-pad-fake") to
        the same minimal generated-lock fixture the other full-install()
        regression tests in this file already use, and its fake_run_npm's
        "ci" branch does two things a real `npm ci` does for a real
        registry dependency: (1) populates the (fake, temp-dir) npm cache
        with the EXACT SRI-addressed tarball bytes `left-pad-fake`'s
        integrity was computed from -- this is the only thing this
        fixture models on npm's behalf, everything downstream
        (verified_registry_package_tarball()'s own re-verification,
        registry_package_content_digests()'s own extraction, and the real
        assert_locally_patched_package_matches_pinned_digests() sweep) is
        the real, unmocked installer code under test; and (2) materializes
        left-pad-fake's own directory on disk, with `index.js` either
        matching that cached tarball exactly (tamper=False) or replaced
        with different, attacker-controlled bytes (tamper=True) --
        modeling a same-UID racer who tampers the EXTRACTED file in place
        sometime during this exact `npm ci` subprocess's own runtime,
        leaving npm's own cache untouched.

        Verified to FAIL to catch the tamper=True case against pre-fix
        HEAD (commit dab6a143d9) in an isolated scratch copy: pre-fix,
        nothing compared any registry dependency's file content against
        anything at all, so installer.install() completed successfully
        with a clean receipt instead of raising.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            registry_files = {"index.js": b"module.exports = 'left-pad-fake';\n"}
            registry_raw, registry_integrity = self._build_fake_registry_tarball(
                registry_files
            )
            registry_lock_path = "node_modules/left-pad-fake"

            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                    registry_lock_path: {
                        "name": "left-pad-fake",
                        "version": "1.0.0",
                        "resolved": (
                            "https://registry.npmjs.org/left-pad-fake/-/"
                            "left-pad-fake-1.0.0.tgz"
                        ),
                        "integrity": registry_integrity,
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            GENUINE_CLI_CONTENT = b"// genuine, tarball-pinned cli.js\n"
            TAMPERED_INDEX_JS = b"module.exports = 'ATTACKER-CONTROLLED';\n"

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_bytes(GENUINE_CLI_CONTENT)
                    os.chmod(cli, 0o600)

                    hex_digest = base64.b64decode(
                        registry_integrity[len("sha512-"):]
                    ).hex()
                    cache_content_path = (
                        Path(cache)
                        / "_cacache/content-v2/sha512"
                        / hex_digest[0:2]
                        / hex_digest[2:4]
                        / hex_digest[4:]
                    )
                    cache_content_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_content_path.write_bytes(registry_raw)

                    registry_dir = cwd / registry_lock_path
                    registry_dir.mkdir(parents=True, mode=0o700)
                    os.chmod(registry_dir, 0o700)
                    index_js = registry_dir / "index.js"
                    index_js.write_bytes(
                        TAMPERED_INDEX_JS if tamper else registry_files["index.js"]
                    )
                    os.chmod(index_js, 0o600)

            def fake_make_patched_asset(
                original_asset, original_sha256, upstream_lock, assets_dir,
                *, expected_name, managed_name, output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {Path("dist/bundle/cli.js"): installer.sha256_bytes(GENUINE_CLI_CONTENT)}
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            receipt_holder: dict = {}

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "GENERATED_LOCK_PACKAGE_COUNT",
                        len(local_assets) + 1,
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))

                if tamper:
                    with self.assertRaisesRegex(
                        installer.PrimeInstallError,
                        r"node_modules/left-pad-fake materialized file content "
                        r"does not match the digest-verified original tarball: "
                        r"index\.js",
                    ):
                        installer.install()
                else:
                    receipt_holder["receipt"] = installer.install()

            if tamper:
                self.assertFalse((release / "lib/node_modules").exists())
                self.assertFalse((tool_root / "pending-install.json").exists())
                self.assertFalse(
                    (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
                )
                return None
            return receipt_holder["receipt"]

    def test_install_locked_detects_tampered_registry_dependency_file_during_npm_ci(
        self,
    ) -> None:
        self._round36_registry_package_harness(tamper=True)

    def test_install_locked_verifies_clean_registry_dependency_content_and_records_receipt(
        self,
    ) -> None:
        receipt = self._round36_registry_package_harness(tamper=False)
        self.assertIsNotNone(receipt)
        self.assertIn(
            "node_modules/left-pad-fake",
            receipt["registry_packages_content_verified"],
        )

    def test_install_locked_detects_undeclared_sibling_package_planted_after_node_modules_move(
        self,
    ) -> None:
        # Regression for independent Claude opus/max round-35 review,
        # 2026-08-20, P1-2b: "assert_materialized_node_modules_matches_lock()
        # runs only once, before the node_modules move/rename, never again
        # afterward -- planting sibling packages ... AFTER the move
        # survives into the committed baseline; verify() reports ok:true.
        # The function's own docstring explicitly claims this exact case
        # (a new package being added) is caught, so this is a genuine
        # violation of its own stated contract."
        #
        # This test's fake_run_npm's "ci" branch materializes ONLY the
        # genuine prime-agent tree -- deliberately clean, no sibling planted
        # during npm ci's own window, so this test isolates the POST-move
        # racer specifically (a pre-move plant would already be caught by
        # the pre-existing pre-move call, and would not isolate this fix).
        # The plant instead happens via a wrapped, real
        # atomic_create_private_file() -- the FIRST call to it that happens
        # AFTER the node_modules -> lib/node_modules move (publishing the
        # launch guard) -- landing a same-UID racer's undeclared sibling
        # package directly under lib/node_modules/ in exactly the window
        # this round's fix closes.
        #
        # Verified to FAIL against pre-fix HEAD (commit dab6a143d9) in an
        # isolated scratch copy: assert_materialized_node_modules_matches_lock()
        # was called exactly once, before the move, so this post-move plant
        # went completely undetected and installer.install() completed
        # successfully with a clean receipt instead of raising.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            generated = {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "orca-managed-prime-agent",
                        "version": installer.VERSION,
                        "dependencies": {
                            "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                        },
                    },
                    **{
                        f"node_modules/local-{index}": {
                            "name": name,
                            "version": installer.VERSION,
                            "resolved": f"file:assets/{asset_name}",
                            "integrity": "sha512-dGVzdA==",
                        }
                        for index, (name, asset_name) in enumerate(local_assets.items())
                    },
                },
            }
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            GENUINE_CLI_CONTENT = b"// genuine, tarball-pinned cli.js\n"

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_bytes(GENUINE_CLI_CONTENT)
                    os.chmod(cli, 0o600)

            def fake_make_patched_asset(
                original_asset, original_sha256, upstream_lock, assets_dir,
                *, expected_name, managed_name, output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {Path("dist/bundle/cli.js"): installer.sha256_bytes(GENUINE_CLI_CONTENT)}
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            real_atomic_create_private_file = installer.atomic_create_private_file
            planted = {"done": False}
            launch_guard_path = release / "bin/prime-agent-launch-guard.py"
            evil_dir = release / "lib/node_modules/evil-post-move-sibling"

            def plant_after_move(path, raw, mode=0o600):
                real_atomic_create_private_file(path, raw, mode)
                if path == launch_guard_path and not planted["done"]:
                    evil_dir.mkdir(parents=True, mode=0o700)
                    os.chmod(evil_dir, 0o700)
                    (evil_dir / "package.json").write_bytes(
                        installer.canonical_json(
                            {"name": "evil-post-move-sibling", "version": "1.0.0"}
                        )
                    )
                    os.chmod(evil_dir / "package.json", 0o600)
                    planted["done"] = True

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_PACKAGE_COUNT", len(local_assets)
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))
                enter(
                    mock.patch.object(
                        installer,
                        "atomic_create_private_file",
                        side_effect=plant_after_move,
                    )
                )

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    r"npm materialized undeclared node_modules package\(s\)",
                ):
                    installer.install()

            self.assertTrue(planted["done"])
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_launch_guard_isolated_mode_defeats_stdlib_shadow_import(self) -> None:
        # Regression for independent Claude opus/max round-35 review,
        # 2026-08-20, P1-2a: "the launch guard's python exec lacks -I/-P,
        # so sys.path[0] is RELEASE_DIR/bin, meaning a same-UID racer who
        # plants bin/hashlib.py ... during the install window has it
        # silently imported and EXECUTED inside the launch guard's own
        # process, before validate_exec_target() even runs."
        #
        # Invokes the REAL generated launch guard script (via
        # managed_launch_guard_script(), not a hand-written stand-in) next
        # to a planted bin/hashlib.py shadow, under BOTH the pre-fix
        # invocation (`-B` only, exactly matching pre-fix HEAD
        # dab6a143d9's generated wrapper) and the fixed invocation
        # (`-B -I`, exactly matching managed_entrypoint_script()'s current
        # generated exec line -- see the assertion at the end of this test,
        # which reads that exact line back out of the real generated
        # wrapper bytes rather than assuming it matches). Confirms the
        # shadow IS imported pre-fix, and confirms BOTH that it is NOT
        # imported post-fix AND that the guard still genuinely functions
        # (execs the real NODE with the right argv) under the fix -- not
        # merely that something about it changed.
        #
        # Deliberately targets `/usr/bin/python3` -- the literal,
        # hardcoded interpreter path the generated wrapper execs, not
        # whatever `python3` a PATH lookup would find -- since this is the
        # one place a Python-version mismatch could otherwise hide a real
        # incompatibility (see the exec line's own comment for why `-P`
        # was deliberately NOT added alongside `-I`: it does not exist on
        # this machine's own `/usr/bin/python3`, verified empirically this
        # same round).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            tool_root.mkdir(mode=0o700)
            lock = tool_root / "lifecycle.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
            ready = root / "ready"
            # Pre-seeded with non-empty placeholder content and pinned to
            # THAT content's own real digest (not the post-exec, truncated
            # content) -- validate_exec_target() checks CLI's content
            # BEFORE fake_node ever runs and truncates `ready` as its own
            # readiness signal, exactly mirroring
            # test_generated_launch_guard_holds_shared_lock_for_child_lifetime's
            # own established pattern above.
            ready.write_bytes(b"not-yet-truncated-by-fake-node")
            os.chmod(ready, 0o600)
            ready_placeholder_sha256 = installer.sha256_file(ready)
            fake_node = root / "fake-node"
            fake_node.write_text('#!/bin/sh\n: > "$1"\n', encoding="utf-8")
            os.chmod(fake_node, 0o700)
            guard = root / "prime-agent-launch-guard.py"
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "TOOL_ROOT", tool_root),
            ):
                guard.write_bytes(
                    installer.managed_launch_guard_script(
                        fake_node,
                        ready,
                        installer.sha256_file(fake_node),
                        ready_placeholder_sha256,
                    )
                )
            os.chmod(guard, 0o700)

            # Same-UID racer's plant: a stdlib-shadowing module sitting
            # next to the launch guard -- sys.path[0] under the pre-fix,
            # `-B`-only invocation.
            shadow = root / "hashlib.py"
            shadow.write_text(
                "import sys\n"
                "sys.stderr.write('SHADOW-HASHLIB-IMPORTED\\n')\n",
                encoding="utf-8",
            )

            def run(*extra_flags: str) -> subprocess.CompletedProcess:
                ready.write_bytes(b"not-yet-truncated-by-fake-node")
                os.chmod(ready, 0o600)
                return subprocess.run(
                    ["/usr/bin/python3", "-B", *extra_flags, os.fspath(guard)],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                    env={"PYTHONDONTWRITEBYTECODE": "1"},
                )

            # Pre-fix invocation: the shadow IS imported. (This also
            # confirms this test's own exploit setup actually works,
            # before trusting the fixed invocation's negative assertion
            # below.)
            pre_fix = run()
            self.assertIn(
                "SHADOW-HASHLIB-IMPORTED",
                pre_fix.stderr,
                "the planted shadow hashlib.py was not imported under the "
                "pre-fix (-B only) invocation -- this test's own exploit "
                "setup is broken",
            )

            # Fixed invocation: the shadow is never reached, AND the guard
            # still genuinely functions -- execs fake_node for real, which
            # truncates `ready`.
            fixed = run("-I")
            self.assertNotIn("SHADOW-HASHLIB-IMPORTED", fixed.stderr)
            self.assertEqual(fixed.returncode, 0, fixed.stderr)
            self.assertEqual(ready.stat().st_size, 0, fixed.stderr)

        # The ACTUAL generated command wrapper uses this exact invocation
        # -- assert against the real generated bytes, not a
        # hand-transcribed copy of the flags, so this test fails if the
        # two ever drift apart.
        wrapper = installer.managed_entrypoint_script(
            Path("/nonexistent/node"),
            Path("/nonexistent/cli"),
            Path("/nonexistent/prime-agent-launch-guard.py"),
        ).decode("utf-8")
        self.assertIn(
            "exec /usr/bin/python3 -B -I /nonexistent/prime-agent-launch-guard.py",
            wrapper,
        )
        self.assertNotIn(" -P", wrapper)

    # ------------------------------------------------------------------
    # Round 38, 2026-08-20 (independent Claude opus/max round-37 review):
    # regression tests for the tree_digest() deny-unknown fix (P1-1/P1-2),
    # the registry-pinning post-move completeness assertion and
    # assert_materialized_node_modules_matches_lock() reverse-direction
    # check (P1-1, secondary), the upstream-package-lock.json download
    # digest re-check (P2-1), and the RELEASE_DIR post-quarantine refusal
    # gate (P2-2).
    # ------------------------------------------------------------------

    def test_allowed_unpinned_release_files_is_derived_from_current_constants(
        self,
    ) -> None:
        # Round 37's own reproduction, against a real install, measured
        # this exemption set at exactly 14 entries: 5 bookkeeping literals
        # plus 9 "assets/<name>" entries. Assert the CURRENT, real value
        # matches that count and content exactly, AND assert the function
        # actually tracks ASSETS/WORKSPACE_ASSETS/MAIN_PATCHED_ASSET/
        # NODE_ASSET rather than being a hardcoded literal that happens to
        # match today -- mutating those constants must change the
        # function's return value with no other code change.
        exempt = installer.allowed_unpinned_release_files()
        self.assertEqual(
            exempt,
            frozenset(
                {
                    "LICENSE",
                    "package.json",
                    "package-lock.json",
                    "upstream-package-lock.json",
                    "lib/node_modules/.package-lock.json",
                    *(f"assets/{name}" for name in installer.ASSETS),
                    f"assets/{installer.NODE_ASSET}",
                    f"assets/{installer.MAIN_PATCHED_ASSET}",
                    *(
                        f"assets/{name}"
                        for name in installer.WORKSPACE_ASSETS.values()
                    ),
                }
            ),
        )
        self.assertEqual(len(exempt), 14)
        # Every entry is genuinely derived from a constant, not hardcoded:
        # mutating ASSETS to add a new download must add a new
        # "assets/<name>" exemption with no other change.
        with mock.patch.object(
            installer,
            "ASSETS",
            {**installer.ASSETS, "extra-fake-asset.tgz": "0" * 64},
        ):
            grown = installer.allowed_unpinned_release_files()
        self.assertIn("assets/extra-fake-asset.tgz", grown)
        self.assertEqual(len(grown), 15)

    def test_lock_row_platform_excludes_current_target(self) -> None:
        excludes = installer.lock_row_platform_excludes_current_target
        # No constraint at all -- never excludes.
        self.assertFalse(excludes({"name": "x", "version": "1.0.0"}))
        # Allow-list form: current platform absent -> excludes.
        self.assertTrue(excludes({"os": ["win32"]}))
        self.assertTrue(excludes({"cpu": ["x64"]}))
        # Allow-list form: current platform present -> does not exclude.
        self.assertFalse(excludes({"os": ["darwin"]}))
        self.assertFalse(excludes({"cpu": ["arm64"]}))
        # Block-list ("!") form: current platform named -> excludes.
        self.assertTrue(excludes({"os": ["!darwin"]}))
        self.assertTrue(excludes({"cpu": ["!arm64"]}))
        # Block-list form: current platform NOT named -> does not exclude.
        self.assertFalse(excludes({"os": ["!win32"]}))
        # Either field alone is sufficient.
        self.assertTrue(excludes({"os": ["darwin"], "cpu": ["x64"]}))
        # Malformed/empty inputs fail closed (never excuse an absence).
        self.assertFalse(excludes({"os": []}))
        self.assertFalse(excludes({"os": "darwin"}))
        self.assertFalse(excludes("not-a-dict"))
        self.assertFalse(excludes(None))

    def test_assert_materialized_node_modules_matches_lock_reverse_top_level(
        self,
    ) -> None:
        # Round 38 reverse-direction regression, top-level: a declared
        # top-level row absent from the materialized tree, with no os/cpu
        # justification on its own lock row, fails closed when `packages`
        # is supplied and contains that row.
        with tempfile.TemporaryDirectory() as directory:
            release_dir = Path(directory).resolve() / "release"
            node_modules = release_dir / "node_modules/prime-agent"
            node_modules.mkdir(parents=True, mode=0o700)
            self._chmod_tree_private(release_dir)
            declared_top_level = frozenset(
                {"node_modules/prime-agent", "node_modules/missing-pkg"}
            )
            with self.assertRaisesRegex(
                installer.PrimeInstallError,
                r"declared node_modules package is missing from the materialized "
                r"tree and is not justified.*missing-pkg",
            ):
                installer.assert_materialized_node_modules_matches_lock(
                    release_dir,
                    declared_top_level,
                    {},
                    packages={
                        "node_modules/missing-pkg": {
                            "name": "missing-pkg",
                            "version": "1.0.0",
                        }
                    },
                )

    def test_assert_materialized_node_modules_matches_lock_reverse_top_level_platform_justified(
        self,
    ) -> None:
        # Same absence as above, but this time the row's own "os"
        # constraint genuinely excludes darwin -- a legitimate,
        # platform-conditional skip, so this must NOT raise.
        with tempfile.TemporaryDirectory() as directory:
            release_dir = Path(directory).resolve() / "release"
            node_modules = release_dir / "node_modules/prime-agent"
            node_modules.mkdir(parents=True, mode=0o700)
            self._chmod_tree_private(release_dir)
            declared_top_level = frozenset(
                {"node_modules/prime-agent", "node_modules/missing-pkg"}
            )
            # Must not raise.
            installer.assert_materialized_node_modules_matches_lock(
                release_dir,
                declared_top_level,
                {},
                packages={
                    "node_modules/missing-pkg": {
                        "name": "missing-pkg",
                        "version": "1.0.0",
                        "os": ["win32"],
                        "optional": True,
                    }
                },
            )

    def test_assert_materialized_node_modules_matches_lock_reverse_top_level_row_not_in_packages(
        self,
    ) -> None:
        # A declared top-level absence whose lock_path is not a key of
        # `packages` at all (the scoping convention real call sites use to
        # exclude the four locally patched/workspace packages -- see
        # assert_materialized_node_modules_matches_lock()'s own round-38
        # docstring paragraph) is treated as nothing to verify here, not
        # as an unjustified absence.
        with tempfile.TemporaryDirectory() as directory:
            release_dir = Path(directory).resolve() / "release"
            node_modules = release_dir / "node_modules/prime-agent"
            node_modules.mkdir(parents=True, mode=0o700)
            self._chmod_tree_private(release_dir)
            declared_top_level = frozenset(
                {"node_modules/prime-agent", "node_modules/@earendil-works/pi-ai"}
            )
            # Must not raise: "node_modules/@earendil-works/pi-ai" is not a
            # key of the (deliberately empty, here) `packages` map.
            installer.assert_materialized_node_modules_matches_lock(
                release_dir, declared_top_level, {}, packages={}
            )

    def test_assert_materialized_node_modules_matches_lock_reverse_nested(
        self,
    ) -> None:
        # Round 38 reverse-direction regression, nested: a declared child
        # of a container that WAS materialized (the parent package's own
        # nested node_modules/ genuinely exists on disk) is itself absent,
        # with no os/cpu justification -- fails closed.
        with tempfile.TemporaryDirectory() as directory:
            release_dir = Path(directory).resolve() / "release"
            container = release_dir / "node_modules/parent-pkg/node_modules"
            (container / "present-child").mkdir(parents=True, mode=0o700)
            self._chmod_tree_private(release_dir)
            declared_top_level = frozenset({"node_modules/parent-pkg"})
            declared_nested = {
                "node_modules/parent-pkg/node_modules": frozenset(
                    {"present-child", "missing-child"}
                )
            }
            with self.assertRaisesRegex(
                installer.PrimeInstallError,
                r"declared nested node_modules package is missing from the "
                r"materialized tree.*missing-child",
            ):
                installer.assert_materialized_node_modules_matches_lock(
                    release_dir,
                    declared_top_level,
                    declared_nested,
                    packages={
                        "node_modules/parent-pkg/node_modules/missing-child": {
                            "name": "missing-child",
                            "version": "1.0.0",
                        }
                    },
                )

    def test_assert_materialized_node_modules_matches_lock_reverse_nested_container_absent_is_skipped(
        self,
    ) -> None:
        # The container itself was never materialized at all (its own
        # parent package directory is missing) -- deliberately NOT treated
        # as "every declared child is individually unjustified", since
        # that would not generally be true; this is skipped rather than
        # guessed at (see this function's own round-38 comment at this
        # exact `continue`).
        with tempfile.TemporaryDirectory() as directory:
            release_dir = Path(directory).resolve() / "release"
            release_dir.mkdir(parents=True, mode=0o700)
            (release_dir / "node_modules").mkdir(mode=0o700)
            self._chmod_tree_private(release_dir)
            # `parent-pkg` itself is declared top-level but genuinely
            # excused (platform-conditional), so the top-level reverse
            # check does not fire for it either -- isolating this test to
            # the nested-container-absent skip specifically.
            declared_top_level = frozenset({"node_modules/parent-pkg"})
            declared_nested = {
                "node_modules/parent-pkg/node_modules": frozenset({"missing-child"})
            }
            # Must not raise.
            installer.assert_materialized_node_modules_matches_lock(
                release_dir,
                declared_top_level,
                declared_nested,
                packages={
                    "node_modules/parent-pkg": {
                        "name": "parent-pkg",
                        "version": "1.0.0",
                        "os": ["win32"],
                        "optional": True,
                    },
                    "node_modules/parent-pkg/node_modules/missing-child": {
                        "name": "missing-child",
                        "version": "1.0.0",
                    },
                },
            )

    def test_assert_materialized_node_modules_matches_lock_reverse_nested_parent_present_container_absent(
        self,
    ) -> None:
        # Round 41 regression (independent Codex sol/max round-39 review,
        # P1-C): unlike the sibling
        # ..._reverse_nested_container_absent_is_skipped test above, here
        # the PARENT package directory genuinely IS materialized on disk
        # (present, and not platform-excluded -- the top-level reverse
        # check for it would pass) -- only its own nested node_modules/
        # container is absent, even though the lock declares a
        # non-platform-excluded child for that container. Before the
        # round-41 fix, this went completely unnoticed: `container_path`
        # not being in `materialized_by_container` was treated identically
        # to the parent itself being absent. This must now fail closed.
        with tempfile.TemporaryDirectory() as directory:
            release_dir = Path(directory).resolve() / "release"
            parent_pkg = release_dir / "node_modules/parent-pkg"
            parent_pkg.mkdir(parents=True, mode=0o700)
            # Deliberately NOT creating parent_pkg/node_modules at all --
            # the parent package directory exists, but its own nested
            # container does not.
            self._chmod_tree_private(release_dir)
            declared_top_level = frozenset({"node_modules/parent-pkg"})
            declared_nested = {
                "node_modules/parent-pkg/node_modules": frozenset({"missing-child"})
            }
            with self.assertRaisesRegex(
                installer.PrimeInstallError,
                r"declared nested node_modules package's own parent container "
                r"is absent from the materialized tree.*missing-child",
            ):
                installer.assert_materialized_node_modules_matches_lock(
                    release_dir,
                    declared_top_level,
                    declared_nested,
                    packages={
                        "node_modules/parent-pkg": {
                            "name": "parent-pkg",
                            "version": "1.0.0",
                        },
                        "node_modules/parent-pkg/node_modules/missing-child": {
                            "name": "missing-child",
                            "version": "1.0.0",
                        },
                    },
                )

    def test_assert_materialized_node_modules_matches_lock_reverse_nested_parent_present_container_absent_platform_justified(
        self,
    ) -> None:
        # Positive control for the round-41 fix immediately above: the
        # PARENT package directory is materialized, its own nested
        # node_modules/ container is absent, and the sole declared child
        # of that container is genuinely platform-excluded (an "os"
        # constraint that excludes darwin) -- a legitimate,
        # platform-conditional skip. Confirms the round-41 fix does not
        # over-tighten: this must NOT raise.
        with tempfile.TemporaryDirectory() as directory:
            release_dir = Path(directory).resolve() / "release"
            parent_pkg = release_dir / "node_modules/parent-pkg"
            parent_pkg.mkdir(parents=True, mode=0o700)
            # Deliberately NOT creating parent_pkg/node_modules.
            self._chmod_tree_private(release_dir)
            declared_top_level = frozenset({"node_modules/parent-pkg"})
            declared_nested = {
                "node_modules/parent-pkg/node_modules": frozenset(
                    {"platform-excluded-child"}
                )
            }
            # Must not raise.
            installer.assert_materialized_node_modules_matches_lock(
                release_dir,
                declared_top_level,
                declared_nested,
                packages={
                    "node_modules/parent-pkg": {
                        "name": "parent-pkg",
                        "version": "1.0.0",
                    },
                    "node_modules/parent-pkg/node_modules/platform-excluded-child": {
                        "name": "platform-excluded-child",
                        "version": "1.0.0",
                        "os": ["win32"],
                        "optional": True,
                    },
                },
            )

    def test_assert_materialized_node_modules_matches_lock_reverse_nested_parent_present_container_absent_deep(
        self,
    ) -> None:
        # Round 41 regression, arbitrary nesting depth: the SAME
        # parent-present/container-absent gap as the two tests above, but
        # one level deeper -- "parent-pkg" is materialized at the top
        # level, its own nested node_modules/ container IS materialized
        # and contains "mid-pkg", but mid-pkg's OWN nested node_modules/
        # container is absent even though the lock declares a
        # non-platform-excluded child for it. Confirms the round-41 fix
        # generalizes past exactly one level of nesting.
        with tempfile.TemporaryDirectory() as directory:
            release_dir = Path(directory).resolve() / "release"
            mid_pkg = release_dir / "node_modules/parent-pkg/node_modules/mid-pkg"
            mid_pkg.mkdir(parents=True, mode=0o700)
            # Deliberately NOT creating
            # node_modules/parent-pkg/node_modules/mid-pkg/node_modules.
            self._chmod_tree_private(release_dir)
            declared_top_level = frozenset({"node_modules/parent-pkg"})
            declared_nested = {
                "node_modules/parent-pkg/node_modules": frozenset({"mid-pkg"}),
                "node_modules/parent-pkg/node_modules/mid-pkg/node_modules": frozenset(
                    {"deep-missing-child"}
                ),
            }
            with self.assertRaisesRegex(
                installer.PrimeInstallError,
                r"declared nested node_modules package's own parent container "
                r"is absent from the materialized tree.*deep-missing-child",
            ):
                installer.assert_materialized_node_modules_matches_lock(
                    release_dir,
                    declared_top_level,
                    declared_nested,
                    packages={
                        "node_modules/parent-pkg": {
                            "name": "parent-pkg",
                            "version": "1.0.0",
                        },
                        "node_modules/parent-pkg/node_modules/mid-pkg": {
                            "name": "mid-pkg",
                            "version": "1.0.0",
                        },
                        "node_modules/parent-pkg/node_modules/mid-pkg/node_modules/deep-missing-child": {
                            "name": "deep-missing-child",
                            "version": "1.0.0",
                        },
                    },
                )

    def _chmod_tree_private(self, root: Path) -> None:
        for dirpath, _dirnames, _filenames in os.walk(root):
            os.chmod(dirpath, 0o700)

    def _round38_deny_unknown_tree_digest_harness(
        self, *, plant_relative_dir: str | None, plant_relative_file: str
    ) -> None:
        """Shared harness for the round-38 tree_digest() deny-unknown
        regression tests below: builds a minimal tree with ONE genuinely
        pinned regular file, plants ONE extra, entirely unpinned regular
        file at `plant_relative_file` (creating `plant_relative_dir`
        first, if given), and confirms tree_digest() now refuses the call
        -- exercising the round-38 "every observed regular file must be
        pinned or an accepted exemption" half of the completeness
        invariant directly, isolated from the full install() pipeline and
        from the round-34 "every pinned key must be observed" half (see
        `_round34_p1_1_completeness_harness()`, just above, for that other
        half's own regression tests).

        Verified to FAIL against pre-fix HEAD (commit 36a4008c08) via a
        standalone scratch script importing that commit's tree_digest()
        directly: for every one of the four locations this harness is
        used against below (RELEASE_DIR root, bin/, toolchain/, and a
        node_modules/-shaped path), tree_digest() returned a digest/count
        pair successfully, silently folding the planted file into the
        recorded baseline with zero comparison against anything -- exactly
        as if `pinned_relative_digests` had never been passed for that
        path at all.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            pinned_content = b"genuine pinned content\n"
            pinned_path = root / "pinned-file.js"
            pinned_path.write_bytes(pinned_content)
            pinned_digest = installer.sha256_bytes(pinned_content)
            if plant_relative_dir is not None:
                (root / plant_relative_dir).mkdir(parents=True, mode=0o700)
            stray_path = root / plant_relative_file
            stray_path.write_bytes(b"attacker-controlled\n")
            self._chmod_tree_private(root)
            os.chmod(pinned_path, 0o600)
            os.chmod(stray_path, 0o600)
            pinned_relative_digests = {"pinned-file.js": pinned_digest}
            with mock.patch.object(installer, "SSD_ROOT", root):
                with self.assertRaises(installer.PrimeInstallError) as cm:
                    installer.tree_digest(
                        root, pinned_relative_digests=pinned_relative_digests
                    )
            message = str(cm.exception)
            self.assertIn(
                "neither pinned nor an accepted unpinned exemption", message
            )
            self.assertIn(plant_relative_file, message)

    def test_tree_digest_deny_unknown_catches_stray_file_at_release_root(
        self,
    ) -> None:
        self._round38_deny_unknown_tree_digest_harness(
            plant_relative_dir=None,
            plant_relative_file="stray-root-file.js",
        )

    def test_tree_digest_deny_unknown_catches_stray_file_under_bin(self) -> None:
        self._round38_deny_unknown_tree_digest_harness(
            plant_relative_dir="bin",
            plant_relative_file="bin/stray-bin-file.js",
        )

    def test_tree_digest_deny_unknown_catches_stray_file_under_toolchain(
        self,
    ) -> None:
        self._round38_deny_unknown_tree_digest_harness(
            plant_relative_dir="toolchain",
            plant_relative_file="toolchain/stray-toolchain-file.js",
        )

    def test_tree_digest_deny_unknown_catches_stray_file_under_node_modules(
        self,
    ) -> None:
        # Round 37's own zero-race P1-1(b) reproduction planted content at
        # a declared-but-always-platform-skipped registry row's own path
        # (`@mariozechner/clipboard-*` on this platform) -- this test
        # models that same shape of location (a scoped package directory
        # directly under node_modules/) generically, isolated from the
        # full install() pipeline. See
        # test_install_locked_detects_content_planted_at_always_skipped_registry_row()
        # below for the full end-to-end reproduction through the real
        # install() pipeline.
        self._round38_deny_unknown_tree_digest_harness(
            plant_relative_dir="node_modules/@mariozechner/clipboard-win32",
            plant_relative_file="node_modules/@mariozechner/clipboard-win32/index.js",
        )

    def test_tree_digest_deny_unknown_accepts_allowed_unpinned_release_files(
        self,
    ) -> None:
        # Negative counterpart to the four tests above: every path
        # allowed_unpinned_release_files() names is exempt, so a tree
        # containing ONLY a pinned file plus every one of those exempt
        # paths (populated with arbitrary, unpinned content) is accepted
        # normally -- proving the exemption set genuinely suppresses the
        # new check for the paths it is supposed to, not merely that the
        # check is broken/always-permissive.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            pinned_content = b"genuine pinned content\n"
            pinned_path = root / "pinned-file.js"
            pinned_path.write_bytes(pinned_content)
            pinned_digest = installer.sha256_bytes(pinned_content)
            for relative in installer.allowed_unpinned_release_files():
                exempt_path = root / relative
                exempt_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                exempt_path.write_bytes(b"unpinned but accepted content\n")
            self._chmod_tree_private(root)
            os.chmod(pinned_path, 0o600)
            for relative in installer.allowed_unpinned_release_files():
                os.chmod(root / relative, 0o600)
            pinned_relative_digests = {"pinned-file.js": pinned_digest}
            with mock.patch.object(installer, "SSD_ROOT", root):
                # Must not raise.
                installer.tree_digest(
                    root, pinned_relative_digests=pinned_relative_digests
                )

    def _round40_symlink_deny_unknown_tree_digest_harness(
        self, *, plant_relative_dir: str | None, plant_relative_symlink: str
    ) -> None:
        """Shared harness for the round-40 tree_digest() symlink deny-unknown
        regression tests below: builds a minimal tree with ONE genuinely
        pinned regular file and ONE unpinned-but-exempt file named LICENSE
        (a real allowed_unpinned_release_files() member -- present in every
        genuine release tree, verified once at download time, but
        deliberately never a key of `pinned_relative_digests`; see that
        function's own docstring), plants a SYMLINK at
        `plant_relative_symlink` (creating `plant_relative_dir` first, if
        given) pointing at LICENSE, and confirms tree_digest() now refuses
        the call -- exercising round 40's symlink half of the completeness
        invariant directly, isolated from the full install() pipeline.

        Mirrors the independent round-39 review's own real, non-mocked Node
        reproduction: planting a symlink at a node_modules-package-shaped
        path pointing at LICENSE, confirming real Node's own extensionless-
        symlink-target loader fallback loads LICENSE's content and executes
        it as JavaScript (empirically re-confirmed against a real pinned
        Node binary while implementing this fix -- see this round's own
        dispatch notes for the exact transcript).

        Verified to FAIL against pre-fix HEAD (commit 8160356b9a) via a
        standalone scratch script importing that commit's tree_digest()
        directly: for every location this harness is used against below,
        tree_digest() returned a digest/count pair successfully, silently
        folding the symlink into the recorded baseline with zero comparison
        against anything -- round 38's regular-file-only deny-unknown check
        never populates anything from the `S_ISLNK` branch at all, so it is
        structurally incapable of ever comparing a symlink against
        anything.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            pinned_content = b"genuine pinned content\n"
            pinned_path = root / "pinned-file.js"
            pinned_path.write_bytes(pinned_content)
            pinned_digest = installer.sha256_bytes(pinned_content)
            exempt_content = b"console.log('EXPLOIT_EXECUTED_VIA_SYMLINK');\n"
            exempt_path = root / "LICENSE"
            exempt_path.write_bytes(exempt_content)
            self.assertIn("LICENSE", installer.allowed_unpinned_release_files())
            if plant_relative_dir is not None:
                (root / plant_relative_dir).mkdir(parents=True, mode=0o700)
            symlink_path = root / plant_relative_symlink
            symlink_path.symlink_to(
                Path(os.path.relpath(exempt_path, symlink_path.parent))
            )
            self._chmod_tree_private(root)
            os.chmod(pinned_path, 0o600)
            os.chmod(exempt_path, 0o600)
            pinned_relative_digests = {"pinned-file.js": pinned_digest}
            with mock.patch.object(installer, "SSD_ROOT", root):
                with self.assertRaises(installer.PrimeInstallError) as cm:
                    installer.tree_digest(
                        root, pinned_relative_digests=pinned_relative_digests
                    )
            message = str(cm.exception)
            self.assertIn(
                "neither a package's own .bin/ entry nor resolved to a "
                "pinned, digest-verified regular file",
                message,
            )
            self.assertIn(plant_relative_symlink, message)

    def test_tree_digest_deny_unknown_catches_symlink_to_license_at_node_modules_top_level(
        self,
    ) -> None:
        # The reviewer's exact reproduction shape: a symlink directly under
        # node_modules/, named like a real optional native dependency the
        # pinned bundle unconditionally require()s (bufferutil).
        self._round40_symlink_deny_unknown_tree_digest_harness(
            plant_relative_dir="node_modules",
            plant_relative_symlink="node_modules/bufferutil",
        )

    def test_tree_digest_deny_unknown_catches_symlink_to_license_at_scoped_package_path(
        self,
    ) -> None:
        self._round40_symlink_deny_unknown_tree_digest_harness(
            plant_relative_dir="node_modules/@mariozechner",
            plant_relative_symlink="node_modules/@mariozechner/clipboard-win32",
        )

    def test_tree_digest_deny_unknown_catches_symlink_to_license_under_bin(
        self,
    ) -> None:
        self._round40_symlink_deny_unknown_tree_digest_harness(
            plant_relative_dir="bin",
            plant_relative_symlink="bin/evil-binary",
        )

    def test_tree_digest_deny_unknown_catches_symlink_to_license_under_toolchain(
        self,
    ) -> None:
        self._round40_symlink_deny_unknown_tree_digest_harness(
            plant_relative_dir="toolchain/bin",
            plant_relative_symlink="toolchain/bin/evil-node",
        )

    def test_tree_digest_deny_unknown_catches_symlink_to_license_inside_dot_bin(
        self,
    ) -> None:
        # The sneaky variant: the symlink DOES sit inside a `.bin/`
        # directory (the one structural property genuine npm-produced
        # symlinks share), but its target -- LICENSE -- is not a pinned,
        # digest-verified regular file. Proves the check requires BOTH
        # conditions, not just the `.bin/` placement alone (which would
        # have silently reopened the identical hole one level up: an
        # attacker who also gets to choose the parent directory name could
        # simply name it ".bin").
        self._round40_symlink_deny_unknown_tree_digest_harness(
            plant_relative_dir="node_modules/.bin",
            plant_relative_symlink="node_modules/.bin/evil",
        )

    def test_tree_digest_deny_unknown_accepts_genuine_bin_symlink_to_pinned_target(
        self,
    ) -> None:
        # Positive counterpart to the five rejection tests above (and to
        # test_tree_digest_deny_unknown_accepts_allowed_unpinned_release_files
        # for the regular-file half): a symlink that IS what a genuine `npm
        # ci` actually produces -- directly inside a `.bin/` directory,
        # resolving to a target that IS a pinned, digest-verified regular
        # file -- is accepted normally. Proves round 40's new symlink check
        # genuinely discriminates between the real shape and the reviewer's
        # attack shape, rather than merely rejecting every symlink
        # unconditionally (which would make a genuine, clean install fail
        # closed on its own legitimate `.bin/` entries).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            pinned_content = b"genuine pinned content\n"
            pinned_path = root / "node_modules/some-package/bin/cli.js"
            pinned_path.parent.mkdir(parents=True, mode=0o700)
            pinned_path.write_bytes(pinned_content)
            pinned_digest = installer.sha256_bytes(pinned_content)
            bin_dir = root / "node_modules/.bin"
            bin_dir.mkdir(mode=0o700)
            symlink_path = bin_dir / "some-package"
            symlink_path.symlink_to(Path(os.path.relpath(pinned_path, bin_dir)))
            self._chmod_tree_private(root)
            os.chmod(pinned_path, 0o600)
            pinned_relative_digests = {
                "node_modules/some-package/bin/cli.js": pinned_digest
            }
            with mock.patch.object(installer, "SSD_ROOT", root):
                # Must not raise.
                installer.tree_digest(
                    root, pinned_relative_digests=pinned_relative_digests
                )

    def test_assert_no_unexpected_ancestor_node_modules_accepts_clean_chain(
        self,
    ) -> None:
        # verify()'s counterpart to the generated launch guard's
        # unexpected_ancestor_node_modules() -- see
        # test_unexpected_ancestor_node_modules_detects_real_node_rce_path
        # above for the real-Node grounding of the underlying finding this
        # closes (independent Claude opus/max round-39 review, P1-2). Pure
        # Python logic, so no real Node needed here; the shared invariant
        # (Node's ancestor node_modules walk reaches outside RELEASE_DIR)
        # is what the real-Node tests above already establish.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "tool/releases/v0.7.2"
            release.mkdir(parents=True)
            # Must not raise.
            installer.assert_no_unexpected_ancestor_node_modules(release)

    def test_assert_no_unexpected_ancestor_node_modules_catches_directory_two_levels_up(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "tool/releases/v0.7.2"
            release.mkdir(parents=True)
            (root / "tool/node_modules").mkdir()
            with self.assertRaisesRegex(
                installer.PrimeInstallError,
                r"unexpected node_modules directory.*tool/node_modules$",
            ):
                installer.assert_no_unexpected_ancestor_node_modules(release)

    def test_assert_no_unexpected_ancestor_node_modules_catches_dangling_symlink(
        self,
    ) -> None:
        # os.path.lexists (not os.path.exists) is what this function uses,
        # specifically so a dangling symlink -- which os.path.exists()
        # alone would silently treat as absent -- is still caught.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "tool/releases/v0.7.2"
            release.mkdir(parents=True)
            bad = root / "tool/releases/node_modules"
            os.symlink("nonexistent-target", bad)
            self.assertFalse(os.path.exists(bad))
            self.assertTrue(os.path.lexists(bad))
            with self.assertRaises(installer.PrimeInstallError):
                installer.assert_no_unexpected_ancestor_node_modules(release)

    def test_assert_no_unexpected_ancestor_node_modules_ignores_locations_inside_release_dir(
        self,
    ) -> None:
        # Negative counterpart: a node_modules directory INSIDE release_dir
        # itself (the legitimate one, plus any legitimately nested ones)
        # is not this function's concern at all -- it only walks STRICTLY
        # ABOVE release_dir. Those in-tree locations are covered by
        # tree_digest()'s own completeness invariant instead (see
        # tree_digest()'s docstring).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "tool/releases/v0.7.2"
            (release / "lib/node_modules").mkdir(parents=True)
            (release / "node_modules").mkdir()
            # Must not raise -- both are inside release_dir.
            installer.assert_no_unexpected_ancestor_node_modules(release)

    def _round38_full_install_stray_harness(
        self,
        *,
        extra_registry_lock_path: str | None = None,
        plant_relative_path: str,
        expected_message_fragment: str,
        plant_as_symlink: bool = False,
    ) -> None:
        """Shared harness for the round-38 end-to-end regression tests
        below: runs the REAL, unmocked `_install_locked_within_release_dir()`
        pipeline (through the top-level `installer.install()` entry point,
        the same way the round-36 registry-package and round-35
        undeclared-sibling-package harnesses elsewhere in this file do),
        with `fake_run_npm`'s "ci" branch materializing ONLY the genuine
        prime-agent tree -- deliberately never creating anything at
        `plant_relative_path` itself, modeling either a declared registry
        row this platform's `npm ci` never downloads at all (round 37's
        own zero-race P1-1(b) finding, when `extra_registry_lock_path` is
        given) or the pre-move node_modules/ path after it has been
        renamed away (round 37's own P1-2 finding, when it is not). The
        plant itself happens via a wrapped, real
        `atomic_create_private_file()` -- the FIRST call to it that
        happens AFTER the launch guard is published, i.e. after the
        node_modules -> lib/node_modules move -- landing a same-UID
        racer's content at `plant_relative_path` sometime before
        write_pending_install()'s own tree_digest() call, exactly
        mirroring the existing
        test_install_locked_detects_undeclared_sibling_package_planted_after_node_modules_move
        test's own established technique.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            local_assets = {
                "prime-agent": installer.MAIN_PATCHED_ASSET,
                **installer.WORKSPACE_ASSETS,
            }
            packages: dict[str, object] = {
                "": {
                    "name": "orca-managed-prime-agent",
                    "version": installer.VERSION,
                    "dependencies": {
                        "prime-agent": f"file:assets/{installer.MAIN_PATCHED_ASSET}"
                    },
                },
                **{
                    f"node_modules/local-{index}": {
                        "name": name,
                        "version": installer.VERSION,
                        "resolved": f"file:assets/{asset_name}",
                        "integrity": "sha512-dGVzdA==",
                    }
                    for index, (name, asset_name) in enumerate(local_assets.items())
                },
            }
            if extra_registry_lock_path is not None:
                # The declared PACKAGE name (e.g. "@mariozechner/clipboard-win32"
                # for a scoped package) -- everything after the row's OWN
                # last "node_modules/" segment, matching exactly how
                # package_name_from_lock_path()/declared_top_level_node_modules_packages()
                # resolve a row's identity when it has no explicit "name"
                # override that would take precedence. Deliberately NOT
                # `.rsplit("/", 1)[-1]`, which would drop a scoped
                # package's own "@scope/" prefix and make this row appear
                # as an entirely different (unscoped) declared name than
                # what actually gets materialized on disk.
                declared_name = extra_registry_lock_path.split("node_modules/", 1)[-1]
                bare_name = declared_name.rsplit("/", 1)[-1]
                packages[extra_registry_lock_path] = {
                    "name": declared_name,
                    "version": "1.0.0",
                    "resolved": (
                        f"https://registry.npmjs.org/{bare_name}/-/"
                        f"{bare_name}-1.0.0.tgz"
                    ),
                    "integrity": "sha512-dGVzdA==",
                    # A real declared-but-always-skipped-on-darwin-arm64
                    # row (round 37's own `@mariozechner/clipboard-*`
                    # finding) genuinely carries an "os" constraint that
                    # excludes this platform -- included here so this
                    # fixture is a faithful model, not merely a row this
                    # installer's own new reverse-direction check would
                    # ALSO separately flag as unjustified.
                    "os": ["win32"],
                    "optional": True,
                }
            generated = {"lockfileVersion": 3, "packages": packages}
            with mock.patch.object(installer, "RELEASE_DIR", release):
                expected_lock_sha256 = installer.sha256_bytes(
                    installer.normalized_production_lock(generated)
                )
            generated_lock_raw = installer.canonical_json(generated)

            GENUINE_CLI_CONTENT = b"// genuine, tarball-pinned cli.js\n"

            def fake_run_npm(
                npm_path, node_path, args, cwd, cache, install_home, install_tmp,
                timeout=300, child_umask=None, release_dir_fd=None,
            ):
                cwd = Path(cwd)
                if args and args[0] == "install":
                    lock_path = cwd / "package-lock.json"
                    lock_path.write_bytes(generated_lock_raw)
                    os.chmod(lock_path, 0o600)
                elif args and args[0] == "ci":
                    bundle = cwd / "node_modules/prime-agent/dist/bundle"
                    bundle.mkdir(parents=True, mode=0o700)
                    for ancestor in (
                        cwd / "node_modules",
                        cwd / "node_modules/prime-agent",
                        cwd / "node_modules/prime-agent/dist",
                        bundle,
                    ):
                        os.chmod(ancestor, 0o700)
                    cli = bundle / "cli.js"
                    cli.write_bytes(GENUINE_CLI_CONTENT)
                    os.chmod(cli, 0o600)
                    # Deliberately never materializes anything for
                    # `extra_registry_lock_path` -- modeling this
                    # platform's `npm ci` genuinely, unconditionally
                    # skipping it (round 37's own zero-race P1-1(b)
                    # finding: nothing legitimate ever contends for that
                    # exact path).

            def fake_make_patched_asset(
                original_asset, original_sha256, upstream_lock, assets_dir,
                *, expected_name, managed_name, output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                published_stat = patched.lstat()
                content_digests = (
                    {Path("dist/bundle/cli.js"): installer.sha256_bytes(GENUINE_CLI_CONTENT)}
                    if managed_name == "prime-agent"
                    else {}
                )
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
                    (published_stat.st_dev, published_stat.st_ino),
                    content_digests,
                )

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            real_atomic_create_private_file = installer.atomic_create_private_file
            planted = {"done": False}
            launch_guard_path = release / "bin/prime-agent-launch-guard.py"

            def plant_after_move(path, raw, mode=0o600):
                real_atomic_create_private_file(path, raw, mode)
                if path == launch_guard_path and not planted["done"]:
                    target = release / plant_relative_path
                    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    if plant_as_symlink:
                        # Round 40, 2026-08-20: a symlink-shaped
                        # materialization (rather than a regular file) at
                        # the previously-skipped registry package's own
                        # path -- see the round-40 fix to the post-move
                        # `skipped_registry_lock_paths` assertion in
                        # `_install_locked_within_release_dir()`, which
                        # used to check `is_dir() and not is_symlink()`
                        # (False, i.e. "still absent", for exactly this
                        # shape). Points at `launch_guard_path`'s own
                        # parent directory (guaranteed to exist by this
                        # point, since the launch guard was just written)
                        # so `post_move_dir.is_dir()` would ALSO have been
                        # True pre-fix, following the symlink -- proving
                        # this is caught by the "is a symlink at all" half
                        # of the fix, not merely by "is not a directory".
                        target.symlink_to(
                            Path(os.path.relpath(launch_guard_path.parent, target.parent))
                        )
                    else:
                        target.write_bytes(b"attacker-controlled\n")
                        os.chmod(target, 0o600)
                    for dirpath, _dirnames, _filenames in os.walk(target.parent):
                        os.chmod(dirpath, 0o700)
                    planted["done"] = True

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "GENERATED_LOCK_SHA256", expected_lock_sha256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "GENERATED_LOCK_PACKAGE_COUNT",
                        len(local_assets) + (1 if extra_registry_lock_path else 0),
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "LOCK_SHA256", STUB_UPSTREAM_LOCK_SHA256
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "extract_node_toolchain",
                        side_effect=self.fake_extract_node_toolchain,
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "exact_tool_version", return_value="stub"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "make_patched_asset",
                        side_effect=fake_make_patched_asset,
                    )
                )
                enter(mock.patch.object(installer, "run_npm", side_effect=fake_run_npm))
                enter(
                    mock.patch.object(
                        installer,
                        "atomic_create_private_file",
                        side_effect=plant_after_move,
                    )
                )

                with self.assertRaises(installer.PrimeInstallError) as cm:
                    installer.install()
                self.assertIn(expected_message_fragment, str(cm.exception))

            self.assertTrue(planted["done"])
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_install_locked_detects_content_planted_at_always_skipped_registry_row(
        self,
    ) -> None:
        # Round 37's own P1-1(b) "zero-race" reproduction: a declared
        # registry row this platform's `npm ci` unconditionally skips
        # every single install (round 37's real finding:
        # `@mariozechner/clipboard-*`, modeled here generically) has
        # content planted at its exact declared-but-never-materialized
        # path. Pre-fix (commit 36a4008c08), verified via a standalone
        # scratch script exercising the equivalent pre-fix code path
        # directly: the pre-move presence probe's bare `continue` silently
        # excluded this row from `registry_pinned_digests` with nothing
        # downstream re-checking it, and neither
        # assert_materialized_node_modules_matches_lock() (only checks the
        # materialized-but-undeclared direction) nor tree_digest() (no
        # deny-unknown half) said anything about the planted content --
        # install() completed successfully with a clean receipt. Post-fix,
        # this is caught by the round-38 registry-pinning post-move
        # completeness assertion in
        # `_install_locked_within_release_dir()` (which raises first, with
        # a specific message) and independently, structurally, by
        # tree_digest()'s own deny-unknown check (see
        # test_tree_digest_deny_unknown_catches_stray_file_under_node_modules()
        # above for that check exercised in isolation) -- either mechanism
        # alone already refuses this exact plant.
        # Planted at its REAL post-move materialized location
        # (lib/node_modules/..., matching where this installer's own
        # `global_root.parent / lock_path` re-check looks -- see the
        # registry-pinning post-move completeness assertion in
        # `_install_locked_within_release_dir()`), NOT the pre-move
        # "node_modules/..." path -- planting at the pre-move path models
        # the DIFFERENT P1-2 finding instead (see the next test).
        self._round38_full_install_stray_harness(
            extra_registry_lock_path="node_modules/@mariozechner/clipboard-win32",
            plant_relative_path="lib/node_modules/@mariozechner/clipboard-win32/index.js",
            expected_message_fragment="@mariozechner/clipboard-win32",
        )

    def test_install_locked_detects_symlink_materialized_at_always_skipped_registry_row(
        self,
    ) -> None:
        # Round 40, 2026-08-20 (independent Claude opus/max round-39
        # review, P1-1): the SAME scenario as the test just above, but the
        # post-move materialization is a SYMLINK (to an existing, real
        # directory elsewhere in the release tree) rather than a regular
        # file inside a freshly created directory. Pre-fix (commit
        # 8160356b9a), the post-move `skipped_registry_lock_paths`
        # assertion checked `post_move_dir.is_dir() and not
        # post_move_dir.is_symlink()` -- which evaluates False (i.e. "not
        # materialized, still fine") for exactly this shape, since
        # `is_dir()` on a symlink-to-directory follows the link and
        # returns True, and `not is_symlink()` then makes the whole
        # conjunction False. Verified via a standalone scratch script
        # exercising that pre-fix condition directly against the plant
        # this test performs: it evaluates False, so pre-fix HEAD would
        # have silently accepted this plant and install() would have
        # completed with a clean receipt. Post-fix, `post_move_dir.exists()
        # or post_move_dir.is_symlink()` catches it -- `exists()` follows
        # the symlink to the real directory it points at and returns True.
        self._round38_full_install_stray_harness(
            extra_registry_lock_path="node_modules/@mariozechner/clipboard-win32",
            plant_relative_path="lib/node_modules/@mariozechner/clipboard-win32",
            expected_message_fragment="@mariozechner/clipboard-win32",
            plant_as_symlink=True,
        )

    def test_install_locked_detects_content_planted_at_pre_move_node_modules_path_after_move(
        self,
    ) -> None:
        # Round 37's own P1-2 reproduction: RELEASE_DIR/node_modules (the
        # pre-move path) is renamed away as a whole to lib/node_modules
        # early in `_install_locked_within_release_dir()` and never looked
        # at again by anything downstream through round 37 -- round 37's
        # own empirical check confirmed this path genuinely sits on real
        # Node's actual module resolution search path (walking up from the
        # entrypoint's own directory), and the real pinned prime-agent
        # 0.7.2 bundle unconditionally attempts to require() optional
        # native accelerators (bufferutil, utf-8-validate) absent from the
        # real closure -- so a same-UID racer recreating "node_modules"
        # from scratch at the OLD path, after the move, plants something
        # that would actually load at runtime. Pre-fix (commit
        # 36a4008c08), verified via a standalone scratch script: nothing
        # in `_install_locked_within_release_dir()` or tree_digest() ever
        # looked at this path again once it reappeared post-move, and
        # install() completed successfully with a clean receipt. Post-fix,
        # tree_digest()'s own deny-unknown check catches it (this planted
        # file has no pinned entry and is not one of
        # allowed_unpinned_release_files()'s exemptions).
        self._round38_full_install_stray_harness(
            extra_registry_lock_path=None,
            plant_relative_path="node_modules/bufferutil/index.js",
            expected_message_fragment="node_modules/bufferutil/index.js",
        )

    def test_install_locked_detects_tampered_upstream_lock_after_download(
        self,
    ) -> None:
        # Round 38, P2-1 regression: safe_download() verifies the
        # downloaded upstream-package-lock.json bytes IN MEMORY, then
        # writes them to disk -- before this round, the very next read of
        # that same file was a bare `.read_bytes()` with no digest
        # re-check at all, mirroring the exact TOCTOU gap round 13/14
        # already closed for the Node.js toolchain tarball and the four
        # locally patched packages' own source tarballs. This test's
        # `fake_safe_download` writes DIFFERENT bytes than
        # `installer.LOCK_SHA256` (left at its real, pinned value,
        # unlike every OTHER full-install harness in this file, which
        # patches `LOCK_SHA256` to match its own stub payload) --
        # modeling a same-UID racer who swaps the file's on-disk content
        # in the window between safe_download() writing it and this
        # function reading it back, whatever the origin of the mismatch.
        #
        # Verified to FAIL against pre-fix HEAD (commit 36a4008c08) via a
        # standalone scratch script: the bare `.read_bytes()` this round
        # replaced had no digest comparison at all, so this exact
        # mismatch went completely unnoticed at this point -- the
        # (structurally valid but digest-mismatched) lock content was
        # used to build `dependencies` further down, and only a
        # coincidental downstream failure (or none at all, for lock
        # content that still produces a valid closure) would have
        # surfaced anything wrong.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)

            def fake_safe_download(url, destination, expected_sha256, *, max_bytes=None):
                if destination.name == "upstream-package-lock.json":
                    # Deliberately NOT matching the real, un-patched
                    # installer.LOCK_SHA256 -- modeling the tampered-content
                    # scenario this fix closes.
                    payload = installer.canonical_json(
                        {"lockfileVersion": 3, "packages": {}, "tampered": True}
                    )
                else:
                    payload = b"stub-download"
                installer.atomic_create_private_file(destination, payload, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                enter(
                    mock.patch.object(
                        installer, "safe_download", side_effect=fake_safe_download
                    )
                )

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "upstream package lock changed on disk before use",
                ):
                    installer.install()

            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )

    def test_install_locked_refuses_when_release_dir_present_after_quarantine_attempt(
        self,
    ) -> None:
        # Round 38, P2-2 regression: STATE_DIR, PROBE_HOME, and the
        # session directory each already have a "still present after the
        # quarantine attempt above -> refuse" gate in `_install_locked()`
        # -- RELEASE_DIR was the one managed root missing it.
        # ensure_private_dir(RELEASE_DIR) (unlike
        # create_fresh_private_dir(), used for other release-scoped
        # extraction targets) ACCEPTS a pre-existing same-UID 0700
        # directory rather than refusing one outright -- exactly right for
        # TOOL_ROOT/the npm cache/scratch dirs it is also used for, but
        # silently wrong for RELEASE_DIR if quarantine_partial_release()
        # above did not (or could not) actually relocate it.
        #
        # This test models exactly that: RELEASE_DIR already exists (with
        # a leftover, unrelated file inside) at the point _install_locked()
        # is reached, while STATE_DIR/PROBE_HOME/the session directory do
        # NOT exist and RECEIPT_PATH/PENDING_PATH/bin_link/STATE_LINK are
        # all absent too -- so quarantine_partial_release()'s own
        # `if not present: return {"ok": True, "state": "none"}` early-out
        # fires for every OTHER managed root, but RELEASE_DIR alone was
        # never in `present` either, because the outer `any(... for path
        # in (RELEASE_DIR, STATE_DIR, PROBE_HOME, managed_session_dir()))`
        # guard IS true (RELEASE_DIR exists) -- so
        # quarantine_partial_release() DOES run and, since only RELEASE_DIR
        # is actually present among the four, quarantines it successfully.
        # To reach the specific gap this round closes (RELEASE_DIR
        # SURVIVING the quarantine attempt), this test instead makes
        # quarantine_partial_release() itself unable to relocate it, by
        # mocking it to a no-op stand-in -- modeling either a same-UID
        # racer recreating RELEASE_DIR in the narrow window between a real
        # quarantine returning and this gate's check, or a partially
        # failed quarantine this file's own rollback path could not fully
        # undo.
        #
        # Verified to FAIL against pre-fix HEAD (commit 36a4008c08): with
        # the same mock in place, `ensure_private_dir(RELEASE_DIR)` a few
        # lines later silently ACCEPTED the pre-existing directory (a real,
        # same-UID, 0700 directory -- exactly what that function's own
        # accept-existing branch requires) instead of refusing it, and the
        # install proceeded to build a fresh release INSIDE the leftover
        # directory rather than raising.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tool_root = root / "tool"
            release = tool_root / "releases" / f"v{installer.VERSION}"
            user_home = root / "user"
            user_home.mkdir(mode=0o700)
            release.mkdir(parents=True, mode=0o700)
            for ancestor in (tool_root, tool_root / "releases", release):
                os.chmod(ancestor, 0o700)
            leftover = release / "leftover-from-a-prior-attempt.txt"
            leftover.write_bytes(b"leftover\n")
            os.chmod(leftover, 0o600)

            fake_evidence = {
                "volume_uuid": "TEST-UUID",
                "node_version": installer.NODE_VERSION,
                "npm_version": installer.NPM_VERSION,
                "orca_support": {"test": "support"},
            }

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(mock.patch.object(installer, "SSD_ROOT", root))
                enter(mock.patch.object(installer, "TOOL_ROOT", tool_root))
                enter(mock.patch.object(installer, "RELEASE_DIR", release))
                enter(mock.patch.object(installer, "STATE_DIR", tool_root / "state"))
                enter(
                    mock.patch.object(installer, "PROBE_HOME", tool_root / "probe-home")
                )
                enter(mock.patch.object(installer, "USER_HOME", user_home))
                enter(mock.patch.object(installer, "STATE_LINK", user_home / ".prime"))
                enter(
                    mock.patch.object(
                        installer, "BIN_LINK", user_home / ".local/bin/prime-agent"
                    )
                )
                enter(
                    mock.patch.object(
                        installer,
                        "RECEIPT_PATH",
                        tool_root / "receipts" / f"v{installer.VERSION}.json",
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "PENDING_PATH", tool_root / "pending-install.json"
                    )
                )
                enter(
                    mock.patch.object(installer, "volume_uuid", return_value="TEST-UUID")
                )
                enter(
                    mock.patch.object(
                        installer,
                        "verify_orca_support",
                        return_value={"test": "support"},
                    )
                )
                enter(
                    mock.patch.object(
                        installer, "prime_agent_command_candidates", return_value=[]
                    )
                )
                enter(
                    mock.patch.object(installer, "preflight", return_value=fake_evidence)
                )
                # Model a quarantine attempt that runs (since RELEASE_DIR
                # is present) but fails to actually relocate RELEASE_DIR --
                # the specific residual this round's gate closes.
                enter(
                    mock.patch.object(
                        installer,
                        "quarantine_partial_release",
                        return_value={"ok": True, "state": "quarantined", "items": []},
                    )
                )

                with self.assertRaisesRegex(
                    installer.PrimeInstallError,
                    "managed Prime Agent release already exists; refusing to reuse it",
                ):
                    installer.install()

            # Nothing was mutated inside the leftover release directory.
            self.assertTrue(leftover.exists())
            self.assertEqual(leftover.read_bytes(), b"leftover\n")
            self.assertFalse((tool_root / "pending-install.json").exists())
            self.assertFalse(
                (tool_root / "receipts" / f"v{installer.VERSION}.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
