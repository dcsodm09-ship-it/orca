from __future__ import annotations

import contextlib
import io
import json
import os
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


class PrimeAgentInstallerTests(unittest.TestCase):
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
        for index, (name, asset_name) in enumerate(local_assets.items()):
            (assets / asset_name).write_bytes(name.encode("utf-8"))
            packages.setdefault(
                f"node_modules/local-{index}",
                {
                    "name": name,
                    "version": installer.VERSION,
                    "resolved": f"file:assets/{asset_name}",
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
                return installer.validate_generated_lock(raw, generated)

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
                installer.validate_generated_lock(raw, generated)

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
            for asset_name in (installer.MAIN_PATCHED_ASSET, *installer.WORKSPACE_ASSETS.values()):
                (assets / asset_name).write_text("asset", encoding="utf-8")
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
                        installer.validate_generated_lock(raw, generated)

    def test_safe_extract_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            archive_path = root / "bad.tgz"
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("package/../../escape")
                payload = b"bad"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            destination = root / "out"
            destination.mkdir()
            with self.assertRaises(installer.PrimeInstallError):
                installer.safe_extract_main_asset(archive_path, destination)
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
        ).decode("utf-8")
        self.assertIn("fcntl.LOCK_SH", guard)
        self.assertIn(
            'RESOURCE_GUARDS = ("--no-extensions", "--no-skills", "--no-prompt-templates")',
            guard,
        )
        self.assertIn("def guarded_arguments", guard)
        self.assertIn("[NODE, CLI, *guarded_arguments", guard)

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
                guard.write_bytes(installer.managed_launch_guard_script(node, cli))
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
            ready = root / "ready"
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
                    installer.managed_launch_guard_script(fake_node, ready)
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
                    if ready.exists():
                        break
                    child.poll()
                    if child.returncode is not None:
                        self.fail("generated launch guard exited before readiness")
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                with (
                    mock.patch.object(installer, "SSD_ROOT", root),
                    mock.patch.object(installer, "TOOL_ROOT", tool_root),
                ):
                    with self.assertRaisesRegex(installer.PrimeInstallError, "busy"):
                        with installer.exclusive_lifecycle_lock(create=False):
                            self.fail("exclusive lock must not overlap launch guard")
            finally:
                child.wait(timeout=5)

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

            with mock.patch.object(installer, "SSD_ROOT", ssd):
                with self.assertRaises(installer.PrimeInstallError):
                    installer.extract_node_toolchain(node_asset, destination)

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
                    installer.safe_extract_main_asset(archive_path, destination)
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
                    installer.extract_node_toolchain(node_asset, destination)
            self.assertTrue(planted["done"])

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

            destination = root / "out"
            destination.mkdir(mode=0o700)
            untracked_dir = destination / "package"
            untracked_dir.mkdir(mode=0o700)
            (untracked_dir / "evil.js").write_bytes(b"attacker payload")

            package_dir, manifest, digests, dir_modes = installer.safe_extract_main_asset(
                archive_path, destination
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

            real_safe_extract_main_asset = installer.safe_extract_main_asset

            def race_after_extraction(asset: Path, destination: Path):
                package_dir, manifest, digests, dir_modes = real_safe_extract_main_asset(
                    asset, destination
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
                patched, _digest, _manifest = installer.make_patched_asset(
                    original_asset,
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

            real_safe_extract_main_asset = installer.safe_extract_main_asset

            def swap_after_extraction(asset: Path, destination: Path):
                package_dir, manifest, digests, dir_modes = real_safe_extract_main_asset(
                    asset, destination
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

            outside_target = root / "outside-victim"
            outside_target.mkdir(mode=0o700)
            (outside_target / "sentinel").write_bytes(b"do-not-touch")

            real_safe_extract_main_asset = installer.safe_extract_main_asset
            package_dir_holder: dict[str, Path] = {}

            def learn_package_dir(asset: Path, destination: Path):
                result = real_safe_extract_main_asset(asset, destination)
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
                patched, _digest, _manifest = installer.make_patched_asset(
                    original_asset,
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
                guard.write_bytes(installer.managed_launch_guard_script(node, cli))
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
                guard.write_bytes(installer.managed_launch_guard_script(node, cli))
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
                guard.write_bytes(installer.managed_launch_guard_script(node, cli))
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
                guard.write_bytes(installer.managed_launch_guard_script(node, cli))
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
            # package/help<arg> must still succeed, AND -- the round-3/4
            # argv-corruption bug class this fix must not reopen -- their
            # argv must arrive at the underlying CLI COMPLETELY UNCHANGED:
            # confirms config/package/help remain in RUNTIME_NO_GUARD_
            # COMMANDS on the Python launch-guard side even though they
            # left public_commands on the shell side, so guarded_arguments()
            # never splices RESOURCE_GUARDS into their argv (a position
            # never verified safe for these three specific commands).
            for arguments in (
                ("config", "get", "x"),
                ("package", "list"),
                ("help", "auth"),
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
                npm_path, node_path, args, cwd, cache, install_home, install_tmp, timeout=300
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
                upstream_lock,
                assets_dir,
                *,
                expected_name,
                managed_name,
                output_name,
            ):
                patched = assets_dir / output_name
                installer.atomic_create_private_file(patched, b"stub-asset", 0o600)
                return (
                    patched,
                    installer.sha256_bytes(b"stub-asset"),
                    {"name": managed_name, "version": installer.VERSION},
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
            fake_node = release / "toolchain/bin/node"
            fake_npm_cli = release / "toolchain/lib/node_modules/npm/bin/npm-cli.js"

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
                        installer,
                        "extract_node_toolchain",
                        return_value=(fake_node, fake_npm_cli),
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


if __name__ == "__main__":
    unittest.main()
