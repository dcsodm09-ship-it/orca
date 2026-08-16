from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "orca-context-bridge" / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "agent_capacity.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import agent_capacity


class CapacityRecommendationTests(unittest.TestCase):
    def test_red_gate_when_load_exceeds_cpu_capacity(self) -> None:
        result = agent_capacity.capacity_recommendation(
            8,
            8.01,
            50,
            include_orca=True,
            orca_available=True,
            active_agents=0,
        )
        self.assertEqual(result["gate"], "red")
        self.assertEqual(result["new_workers_default"], 0)
        self.assertTrue(result["coordinator_only"])

    def test_red_gate_when_free_memory_is_critical(self) -> None:
        result = agent_capacity.capacity_recommendation(
            8,
            1,
            14.9,
            include_orca=True,
            orca_available=True,
            active_agents=0,
        )
        self.assertEqual(result["gate"], "red")
        self.assertEqual(result["new_workers_max"], 0)

    def test_yellow_gate_limits_to_one_worker(self) -> None:
        result = agent_capacity.capacity_recommendation(
            8,
            6,
            40,
            include_orca=True,
            orca_available=True,
            active_agents=0,
        )
        self.assertEqual(result["gate"], "yellow")
        self.assertEqual(result["new_workers_default"], 1)
        self.assertEqual(result["new_workers_max"], 1)

    def test_green_gate_defaults_to_two_workers(self) -> None:
        result = agent_capacity.capacity_recommendation(
            8,
            2,
            40,
            include_orca=True,
            orca_available=True,
            active_agents=0,
        )
        self.assertEqual(result["gate"], "green")
        self.assertEqual(result["new_workers_default"], 2)
        self.assertEqual(result["new_workers_max"], 3)

    def test_missing_signals_keep_conservative_yellow_budget(self) -> None:
        result = agent_capacity.capacity_recommendation(
            None,
            None,
            None,
            include_orca=True,
            orca_available=True,
            active_agents=0,
        )
        self.assertEqual(result["gate"], "yellow")
        self.assertEqual(result["new_workers_default"], 1)

    def test_missing_orca_activity_stays_yellow_even_on_a_quiet_host(self) -> None:
        result = agent_capacity.capacity_recommendation(
            8,
            2,
            40,
            include_orca=True,
            orca_available=False,
            active_agents=None,
        )
        self.assertEqual(result["gate"], "yellow")
        self.assertEqual(result["new_workers_max"], 1)


class OrcaInvocationTests(unittest.TestCase):
    def test_sanitized_cli_environment_enables_electron_node_mode(self) -> None:
        self.assertEqual(agent_capacity.SUBPROCESS_ENV["ELECTRON_RUN_AS_NODE"], "1")
        result = agent_capacity._run_bounded(
            ["/usr/bin/env"],
            timeout=agent_capacity.COMMAND_TIMEOUT_SECONDS,
            output_limit=agent_capacity.DEFAULT_OUTPUT_LIMIT,
        )
        self.assertIsNone(result.error)
        self.assertIsNotNone(result.stdout)
        self.assertIn(b"ELECTRON_RUN_AS_NODE=1", result.stdout.splitlines())


class TrustedPathTests(unittest.TestCase):
    def test_descriptor_close_failure_fails_closed(self) -> None:
        with mock.patch.object(agent_capacity.os, "close", side_effect=OSError("close")):
            self.assertIsNone(
                agent_capacity._open_trusted_path(Path("/"), directory_leaf=True)
            )

    def test_control_exception_is_not_suppressed(self) -> None:
        with mock.patch.object(
            agent_capacity.os, "open", side_effect=KeyboardInterrupt()
        ):
            with self.assertRaises(KeyboardInterrupt):
                agent_capacity._open_trusted_path(Path("/Applications"))


class OrcaSummaryTests(unittest.TestCase):
    def test_summarize_orca_worktrees_only_counts_working_rows(self) -> None:
        result = agent_capacity.summarize_orca_worktrees(
            {
                "ok": True,
                "result": {
                    "worktrees": [
                        {
                            "status": "working",
                            "agents": [{"state": "working"}, {"state": "working"}],
                        },
                        {"status": "inactive", "agents": [{"state": "idle"}]},
                        {"status": "working", "agents": []},
                    ],
                    "totalCount": 3,
                    "truncated": False,
                }
            }
        )
        self.assertEqual(
            result,
            {"worktrees": 3, "working_worktrees": 2, "reported_agents": 2},
        )

    def test_summarize_orca_worktrees_rejects_unexpected_shape(self) -> None:
        self.assertIsNone(agent_capacity.summarize_orca_worktrees({"result": {}}))


class CapacityGateOverrideFlagTests(unittest.TestCase):
    """Regression coverage for the explicit, single-invocation override flag added
    2026-08-16 at the user's explicit, repeated request to stop the gate from
    blocking dispatch. The two properties that matter are: (1) without the flag,
    behavior is byte-for-byte unchanged from before the flag existed, so every other
    caller of this same canonical script path is unaffected; (2) with the flag, the
    true measured state is still fully present in the output, just not acted on.
    """

    def _run(self, *extra_args: str) -> dict:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *extra_args],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(completed.stdout)

    def test_default_invocation_has_no_override_fields(self) -> None:
        snapshot = self._run()
        self.assertNotIn("true_recommendation_before_override", snapshot)
        self.assertIn(snapshot["recommendation"]["gate"], {"red", "yellow", "green"})

    def test_override_flag_preserves_true_state_and_forces_one_worker(self) -> None:
        snapshot = self._run(
            "--i-am-explicitly-overriding-the-capacity-gate-this-run-only"
        )
        self.assertIn("true_recommendation_before_override", snapshot)
        true_gate = snapshot["true_recommendation_before_override"]["gate"]
        self.assertIn(true_gate, {"red", "yellow", "green"})
        forced = snapshot["recommendation"]
        self.assertEqual(forced["gate"], "override_forced_by_caller")
        self.assertEqual(forced["new_workers_max"], 1)
        self.assertEqual(forced["new_workers_default"], 1)
        self.assertIn(true_gate, forced["reason"][0])

    def test_override_flag_emits_a_stderr_warning(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--i-am-explicitly-overriding-the-capacity-gate-this-run-only",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        self.assertIn("WARNING", completed.stderr)
        self.assertIn("forced open", completed.stderr)


if __name__ == "__main__":
    unittest.main()
