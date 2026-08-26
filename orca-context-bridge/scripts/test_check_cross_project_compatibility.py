#!/usr/bin/env python3
"""Unit tests for check_cross_project_compatibility.py (M8 Gate A Tier-1).

Run with:
    python3 -m unittest test_check_cross_project_compatibility.py -v
(from this directory), or plain
`python3 test_check_cross_project_compatibility.py`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_cross_project_compatibility as ccc  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def make_reverse_row(ref_key="p:script:x", in_degree=0, referencing_project_ids=None, referenced_by=None) -> dict:
    return {
        "ref_key": ref_key,
        "in_degree": in_degree,
        "referencing_project_ids": referencing_project_ids or [],
        "referenced_by": referenced_by if referenced_by is not None else [],
    }


def make_edge(project_id="q", capability_global_id="q#dep", scope="cross-project", raw="p:script:x") -> dict:
    return {"project_id": project_id, "capability_global_id": capability_global_id, "scope": scope, "raw": raw}


def make_capability(project_id: str, cid: str, kind="script", name="thing", depends_on=None) -> dict:
    return {
        "project_id": project_id,
        "id": cid,
        "kind": kind,
        "name": name,
        "global_id": f"{project_id}#{cid}",
        "depends_on": depends_on or [],
    }


def make_dep(raw, state, target_global_id=None, scope="cross-project") -> dict:
    entry = {"raw": raw, "scope": scope, "ref_key": None, "state": state}
    if target_global_id is not None:
        entry["target_global_id"] = target_global_id
    return entry


def make_catalog(
    capabilities=None, reverse_index=None, verified_at="2026-08-23T00:00:00Z", projects=None, wiki_pages=None
) -> dict:
    catalog: dict = {
        "schema_version": 1,
        "verified_at": verified_at,
        "capabilities": capabilities if capabilities is not None else [],
    }
    if reverse_index is not None:
        catalog["capability_reverse_index"] = reverse_index
    if projects is not None:
        catalog["projects"] = projects
    if wiki_pages is not None:
        catalog["wiki_pages"] = wiki_pages
    return catalog


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ccc.main(argv)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Gate D ("run" subcommand) fixture helpers
# ---------------------------------------------------------------------------


def fresh_iso(now: "datetime | None" = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_project_row(project_id: str, real_path, status: str = "ok") -> dict:
    return {"project_id": project_id, "real_path": str(real_path), "status": status}


def make_affected_catalog(
    target_global_id: str,
    dependent_project_id: str,
    *,
    projects=None,
    verified_at: "str | None" = None,
    extra_reverse_index: "dict | None" = None,
) -> dict:
    """A catalog where `dependent_project_id` is the sole project whose
    capability_reverse_index entry resolves `target_global_id` -- i.e. the
    minimal fixture for "--global-id target_global_id --authorize-project
    dependent_project_id" to pass the affected-subset check."""
    row = make_reverse_row(
        in_degree=1,
        referencing_project_ids=[dependent_project_id],
        referenced_by=[make_edge(project_id=dependent_project_id, capability_global_id=f"{dependent_project_id}#dep")],
    )
    reverse_index = {target_global_id: row}
    if extra_reverse_index:
        reverse_index.update(extra_reverse_index)
    return make_catalog(
        reverse_index=reverse_index,
        projects=projects,
        verified_at=verified_at if verified_at is not None else fresh_iso(),
    )


def make_project_dir(root: Path, name: str) -> Path:
    project_dir = root / name
    (project_dir / "wiki").mkdir(parents=True, exist_ok=True)
    return project_dir


def write_compat_check(project_dir: Path, checks: list) -> None:
    _write_json(project_dir / "wiki" / "compat-check.json", {"schema_version": 1, "checks": checks})


def make_check_entry(
    id="chk",  # noqa: A002
    depends_on_ref="p#x",
    check_command=None,
    cwd=".",
    timeout_seconds=30,
    reviewed_by="alice",
    reviewed_at="2026-08-23T00:00:00Z",
) -> dict:
    return {
        "id": id,
        "depends_on_ref": depends_on_ref,
        "check_command": check_command if check_command is not None else [sys.executable, "-c", "pass"],
        "cwd": cwd,
        "timeout_seconds": timeout_seconds,
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
    }


def _run_compat(**kwargs) -> dict:
    """Thin wrapper over ccc.run_compatibility_checks with sane defaults for
    every keyword the pure function requires, so each test only overrides
    what it actually cares about."""
    defaults = dict(
        now=datetime.now(timezone.utc),
        stale_after_seconds=ccc.DEFAULT_STALE_AFTER_HOURS * 3600,
        allow_stale_catalog=False,
        timeout_ceiling_seconds=ccc.DEFAULT_TIMEOUT_CEILING_SECONDS,
    )
    defaults.update(kwargs)
    return ccc.run_compatibility_checks(**defaults)


# ---------------------------------------------------------------------------
# --changed-global-id shape validation (usage error vs "confirmed none")
# ---------------------------------------------------------------------------


class GlobalIdShapeTests(unittest.TestCase):
    def test_valid_shapes(self) -> None:
        self.assertTrue(ccc.global_id_shape_is_valid("proj#cap"))
        self.assertTrue(ccc.global_id_shape_is_valid("a/b#c:d"))

    def test_invalid_shapes(self) -> None:
        self.assertFalse(ccc.global_id_shape_is_valid(""))
        self.assertFalse(ccc.global_id_shape_is_valid("no-hash-here"))
        self.assertFalse(ccc.global_id_shape_is_valid("has\nnewline#x"))
        self.assertFalse(ccc.global_id_shape_is_valid("has\x00nul#x"))


# ---------------------------------------------------------------------------
# reverse_index mode: the primary path
# ---------------------------------------------------------------------------


class ReverseIndexModeTests(unittest.TestCase):
    def test_found_downstream_returns_exit_0(self) -> None:
        edge = make_edge()
        catalog = make_catalog(reverse_index={"p#x": make_reverse_row(in_degree=1, referenced_by=[edge])})
        result = ccc.check_affected(catalog, "p#x")
        self.assertEqual(result["mode"], "reverse_index")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["affected"], [{"global_id": "q#dep", "project_id": "q", "scope": "cross-project", "raw": "p:script:x"}])
        self.assertTrue(result["global_id_known_in_catalog"])

    def test_known_with_zero_inbound_edges_returns_exit_1(self) -> None:
        catalog = make_catalog(reverse_index={"p#x": make_reverse_row(in_degree=0, referenced_by=[])})
        result = ccc.check_affected(catalog, "p#x")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["affected"], [])
        self.assertTrue(result["global_id_known_in_catalog"])

    def test_global_id_absent_everywhere_returns_exit_1_and_known_false(self) -> None:
        catalog = make_catalog(
            capabilities=[make_capability("p", "y")],
            reverse_index={"p#y": make_reverse_row()},
        )
        result = ccc.check_affected(catalog, "p#never-existed")
        self.assertEqual(result["exit_code"], 1)
        self.assertFalse(result["global_id_known_in_catalog"])

    def test_global_id_known_via_capabilities_even_with_no_reverse_row(self) -> None:
        catalog = make_catalog(
            capabilities=[make_capability("p", "x")],
            reverse_index={},  # no row at all for p#x, but it's a real capability with 0 inbound edges
        )
        result = ccc.check_affected(catalog, "p#x")
        self.assertEqual(result["exit_code"], 1)
        self.assertTrue(result["global_id_known_in_catalog"])

    def test_row_not_a_dict_treated_as_absent_with_warning(self) -> None:
        catalog = make_catalog(reverse_index={"p#x": "not-a-dict"})
        result = ccc.check_affected(catalog, "p#x")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["affected"], [])
        codes = [w["code"] for w in result["warnings"]]
        self.assertIn("reverse_index_row_not_an_object", codes)

    def test_referenced_by_not_a_list_degrades_with_warning_but_stays_reverse_index_mode(self) -> None:
        catalog = make_catalog(reverse_index={"p#x": {"ref_key": "p:script:x", "in_degree": 1, "referenced_by": "oops"}})
        result = ccc.check_affected(catalog, "p#x")
        self.assertEqual(result["mode"], "reverse_index")
        self.assertEqual(result["affected"], [])
        codes = [w["code"] for w in result["warnings"]]
        self.assertIn("reverse_index_row_referenced_by_not_a_list", codes)

    def test_referenced_by_not_a_list_with_nonzero_in_degree_is_not_confirmed_none(self) -> None:
        """Regression for the P1 found in independent review: a row that
        claims in_degree=5 but whose referenced_by cannot be parsed at all
        must NOT collapse to exit 1 ("confirmed: no downstream
        dependents") -- that would misreport "queried but unparseable" as
        "queried and confirmed empty"."""
        catalog = make_catalog(reverse_index={"p#x": {"ref_key": "p:script:x", "in_degree": 5, "referenced_by": "oops"}})
        result = ccc.check_affected(catalog, "p#x")
        self.assertEqual(result["mode"], "reverse_index")
        self.assertEqual(result["affected"], [])
        self.assertTrue(result["row_degraded"])
        self.assertEqual(result["exit_code"], 3)
        codes = [w["code"] for w in result["warnings"]]
        self.assertIn("reverse_index_row_in_degree_mismatch", codes)

    def test_referenced_by_not_a_list_with_in_degree_zero_is_not_degraded(self) -> None:
        """A row honestly declaring in_degree=0 alongside an unparseable
        referenced_by is internally CONSISTENT (0 == len([])) even though
        the shape is odd -- this is not the mismatch case and stays a
        normal confirmed-none (exit 1)."""
        catalog = make_catalog(reverse_index={"p#x": {"ref_key": "p:script:x", "in_degree": 0, "referenced_by": "oops"}})
        result = ccc.check_affected(catalog, "p#x")
        self.assertFalse(result["row_degraded"])
        self.assertEqual(result["exit_code"], 1)

    def test_in_degree_as_bool_is_not_treated_as_a_declared_count(self) -> None:
        """isinstance(True, int) is True in Python; in_degree: true must
        not be silently read as the integer 1."""
        catalog = make_catalog(reverse_index={"p#x": {"ref_key": "p:script:x", "in_degree": True, "referenced_by": []}})
        result = ccc.check_affected(catalog, "p#x")
        self.assertFalse(result["row_degraded"])
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["in_degree"], 0)

    def test_malformed_referenced_by_entries_are_skipped_and_counted(self) -> None:
        good = make_edge(capability_global_id="q#good")
        bad = {"project_id": "q"}  # missing capability_global_id
        catalog = make_catalog(reverse_index={"p#x": make_reverse_row(referenced_by=[good, bad])})
        result = ccc.check_affected(catalog, "p#x")
        self.assertEqual([a["global_id"] for a in result["affected"]], ["q#good"])
        codes = [w["code"] for w in result["warnings"]]
        self.assertIn("reverse_index_row_skipped_malformed_entries", codes)

    def test_skipped_entries_causing_in_degree_mismatch_also_degrade(self) -> None:
        """The declared in_degree=1 promised one resolvable entry; the sole
        entry present is malformed and gets skipped, leaving affected=[].
        1 != 0 is the same mismatch as the whole-field case above, via a
        different route (partial skip, not total unparseability)."""
        bad = {"project_id": "q"}  # missing capability_global_id
        catalog = make_catalog(reverse_index={"p#x": make_reverse_row(in_degree=1, referenced_by=[bad])})
        result = ccc.check_affected(catalog, "p#x")
        self.assertTrue(result["row_degraded"])
        self.assertEqual(result["exit_code"], 3)

    def test_row_degraded_key_present_and_false_in_ordinary_results(self) -> None:
        catalog = make_catalog(reverse_index={"p#x": make_reverse_row(in_degree=1, referenced_by=[make_edge()])})
        result = ccc.check_affected(catalog, "p#x")
        self.assertIn("row_degraded", result)
        self.assertFalse(result["row_degraded"])


# ---------------------------------------------------------------------------
# fallback_scan mode: capability_reverse_index missing / wrong type
# ---------------------------------------------------------------------------


class FallbackScanModeTests(unittest.TestCase):
    def test_missing_reverse_index_falls_back_and_finds_resolved_edge(self) -> None:
        dependent = make_capability(
            "q", "dep", depends_on=[make_dep("p:script:x", state="resolved", target_global_id="p#x")]
        )
        catalog = make_catalog(capabilities=[make_capability("p", "x"), dependent])
        result = ccc.check_affected(catalog, "p#x")
        self.assertEqual(result["mode"], "fallback_scan")
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual([a["global_id"] for a in result["affected"]], ["q#dep"])

    def test_wrong_type_reverse_index_also_falls_back(self) -> None:
        catalog = make_catalog(capabilities=[make_capability("p", "x")], reverse_index=["not", "a", "dict"])
        result = ccc.check_affected(catalog, "p#x")
        self.assertEqual(result["mode"], "fallback_scan")
        self.assertEqual(result["exit_code"], 3)

    def test_fallback_scan_ignores_unresolved_and_self_reference_states(self) -> None:
        dependent = make_capability(
            "q", "dep",
            depends_on=[
                make_dep("p:script:x", state="unresolved"),
                make_dep("p:script:x", state="self-reference", target_global_id="q#dep"),
            ],
        )
        catalog = make_catalog(capabilities=[dependent])
        result = ccc.check_affected(catalog, "p#x")
        self.assertEqual(result["mode"], "fallback_scan")
        self.assertEqual(result["affected"], [])

    def test_fallback_scan_exit_code_is_always_3_even_with_zero_results(self) -> None:
        catalog = make_catalog(capabilities=[make_capability("p", "x")])
        result = ccc.check_affected(catalog, "p#x")
        self.assertEqual(result["mode"], "fallback_scan")
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["affected"], [])

    def test_fallback_scan_row_degraded_is_false_not_absent(self) -> None:
        """fallback_scan's own exit-3 reason (whole index unusable) is
        distinct from reverse_index mode's row_degraded (one row
        inconsistent) -- the key must still be present so callers can rely
        on its shape regardless of mode."""
        catalog = make_catalog(capabilities=[make_capability("p", "x")])
        result = ccc.check_affected(catalog, "p#x")
        self.assertIn("row_degraded", result)
        self.assertFalse(result["row_degraded"])

    def test_both_reverse_index_and_capabilities_unusable_raises_fatal(self) -> None:
        catalog = make_catalog(capabilities="not-a-list")
        with self.assertRaises(ccc.CheckFatal) as ctx:
            ccc.check_affected(catalog, "p#x")
        self.assertEqual(ctx.exception.reason, "reverse_index_and_capabilities_both_unusable")


# ---------------------------------------------------------------------------
# CLI exit codes: 0 / 1 / 2 / 3 / 4
# ---------------------------------------------------------------------------


class CliExitCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-cli-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _catalog_path(self, catalog: dict) -> Path:
        path = self.tmp / "catalog.json"
        _write_json(path, catalog)
        return path

    def test_exit_0_found(self) -> None:
        catalog = make_catalog(reverse_index={"p#x": make_reverse_row(in_degree=1, referenced_by=[make_edge()])})
        path = self._catalog_path(catalog)
        code, out, err = _run_main(["affected", "--changed-global-id", "p#x", "--catalog", str(path), "--json", "--quiet"])
        self.assertEqual(code, 0)

    def test_exit_1_confirmed_none(self) -> None:
        catalog = make_catalog(reverse_index={"p#x": make_reverse_row()})
        path = self._catalog_path(catalog)
        code, out, err = _run_main(["affected", "--changed-global-id", "p#x", "--catalog", str(path), "--quiet"])
        self.assertEqual(code, 1)

    def test_exit_2_malformed_global_id(self) -> None:
        catalog = make_catalog()
        path = self._catalog_path(catalog)
        code, out, err = _run_main(["affected", "--changed-global-id", "no-hash", "--catalog", str(path), "--quiet"])
        self.assertEqual(code, 2)

    def test_exit_2_empty_catalog_path(self) -> None:
        code, out, err = _run_main(["affected", "--changed-global-id", "p#x", "--catalog", "", "--quiet"])
        self.assertEqual(code, 2)

    def test_exit_3_fallback_scan(self) -> None:
        catalog = make_catalog(capabilities=[make_capability("p", "x")])
        path = self._catalog_path(catalog)
        code, out, err = _run_main(["affected", "--changed-global-id", "p#x", "--catalog", str(path), "--quiet"])
        self.assertEqual(code, 3)

    def test_exit_3_reverse_index_row_degraded_via_cli(self) -> None:
        """End-to-end CLI regression for the P1 fix: a reverse_index row
        with in_degree=5 but an unparseable referenced_by must exit 3, not
        1, through the full argparse -> check_affected -> exit path (not
        just the internal function)."""
        catalog = make_catalog(
            reverse_index={"p#x": {"ref_key": "p:script:x", "in_degree": 5, "referenced_by": "oops"}}
        )
        path = self._catalog_path(catalog)
        code, out, err = _run_main(["affected", "--changed-global-id", "p#x", "--catalog", str(path), "--json", "--quiet"])
        self.assertEqual(code, 3)

    def test_exit_3_reverse_index_row_degraded_human_output_labels_it(self) -> None:
        catalog = make_catalog(
            reverse_index={"p#x": {"ref_key": "p:script:x", "in_degree": 5, "referenced_by": "oops"}}
        )
        path = self._catalog_path(catalog)
        code, out, err = _run_main(["affected", "--changed-global-id", "p#x", "--catalog", str(path)])
        self.assertEqual(code, 3)
        self.assertIn("PARTIAL TRUST", out)

    def test_exit_4_catalog_missing(self) -> None:
        missing = self.tmp / "no-such-catalog.json"
        code, out, err = _run_main(["affected", "--changed-global-id", "p#x", "--catalog", str(missing), "--quiet"])
        self.assertEqual(code, 4)

    def test_exit_4_catalog_unparseable(self) -> None:
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code, out, err = _run_main(["affected", "--changed-global-id", "p#x", "--catalog", str(bad), "--quiet"])
        self.assertEqual(code, 4)

    def test_exit_4_catalog_not_an_object(self) -> None:
        path = self.tmp / "array.json"
        path.write_text("[]", encoding="utf-8")
        code, out, err = _run_main(["affected", "--changed-global-id", "p#x", "--catalog", str(path), "--quiet"])
        self.assertEqual(code, 4)

    def test_exit_4_reverse_index_and_capabilities_both_unusable(self) -> None:
        path = self.tmp / "empty.json"
        path.write_text("{}", encoding="utf-8")
        code, out, err = _run_main(["affected", "--changed-global-id", "p#x", "--catalog", str(path), "--quiet"])
        self.assertEqual(code, 4)

    def test_exit_4_too_large_catalog(self) -> None:
        original_max = ccc.MAX_CATALOG_BYTES
        ccc.MAX_CATALOG_BYTES = 10
        try:
            path = self._catalog_path(make_catalog())
            code, out, err = _run_main(["affected", "--changed-global-id", "p#x", "--catalog", str(path), "--quiet"])
            self.assertEqual(code, 4)
        finally:
            ccc.MAX_CATALOG_BYTES = original_max

    def test_quiet_suppresses_all_output(self) -> None:
        catalog = make_catalog(reverse_index={"p#x": make_reverse_row(in_degree=1, referenced_by=[make_edge()])})
        path = self._catalog_path(catalog)
        code, out, err = _run_main(["affected", "--changed-global-id", "p#x", "--catalog", str(path), "--json", "--quiet"])
        self.assertEqual((out, err), ("", ""))

    def test_json_output_ok_true_even_on_confirmed_none(self) -> None:
        catalog = make_catalog(reverse_index={"p#x": make_reverse_row()})
        path = self._catalog_path(catalog)
        code, out, err = _run_main(["affected", "--changed-global-id", "p#x", "--catalog", str(path), "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])


# ---------------------------------------------------------------------------
# No write path at all -- OS-level proof, not a mock
# ---------------------------------------------------------------------------


class NoWritePathIsolationTests(unittest.TestCase):
    """check_cross_project_compatibility.py has no output file and no
    write() call of any kind (see module docstring). Proven here by making
    the catalog's ENTIRE containing directory tree read-only (real OS
    permissions) and confirming the tool still answers correctly with zero
    PermissionErrors and zero new filesystem entries anywhere."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-nowrite-"))
        catalog = make_catalog(reverse_index={"p#x": make_reverse_row(in_degree=1, referenced_by=[make_edge()])})
        self.catalog_path = self.tmp / "catalog.json"
        _write_json(self.catalog_path, catalog)

        for dirpath, dirnames, filenames in os.walk(str(self.tmp), topdown=False):
            for name in filenames:
                os.chmod(os.path.join(dirpath, name), 0o444)
            os.chmod(dirpath, 0o555)
        self.snapshot_before = self._snapshot()

    def tearDown(self) -> None:
        for dirpath, dirnames, filenames in os.walk(str(self.tmp), topdown=True):
            os.chmod(dirpath, 0o755)
            for name in filenames:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o644)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _snapshot(self) -> dict:
        out = {}
        for dirpath, dirnames, filenames in os.walk(str(self.tmp), followlinks=False):
            for name in list(dirnames) + list(filenames):
                full = os.path.join(dirpath, name)
                st = os.lstat(full)
                out[full] = (st.st_mode, st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
        return out

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "permission bits meaningless as root / non-posix")
    def test_negative_control_readonly_dir_rejects_os_write(self) -> None:
        """Mandatory control: if this fails, the assertion below would pass
        vacuously."""
        with self.assertRaises(PermissionError):
            (self.tmp / "new-file.json").write_text("{}", encoding="utf-8")

    def test_run_against_fully_readonly_tree_raises_nothing_and_writes_nothing(self) -> None:
        code, out, err = _run_main(
            ["affected", "--changed-global-id", "p#x", "--catalog", str(self.catalog_path), "--json", "--quiet"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot_before, self._snapshot())


# ===========================================================================
# M8 Gate D: "run" subcommand tests (Tier-2, actually-executes)
# ===========================================================================


def _wait_until_process_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# compat-check.json schema validation
# ---------------------------------------------------------------------------


class CompatCheckSchemaValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-schema-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _validate(self, doc):
        return ccc._validate_compat_check_document(doc, self.tmp)

    def test_not_an_object(self) -> None:
        validated, reason = self._validate([])
        self.assertIsNone(validated)
        self.assertEqual(reason, "not_an_object")

    def test_unsupported_schema_version(self) -> None:
        validated, reason = self._validate({"schema_version": 2, "checks": []})
        self.assertEqual(reason, "unsupported_schema_version")

    def test_schema_version_bool_rejected(self) -> None:
        validated, reason = self._validate({"schema_version": True, "checks": []})
        self.assertEqual(reason, "unsupported_schema_version")

    def test_checks_not_a_list(self) -> None:
        validated, reason = self._validate({"schema_version": 1, "checks": "nope"})
        self.assertEqual(reason, "checks_not_a_list")

    def test_entry_not_an_object(self) -> None:
        validated, reason = self._validate({"schema_version": 1, "checks": ["nope"]})
        self.assertEqual(reason, "entry_not_an_object:0")

    def test_missing_id(self) -> None:
        entry = make_check_entry()
        del entry["id"]
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_id:0")

    def test_missing_depends_on_ref(self) -> None:
        entry = make_check_entry()
        del entry["depends_on_ref"]
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_depends_on_ref:0")

    def test_missing_check_command(self) -> None:
        entry = make_check_entry()
        del entry["check_command"]
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_check_command:0")

    def test_check_command_not_a_list_is_rejected(self) -> None:
        entry = make_check_entry(check_command="python3 x.py --quick")
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_check_command:0")

    def test_check_command_empty_list_rejected(self) -> None:
        entry = make_check_entry(check_command=[])
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_check_command:0")

    def test_check_command_contains_non_string_rejected(self) -> None:
        entry = make_check_entry(check_command=[sys.executable, 1])
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_check_command:0")

    def test_check_command_contains_empty_string_rejected(self) -> None:
        entry = make_check_entry(check_command=[sys.executable, ""])
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_check_command:0")

    def test_check_command_with_embedded_null_byte_rejected(self) -> None:
        """Regression for the P2 in independent review: a NUL byte in an
        argv element used to reach subprocess.Popen() uncaught (POSIX exec
        cannot represent NUL inside an argument string) and, worse, could
        wipe out an EARLIER, already-executed check's results in the same
        document. Rejecting it here at validation time (before ANY entry in
        the document executes -- validation is whole-document, all-or-
        nothing) removes the scenario entirely rather than only mitigating
        it after the fact."""
        entry = make_check_entry(check_command=[sys.executable, "-c", "boom\x00"])
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_check_command:0")

    def test_missing_cwd(self) -> None:
        entry = make_check_entry()
        del entry["cwd"]
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertTrue(reason.startswith("entry_bad_cwd:0"))

    def test_cwd_parent_traversal_rejected(self) -> None:
        entry = make_check_entry(cwd="../escape")
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertTrue(reason.startswith("entry_bad_cwd:0"))

    def test_missing_timeout_seconds(self) -> None:
        entry = make_check_entry()
        del entry["timeout_seconds"]
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_timeout_seconds:0")

    def test_timeout_seconds_zero_rejected(self) -> None:
        entry = make_check_entry(timeout_seconds=0)
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_timeout_seconds:0")

    def test_timeout_seconds_negative_rejected(self) -> None:
        entry = make_check_entry(timeout_seconds=-5)
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_timeout_seconds:0")

    def test_timeout_seconds_bool_rejected(self) -> None:
        entry = make_check_entry(timeout_seconds=True)
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_timeout_seconds:0")

    def test_missing_reviewed_by(self) -> None:
        entry = make_check_entry()
        del entry["reviewed_by"]
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_reviewed_by:0")

    def test_missing_reviewed_at(self) -> None:
        entry = make_check_entry()
        del entry["reviewed_at"]
        _, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertEqual(reason, "entry_bad_reviewed_at:0")

    def test_valid_document_returns_entries(self) -> None:
        entry = make_check_entry()
        validated, reason = self._validate({"schema_version": 1, "checks": [entry]})
        self.assertIsNone(reason)
        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["id"], "chk")


# ---------------------------------------------------------------------------
# cwd containment
# ---------------------------------------------------------------------------


class CwdContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-cwd-"))
        (self.tmp / "sub").mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dot_cwd_is_the_project_root_itself(self) -> None:
        resolved, reason = ccc._resolve_check_cwd(self.tmp, ".")
        self.assertIsNone(reason)
        self.assertEqual(resolved, self.tmp.resolve())

    def test_nested_relative_cwd_ok(self) -> None:
        resolved, reason = ccc._resolve_check_cwd(self.tmp, "sub")
        self.assertIsNone(reason)
        self.assertEqual(resolved, (self.tmp / "sub").resolve())

    def test_parent_traversal_rejected(self) -> None:
        resolved, reason = ccc._resolve_check_cwd(self.tmp, "../escape")
        self.assertIsNone(resolved)
        self.assertEqual(reason, "cwd_parent_segment")

    def test_absolute_cwd_rejected(self) -> None:
        resolved, reason = ccc._resolve_check_cwd(self.tmp, "/etc")
        self.assertIsNone(resolved)

    @unittest.skipIf(os.name != "posix", "symlinks assumed posix")
    def test_symlink_pointing_outside_root_rejected(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="ccc-cwd-outside-"))
        try:
            link = self.tmp / "escape-link"
            os.symlink(str(outside), str(link))
            resolved, reason = ccc._resolve_check_cwd(self.tmp, "escape-link")
            self.assertIsNone(resolved)
            self.assertEqual(reason, "cwd_symlink_component")
        finally:
            shutil.rmtree(outside, ignore_errors=True)


# ---------------------------------------------------------------------------
# shell=False enforcement + process execution primitives
# ---------------------------------------------------------------------------


class ShellFalseEnforcementTests(unittest.TestCase):
    def test_argv_element_with_shell_metacharacters_passed_through_verbatim(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ccc-run-shell-"))
        try:
            echo_script = tmp / "echo_argv.py"
            echo_script.write_text("import sys, json\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
            dangerous = "; rm -rf /tmp/x"
            outcome = ccc._run_one_check([sys.executable, str(echo_script), dangerous], tmp, 5.0)
            self.assertEqual(outcome["exit_code"], 0, outcome["stderr"])
            received = json.loads(outcome["stdout"])
            self.assertEqual(received, [dangerous])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class OutputTruncationTests(unittest.TestCase):
    def test_stdout_and_stderr_truncated_with_flag(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ccc-run-trunc-"))
        try:
            over = ccc.MAX_OUTPUT_BYTES + 500
            code = f"import sys; sys.stdout.write('A'*{over}); sys.stderr.write('B'*{over})"
            outcome = ccc._run_one_check([sys.executable, "-c", code], tmp, 10.0)
            self.assertTrue(outcome["stdout_truncated"])
            self.assertTrue(outcome["stderr_truncated"])
            self.assertEqual(len(outcome["stdout"]), ccc.MAX_OUTPUT_BYTES)
            self.assertEqual(len(outcome["stderr"]), ccc.MAX_OUTPUT_BYTES)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_short_output_not_marked_truncated(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ccc-run-notrunc-"))
        try:
            outcome = ccc._run_one_check([sys.executable, "-c", "print('hi')"], tmp, 5.0)
            self.assertFalse(outcome["stdout_truncated"])
            self.assertFalse(outcome["stderr_truncated"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ProcessGroupTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-run-pg-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_timeout_kills_whole_process_group_including_grandchild(self) -> None:
        pidfile = self.tmp / "grandchild.pid"
        spawner = self.tmp / "spawner.py"
        spawner.write_text(
            "import subprocess, sys, time\n"
            "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(gc.pid))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        outcome = ccc._run_one_check([sys.executable, str(spawner)], self.tmp, 1.0)
        self.assertTrue(outcome["timed_out"])

        deadline = time.monotonic() + 3.0
        pid = None
        while time.monotonic() < deadline:
            if pidfile.exists():
                content = pidfile.read_text(encoding="utf-8").strip()
                if content:
                    pid = int(content)
                    break
            time.sleep(0.05)
        self.assertIsNotNone(pid, "grandchild never wrote its own pid -- fixture race, not what's under test")
        self.assertTrue(_wait_until_process_gone(pid), "grandchild survived the parent's timeout-triggered group kill")


# ---------------------------------------------------------------------------
# Fix-round regression tests: the timeout-kill decision in _drain_and_wait()
# was gated on `open_fds` (pipe state) rather than actual process/group
# liveness. A check_command that closed or redirected its own stdout/stderr
# (fd 1/2) before its declared timeout elapsed made `open_fds` empty EARLY,
# so the tool's only os.killpg() call was never reached even though the
# process (or, in the more severe variant, a forked grandchild) was still
# running well past both the declared timeout and the
# --timeout-ceiling-seconds ceiling.
# ---------------------------------------------------------------------------


class StdioClosedButProcessAliveTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-run-stdio-closed-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_direct_child_closes_stdio_then_outlives_timeout_is_killed(self) -> None:
        """Variant 1: the direct child itself closes fd 1/2 immediately,
        then keeps running well past its declared timeout. Pre-fix,
        `open_fds` emptied out right away (both pipes hit EOF as soon as
        the child closed its own copies), `timed_out` stayed False
        forever, and the tool returned after PROCESS_WAIT_GRACE_SECONDS
        with `timed_out: False, exit_code: None` while the child was
        still alive and running."""
        pidfile = self.tmp / "child.pid"
        script = self.tmp / "close_and_sleep.py"
        script.write_text(
            "import os, time\n"
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "os.close(1)\n"
            "os.close(2)\n"
            "time.sleep(20)\n",
            encoding="utf-8",
        )
        start = time.monotonic()
        outcome = ccc._run_one_check([sys.executable, str(script)], self.tmp, 1.0)
        elapsed = time.monotonic() - start

        self.assertTrue(outcome["timed_out"], outcome)
        # Killed by SIGKILL -- Popen reports this as returncode -SIGKILL,
        # never as a clean 0 or an unresolved None.
        self.assertEqual(outcome["exit_code"], -signal.SIGKILL, outcome)
        self.assertLess(
            elapsed,
            1.0 + ccc.PROCESS_GROUP_KILL_DRAIN_GRACE_SECONDS + ccc.PROCESS_WAIT_GRACE_SECONDS + 5.0,
        )

        pid = int(pidfile.read_text(encoding="utf-8").strip())
        self.assertTrue(
            _wait_until_process_gone(pid),
            "child that closed its own stdio survived past its declared timeout -- P1 regressed",
        )

    def test_forked_grandchild_survives_direct_child_exit_and_is_killed(self) -> None:
        """Variant 2, the more severe repro: the direct child forks a
        grandchild that closes fd 1/2 and keeps running, while the direct
        child itself exits immediately with os._exit(0). Neither process
        ever calls os.setsid() -- the grandchild stays in the SAME
        process group os.killpg() targets. Pre-fix this was a FALSE PASS:
        both pipes hit EOF almost instantly (the direct child's exit and
        the grandchild's own fd-close each drop a reference to the pipe's
        write end), so the tool returned `timed_out: False, exit_code: 0`
        -- a clean, honestly-reported "success" for the direct child --
        while the grandchild kept running un-killed."""
        pidfile = self.tmp / "grandchild.pid"
        script = self.tmp / "fork_and_exit.py"
        script.write_text(
            "import os, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            # Write the pidfile BEFORE closing fd 1/2 (not after) -- this
            # is a deliberate ordering, not cosmetic: closing fd 1/2 is
            # what can make the parent's read loop observe EOF and race
            # ahead to killing this process, so the pidfile write must
            # be guaranteed (by program order within THIS process) to
            # have already landed before that close call even happens.
            f"    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "    os.close(1)\n"
            "    os.close(2)\n"
            "    time.sleep(20)\n"
            "    os._exit(0)\n"
            "else:\n"
            "    os._exit(0)\n",
            encoding="utf-8",
        )
        start = time.monotonic()
        outcome = ccc._run_one_check([sys.executable, str(script)], self.tmp, 1.0)
        elapsed = time.monotonic() - start

        # The direct child's own exit code (0, from os._exit(0)) is
        # honestly reported -- that part is correct and unchanged. The
        # fix is that `timed_out` must now be True precisely because
        # something in its process group was still alive at the
        # deadline, which is what stops callers from treating
        # (exit_code=0, timed_out=False) as a trustworthy clean pass.
        self.assertEqual(outcome["exit_code"], 0, outcome)
        self.assertTrue(
            outcome["timed_out"],
            "false pass: reported timed_out=False while the grandchild was still alive -- " + repr(outcome),
        )
        self.assertLess(
            elapsed,
            1.0 + ccc.PROCESS_GROUP_KILL_DRAIN_GRACE_SECONDS + ccc.PROCESS_WAIT_GRACE_SECONDS + 5.0,
        )

        deadline = time.monotonic() + 3.0
        pid = None
        while time.monotonic() < deadline:
            if pidfile.exists():
                content = pidfile.read_text(encoding="utf-8").strip()
                if content:
                    pid = int(content)
                    break
            time.sleep(0.05)
        self.assertIsNotNone(pid, "grandchild never wrote its own pid -- fixture race, not what's under test")
        self.assertTrue(
            _wait_until_process_gone(pid),
            "grandchild survived past the parent's timeout-triggered group kill -- P1 (false pass) regressed",
        )

    def test_closes_stdio_early_but_exits_cleanly_before_deadline_is_still_a_normal_success(self) -> None:
        """Regression guard for the fix's own precision: a fast,
        well-behaved check that happens to close its own stdout/stderr
        early and then exits cleanly BEFORE the declared deadline must
        stay a normal, non-timed-out success -- the fix must only change
        behavior for processes still ALIVE at the deadline, not for every
        check that merely touches fd 1/2."""
        script = self.tmp / "close_then_exit_fast.py"
        script.write_text(
            "import os\nos.close(1)\nos.close(2)\n",
            encoding="utf-8",
        )
        outcome = ccc._run_one_check([sys.executable, str(script)], self.tmp, 10.0)
        self.assertFalse(outcome["timed_out"], outcome)
        self.assertEqual(outcome["exit_code"], 0, outcome)


# ---------------------------------------------------------------------------
# Authorization + affected-set gating
# ---------------------------------------------------------------------------


class RunAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-run-auth-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_authorize_project_is_exit_2(self) -> None:
        catalog = make_affected_catalog("t#x", "dep")
        result = _run_compat(
            catalog=catalog, global_id="t#x", authorize_project=None, compat_runs_root=self.tmp / "runs"
        )
        self.assertEqual(result["exit_code"], 2)

    def test_unauthorized_project_not_in_affected_set_is_exit_2_zero_execution(self) -> None:
        catalog = make_affected_catalog("t#x", "dep")
        with mock.patch.object(subprocess, "Popen") as popen:
            result = _run_compat(
                catalog=catalog,
                global_id="t#x",
                authorize_project=["not-affected"],
                compat_runs_root=self.tmp / "runs",
            )
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["offending_project_ids"], ["not-affected"])
        popen.assert_not_called()
        self.assertFalse((self.tmp / "runs").exists())

    def test_wiki_pages_only_global_id_resolves_to_empty_affected_set(self) -> None:
        """Permanent rule (design doc 3.5.4): wiki_pages content is
        structurally unreachable for Tier-2 execution. capability_reverse_
        index is only ever built from capabilities[].depends_on
        (build_cross_project_catalog.py never iterates wiki_pages[] into
        it), so a wiki-page global_id naturally resolves to affected=[] --
        this is a regression test proving that stays true, never raises,
        and "run" never partially executes against it."""
        global_id = "p#page:home"
        catalog = make_catalog(
            reverse_index={},
            wiki_pages=[{"id": "home", "project_id": "p", "global_id": global_id, "title": "Home"}],
            verified_at=fresh_iso(),
        )
        affected = ccc.check_affected(catalog, global_id)
        self.assertEqual(affected["affected"], [])

        with mock.patch.object(subprocess, "Popen") as popen:
            result = _run_compat(
                catalog=catalog,
                global_id=global_id,
                authorize_project=["p"],
                compat_runs_root=self.tmp / "runs",
            )
        self.assertEqual(result["exit_code"], 2)
        popen.assert_not_called()


# ---------------------------------------------------------------------------
# Full run_compatibility_checks() end-to-end scenarios
# ---------------------------------------------------------------------------


class RunEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-run-e2e-"))
        self.runs_root = self.tmp / "runs"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_successful_single_project_run_exit_0(self) -> None:
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        write_compat_check(
            dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
        result = _run_compat(
            catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=self.runs_root
        )
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["projects"][0]["outcome"], "ok")
        self.assertEqual(result["projects"][0]["checks"][0]["exit_code"], 0)
        output_path = Path(result["projects"][0]["output_path"])
        self.assertTrue(output_path.exists())
        # Compare resolved forms on both sides -- output_path is itself a
        # .resolve()d path (see write_only_within()), and on macOS a
        # tempdir path commonly differs from its resolved form only by the
        # /tmp -> /private/tmp symlink, which would otherwise make a plain
        # string-prefix check here a false negative rather than a real bug.
        self.assertTrue(str(output_path).startswith(str(self.runs_root.resolve())))

    def test_failing_check_yields_exit_1(self) -> None:
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        write_compat_check(
            dep_dir,
            [
                make_check_entry(
                    depends_on_ref=global_id, check_command=[sys.executable, "-c", "import sys; sys.exit(7)"]
                )
            ],
        )
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
        result = _run_compat(
            catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=self.runs_root
        )
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["projects"][0]["checks"][0]["exit_code"], 7)

    def test_no_matching_checks_is_skip_exit_3(self) -> None:
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        write_compat_check(dep_dir, [make_check_entry(depends_on_ref="other#thing")])
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
        result = _run_compat(
            catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=self.runs_root
        )
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["projects"][0]["outcome"], "skipped")

    def test_missing_compat_check_file_is_skip_exit_3(self) -> None:
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")  # no compat-check.json written
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
        result = _run_compat(
            catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=self.runs_root
        )
        self.assertEqual(result["exit_code"], 3)

    def test_malformed_compat_check_in_one_project_does_not_abort_second(self) -> None:
        global_id = "t#x"
        bad_dir = make_project_dir(self.tmp, "proj-bad")
        good_dir = make_project_dir(self.tmp, "proj-good")
        (bad_dir / "wiki" / "compat-check.json").write_text(
            json.dumps({"schema_version": 1, "checks": "not-a-list"}), encoding="utf-8"
        )
        write_compat_check(
            good_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        row = make_reverse_row(
            in_degree=2,
            referencing_project_ids=["proj-bad", "proj-good"],
            referenced_by=[
                make_edge(project_id="proj-bad", capability_global_id="proj-bad#dep"),
                make_edge(project_id="proj-good", capability_global_id="proj-good#dep"),
            ],
        )
        catalog = make_catalog(
            reverse_index={global_id: row},
            projects=[make_project_row("proj-bad", bad_dir), make_project_row("proj-good", good_dir)],
            verified_at=fresh_iso(),
        )
        result = _run_compat(
            catalog=catalog,
            global_id=global_id,
            authorize_project=["proj-bad", "proj-good"],
            compat_runs_root=self.runs_root,
        )
        self.assertEqual(result["exit_code"], 3)
        by_id = {r["project_id"]: r for r in result["projects"]}
        self.assertEqual(by_id["proj-bad"]["outcome"], "skipped")
        self.assertEqual(by_id["proj-good"]["outcome"], "ok")
        self.assertEqual(by_id["proj-good"]["checks"][0]["exit_code"], 0)
        # Both projects still get an output file -- the malformed one
        # records ITS OWN skip reason, it does not silently vanish.
        self.assertTrue(Path(by_id["proj-bad"]["output_path"]).exists())
        self.assertTrue(Path(by_id["proj-good"]["output_path"]).exists())

    def test_toctou_fresh_read_sees_content_added_after_affected_resolution(self) -> None:
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        write_compat_check(dep_dir, [make_check_entry(depends_on_ref="other#thing")])  # zero matches initially
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])

        real_check_affected = ccc.check_affected

        def _mutate_then_answer(catalog_arg, gid):
            answer = real_check_affected(catalog_arg, gid)
            # Simulate a concurrent commit landing between affected-set
            # resolution (which only ever reads catalog.json) and this
            # tool's own fresh, right-now read of compat-check.json.
            write_compat_check(
                dep_dir,
                [
                    make_check_entry(
                        depends_on_ref=global_id, check_command=[sys.executable, "-c", "print('NEW')"]
                    )
                ],
            )
            return answer

        with mock.patch.object(ccc, "check_affected", side_effect=_mutate_then_answer):
            result = _run_compat(
                catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=self.runs_root
            )
        self.assertEqual(result["exit_code"], 0, result)
        checks = result["projects"][0]["checks"]
        self.assertEqual(len(checks), 1)
        self.assertIn("NEW", checks[0]["stdout"])

    def test_project_id_path_injection_refused_before_any_write(self) -> None:
        global_id = "t#x"
        malicious = "../../OUTSIDE/pwned"
        row = make_reverse_row(
            in_degree=1,
            referencing_project_ids=[malicious],
            referenced_by=[make_edge(project_id=malicious, capability_global_id=f"{malicious}#dep")],
        )
        catalog = make_catalog(
            reverse_index={global_id: row},
            projects=[make_project_row(malicious, self.tmp / "wherever")],
            verified_at=fresh_iso(),
        )
        result = _run_compat(
            catalog=catalog, global_id=global_id, authorize_project=[malicious], compat_runs_root=self.runs_root
        )
        self.assertEqual(result["exit_code"], 3)
        project_result = result["projects"][0]
        self.assertEqual(project_result["outcome"], "skipped")
        self.assertIsNone(project_result["output_path"])
        codes = [w["code"] for w in project_result["warnings"]]
        self.assertIn("project_id_invalid", codes)
        # Nothing was ever written anywhere -- not even a same-named "skip
        # notice" file, because the project_id itself isn't safe to use as
        # a filename in the first place.
        all_written = list(self.runs_root.rglob("*.json")) if self.runs_root.exists() else []
        self.assertEqual(all_written, [])
        self.assertFalse((self.tmp / "OUTSIDE").exists())

    def test_run_id_unique_across_back_to_back_invocations(self) -> None:
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        write_compat_check(
            dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
        result1 = _run_compat(
            catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=self.runs_root
        )
        result2 = _run_compat(
            catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=self.runs_root
        )
        self.assertNotEqual(result1["run_id"], result2["run_id"])
        self.assertTrue((self.runs_root / result1["run_id"]).is_dir())
        self.assertTrue((self.runs_root / result2["run_id"]).is_dir())

    def test_stale_catalog_without_override_is_exit_4_zero_execution(self) -> None:
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        write_compat_check(
            dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        old = fresh_iso(datetime.now(timezone.utc) - timedelta(hours=10))
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)], verified_at=old)
        with mock.patch.object(subprocess, "Popen") as popen:
            result = _run_compat(
                catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=self.runs_root
            )
        self.assertEqual(result["exit_code"], 4)
        self.assertEqual(result["reason"], "catalog_stale")
        popen.assert_not_called()
        self.assertFalse(self.runs_root.exists())

    def test_stale_catalog_with_allow_override_proceeds(self) -> None:
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        write_compat_check(
            dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        old = fresh_iso(datetime.now(timezone.utc) - timedelta(hours=10))
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)], verified_at=old)
        result = _run_compat(
            catalog=catalog,
            global_id=global_id,
            authorize_project=["dep"],
            compat_runs_root=self.runs_root,
            allow_stale_catalog=True,
        )
        self.assertEqual(result["exit_code"], 0, result)
        self.assertTrue(result["catalog_stale"])

    def test_timeout_ceiling_caps_declared_timeout(self) -> None:
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        write_compat_check(
            dep_dir,
            [
                make_check_entry(
                    depends_on_ref=global_id,
                    check_command=[sys.executable, "-c", "import time; time.sleep(5)"],
                    timeout_seconds=1000,
                )
            ],
        )
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
        result = _run_compat(
            catalog=catalog,
            global_id=global_id,
            authorize_project=["dep"],
            compat_runs_root=self.runs_root,
            timeout_ceiling_seconds=1.0,
        )
        check = result["projects"][0]["checks"][0]
        self.assertEqual(check["declared_timeout_seconds"], 1000.0)
        self.assertEqual(check["effective_timeout_seconds"], 1.0)
        self.assertTrue(check["timed_out"])

    def test_false_pass_from_orphaned_grandchild_is_not_reported_as_a_clean_run(self) -> None:
        """End-to-end regression for the more severe of the two P1
        variants (see StdioClosedButProcessAliveTimeoutTests): a
        check_command's direct process forks a grandchild that closes
        its own stdio and keeps running past the declared timeout, while
        the direct process itself exits 0 immediately -- no setsid(), so
        the grandchild stays in the SAME process group. Pre-fix,
        run_compatibility_checks() -- which treats `exit_code == 0 and
        not timed_out` as a genuine success -- could report the whole
        run as exit_code=0 while that grandchild was silently still
        running. Post-fix, the individual check is correctly flagged
        timed_out=True, which flips the run's overall exit_code to 1
        (any_failed_or_timed_out) even though the direct child's own
        exit_code is an honest 0."""
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        pidfile = self.tmp / "grandchild.pid"
        script = self.tmp / "fork_and_exit.py"
        script.write_text(
            "import os, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            # Write the pidfile BEFORE closing fd 1/2 (not after) -- see
            # the identical fixture in StdioClosedButProcessAliveTimeout
            # Tests for why this ordering isn't cosmetic.
            f"    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "    os.close(1)\n"
            "    os.close(2)\n"
            "    time.sleep(20)\n"
            "    os._exit(0)\n"
            "else:\n"
            "    os._exit(0)\n",
            encoding="utf-8",
        )
        write_compat_check(
            dep_dir,
            [
                make_check_entry(
                    depends_on_ref=global_id,
                    check_command=[sys.executable, str(script)],
                    timeout_seconds=1,
                )
            ],
        )
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
        result = _run_compat(
            catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=self.runs_root
        )
        check = result["projects"][0]["checks"][0]
        self.assertEqual(check["exit_code"], 0, check)
        self.assertTrue(check["timed_out"], check)
        self.assertEqual(
            result["exit_code"],
            1,
            "run reported a clean pass (exit_code != 1) while a grandchild was still running -- false pass regressed: "
            + repr(result),
        )

        deadline = time.monotonic() + 3.0
        pid = None
        while time.monotonic() < deadline:
            if pidfile.exists():
                content = pidfile.read_text(encoding="utf-8").strip()
                if content:
                    pid = int(content)
                    break
            time.sleep(0.05)
        self.assertIsNotNone(pid, "grandchild never wrote its own pid -- fixture race, not what's under test")
        self.assertTrue(_wait_until_process_gone(pid), "grandchild survived the full run -- false pass regressed")

    def test_lock_released_before_execution_phase_does_not_deadlock(self) -> None:
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        lock_touch_code = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('held-by-check-command')"
        write_compat_check(
            dep_dir,
            [
                make_check_entry(
                    depends_on_ref=global_id,
                    check_command=[sys.executable, "-c", lock_touch_code, str(self.runs_root / ".compat-runs.lock")],
                )
            ],
        )
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
        with mock.patch.object(ccc, "acquire_lock", side_effect=ccc.acquire_lock) as spy:
            result = _run_compat(
                catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=self.runs_root
            )
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(result["exit_code"], 0, result)

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "permission bits meaningless as root / non-posix")
    def test_readonly_base_dir_raises_named_lock_uncreatable_not_generic_error(self) -> None:
        # Mirrors build_mention_evidence.py's acquire_lock() OSError branch:
        # a read-only base_dir must surface as a NAMED CheckFatal reason,
        # not propagate as an untyped OSError reported as unexpected_error.
        base_dir = self.tmp / "lock-readonly-dir"
        base_dir.mkdir()
        os.chmod(str(base_dir), 0o500)
        try:
            with self.assertRaises(ccc.CheckFatal) as ctx:
                ccc.acquire_lock(base_dir)
            self.assertEqual(ctx.exception.reason, "lock_uncreatable")
        finally:
            os.chmod(str(base_dir), 0o700)


# ---------------------------------------------------------------------------
# CLI wiring smoke test
# ---------------------------------------------------------------------------


class CliRunSmokeTest(unittest.TestCase):
    def test_run_subcommand_prints_json_and_exits_correctly(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ccc-run-cli-"))
        try:
            dep_dir = make_project_dir(tmp, "dep")
            global_id = "t#x"
            write_compat_check(
                dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
            )
            catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
            catalog_path = tmp / "catalog.json"
            _write_json(catalog_path, catalog)
            runs_root = tmp / "runs"
            code, out, err = _run_main(
                [
                    "run",
                    "--global-id",
                    global_id,
                    "--authorize-project",
                    "dep",
                    "--catalog",
                    str(catalog_path),
                    "--compat-runs-root",
                    str(runs_root),
                ]
            )
            self.assertEqual(code, 0, out + err)
            payload = json.loads(out)
            self.assertEqual(payload["exit_code"], 0)
            self.assertEqual(payload["projects"][0]["outcome"], "ok")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_run_subcommand_usage_error_no_authorize_project(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ccc-run-cli-usage-"))
        try:
            catalog_path = tmp / "catalog.json"
            _write_json(catalog_path, make_affected_catalog("t#x", "dep"))
            code, out, err = _run_main(
                ["run", "--global-id", "t#x", "--catalog", str(catalog_path), "--compat-runs-root", str(tmp / "runs")]
            )
            self.assertEqual(code, 2)
            payload = json.loads(out)
            self.assertEqual(payload["reason"], "authorize_project_required")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# Fix-round regression tests (adversarial review of the "run" subcommand):
# injection-and-exec, path-and-containment, authz-and-toctou.
# ===========================================================================


# ---------------------------------------------------------------------------
# P0 / P1: process-group escape (setsid) must not hang the tool forever
# ---------------------------------------------------------------------------


class ProcessGroupEscapeTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-run-escape-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipIf(os.name != "posix", "fork/setsid assumed posix")
    def test_setsid_escaping_grandchild_does_not_hang_the_tool(self) -> None:
        """Regression for the P0 found in independent review: a grandchild
        that calls os.setsid() leaves the process group os.killpg()
        targets, survives the group's SIGKILL, and can keep the inherited
        stdout pipe write end open indefinitely. Before the fix,
        _run_one_check's recovery `proc.communicate()` had no timeout and
        blocked on that pipe for as long as the escapee lived (reproduced
        pre-fix at >25s wall time against a 1s declared timeout, and
        unboundedly against a longer-lived escapee)."""
        pidfile = self.tmp / "escapee.pid"
        script = self.tmp / "daemonize.py"
        script.write_text(
            "import os, sys, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    os.setsid()\n"
            f"    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "    time.sleep(20)\n"
            "    os._exit(0)\n"
            "else:\n"
            "    sys.exit(0)\n",
            encoding="utf-8",
        )
        start = time.monotonic()
        outcome = ccc._run_one_check([sys.executable, str(script)], self.tmp, 1.0)
        elapsed = time.monotonic() - start
        # Bounded by the declared timeout plus the fixed post-kill drain
        # grace window plus the wait grace window -- never by the escaped
        # grandchild's own 20s sleep.
        self.assertLess(
            elapsed,
            1.0 + ccc.PROCESS_GROUP_KILL_DRAIN_GRACE_SECONDS + ccc.PROCESS_WAIT_GRACE_SECONDS + 5.0,
        )
        self.assertTrue(outcome["timed_out"])

        deadline = time.monotonic() + 3.0
        pid = None
        while time.monotonic() < deadline:
            if pidfile.exists():
                content = pidfile.read_text(encoding="utf-8").strip()
                if content:
                    pid = int(content)
                    break
            time.sleep(0.05)
        self.assertIsNotNone(pid, "escapee never wrote its own pid -- fixture race, not what's under test")
        # The escapee is NOT expected to be killed by this tool -- that is
        # a documented, accepted limitation (os.killpg cannot follow a
        # setsid() escape; see _drain_and_wait's own docstring). Reap it
        # ourselves so the test suite does not leak a process.
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------------------
# P1: MAX_OUTPUT_BYTES must be a genuine memory bound, not a display cap
# applied after Popen.communicate() already buffered everything.
# ---------------------------------------------------------------------------


class OutputMemoryBoundTests(unittest.TestCase):
    def test_large_output_is_capped_while_reading_not_after(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ccc-run-mem-"))
        try:
            code = "import sys\nb=b'A'*(1<<20)\nfor _ in range(50): sys.stdout.buffer.write(b)"
            outcome = ccc._run_one_check([sys.executable, "-c", code], tmp, 10.0)
            self.assertEqual(outcome["exit_code"], 0, outcome["stderr"])
            self.assertTrue(outcome["stdout_truncated"])
            self.assertLessEqual(len(outcome["stdout"]), ccc.MAX_OUTPUT_BYTES)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_decoded_text_length_is_also_bounded_despite_replacement_expansion(self) -> None:
        """A byte cap applied BEFORE decode can still produce a decoded
        string up to ~3x longer once invalid UTF-8 bytes each expand to a
        3-byte U+FFFD replacement char (P3 in independent review) --
        _truncate_output() now slices again after decode."""
        tmp = Path(tempfile.mkdtemp(prefix="ccc-run-mem2-"))
        try:
            code = "import sys; sys.stdout.buffer.write(b'\\xff' * 200000)"
            outcome = ccc._run_one_check([sys.executable, "-c", code], tmp, 10.0)
            self.assertLessEqual(len(outcome["stdout"]), ccc.MAX_OUTPUT_BYTES)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# P1: no `env=` meant a check_command inherited the whole parent environment
# ---------------------------------------------------------------------------


class EnvironmentIsolationTests(unittest.TestCase):
    def test_check_command_does_not_inherit_arbitrary_parent_env_vars(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ccc-run-env-"))
        try:
            with mock.patch.dict(os.environ, {"CCC_TEST_CANARY_SECRET": "sk-should-not-leak"}):
                outcome = ccc._run_one_check(
                    [sys.executable, "-c", "import os,json; print(json.dumps(sorted(os.environ)))"],
                    tmp,
                    5.0,
                )
            self.assertEqual(outcome["exit_code"], 0, outcome["stderr"])
            seen = json.loads(outcome["stdout"])
            self.assertNotIn("CCC_TEST_CANARY_SECRET", seen)
            # On macOS, the CoreFoundation runtime injects
            # `__CF_USER_TEXT_ENCODING` into any process that links it
            # (verified independently: it appears even with `env={"PATH":
            # ...}` passed explicitly to subprocess.run, i.e. it comes from
            # the OS/dyld itself at exec time, not from anything this tool
            # inherits or controls) -- it carries no secret, just an opaque
            # numeric locale/uid encoding, so it is excluded from this
            # allowlist check rather than added to _ENV_ALLOWLIST itself
            # (which describes what THIS tool chooses to forward, not what
            # the OS adds unconditionally regardless of that choice).
            platform_injected = {"__CF_USER_TEXT_ENCODING"}
            for name in seen:
                self.assertIn(name, set(ccc._ENV_ALLOWLIST) | platform_injected)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_check_command_does_not_inherit_parent_stdin(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ccc-run-stdin-"))
        try:
            outcome = ccc._run_one_check(
                [sys.executable, "-c", "import sys; sys.stdout.write(repr(sys.stdin.read()))"], tmp, 5.0
            )
            self.assertEqual(outcome["exit_code"], 0, outcome["stderr"])
            self.assertEqual(outcome["stdout"], "''")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# P1: non-finite (NaN/Infinity) numbers must never bypass a numeric gate
# ---------------------------------------------------------------------------


class NonFiniteNumberRejectionTests(unittest.TestCase):
    def test_nan_timeout_seconds_entry_is_rejected(self) -> None:
        entry = make_check_entry(timeout_seconds=float("nan"))
        validated, reason = ccc._validate_compat_check_document(
            {"schema_version": 1, "checks": [entry]}, Path(tempfile.gettempdir())
        )
        self.assertIsNone(validated)
        self.assertEqual(reason, "entry_bad_timeout_seconds:0")

    def test_infinite_timeout_seconds_entry_is_rejected(self) -> None:
        entry = make_check_entry(timeout_seconds=float("inf"))
        validated, reason = ccc._validate_compat_check_document(
            {"schema_version": 1, "checks": [entry]}, Path(tempfile.gettempdir())
        )
        self.assertEqual(reason, "entry_bad_timeout_seconds:0")

    def test_nan_literal_in_compat_check_json_is_rejected_at_parse_time(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ccc-nan-parse-"))
        try:
            project_dir = make_project_dir(tmp, "p")
            (project_dir / "wiki" / "compat-check.json").write_text(
                '{"schema_version": 1, "checks": [{"id":"x","depends_on_ref":"t#x",'
                '"check_command":["/bin/echo","hi"],"cwd":".","timeout_seconds":NaN,'
                '"reviewed_by":"a","reviewed_at":"b"}]}',
                encoding="utf-8",
            )
            doc, reason, digest = ccc._read_compat_check(project_dir)
            self.assertIsNone(doc)
            self.assertEqual(reason, "compat_check_not_valid_json")
            self.assertIsNone(digest)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_infinity_literal_in_catalog_json_is_rejected_at_parse_time(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ccc-inf-catalog-"))
        try:
            path = tmp / "catalog.json"
            path.write_text('{"schema_version": 1, "verified_at": "x", "budget": Infinity}', encoding="utf-8")
            with self.assertRaises(ccc.CheckFatal) as ctx:
                ccc.load_catalog(path)
            self.assertEqual(ctx.exception.reason, "catalog_unparseable")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_nan_timeout_ceiling_seconds_is_rejected_as_usage_error(self) -> None:
        catalog = make_affected_catalog("t#x", "dep")
        result = _run_compat(
            catalog=catalog,
            global_id="t#x",
            authorize_project=["dep"],
            compat_runs_root=Path(tempfile.mkdtemp(prefix="ccc-nan-ceil-")),
            timeout_ceiling_seconds=float("nan"),
        )
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "timeout_ceiling_not_positive")

    def test_infinite_timeout_ceiling_seconds_is_rejected_as_usage_error(self) -> None:
        """This is the specific P1 repro from independent review: an
        infinite ceiling used to let a project's own declared
        timeout_seconds win outright (`min(declared, inf) == declared`),
        defeating the "hard ceiling regardless of what a project declares"
        guarantee."""
        catalog = make_affected_catalog("t#x", "dep")
        result = _run_compat(
            catalog=catalog,
            global_id="t#x",
            authorize_project=["dep"],
            compat_runs_root=Path(tempfile.mkdtemp(prefix="ccc-inf-ceil-")),
            timeout_ceiling_seconds=float("inf"),
        )
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "timeout_ceiling_not_positive")

    def test_nan_stale_after_seconds_is_rejected_as_usage_error(self) -> None:
        catalog = make_affected_catalog("t#x", "dep")
        result = _run_compat(
            catalog=catalog,
            global_id="t#x",
            authorize_project=["dep"],
            compat_runs_root=Path(tempfile.mkdtemp(prefix="ccc-nan-stale-")),
            stale_after_seconds=float("nan"),
        )
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["reason"], "stale_after_hours_negative")

    def test_run_one_check_with_nan_timeout_refuses_to_execute(self) -> None:
        """Defense-in-depth: even called directly (bypassing document
        validation), _run_one_check must never spawn a process it cannot
        bound -- this is the other half of the "spawn, then leak/hang
        forever" bug class found in independent review."""
        tmp = Path(tempfile.mkdtemp(prefix="ccc-nan-direct-"))
        try:
            with mock.patch.object(subprocess, "Popen") as popen:
                outcome = ccc._run_one_check([sys.executable, "-c", "pass"], tmp, float("nan"))
            popen.assert_not_called()
            self.assertIsNone(outcome["exit_code"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# P1: a write failure for one project must never lose every other
# project's already-computed results in the same run.
# ---------------------------------------------------------------------------


class OutputWriteFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-run-writefail-"))
        self.runs_root = self.tmp / "runs"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_enametoolong_project_id_does_not_lose_other_projects_results(self) -> None:
        global_id = "t#x"
        long_id = "A" * 300  # shape-valid (no length bound), but overflows a filename component
        good_dir = make_project_dir(self.tmp, "zgood")
        write_compat_check(
            good_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        bad_dir = make_project_dir(self.tmp, "bad")
        write_compat_check(
            bad_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        row = make_reverse_row(
            in_degree=2,
            referencing_project_ids=[long_id, "zgood"],
            referenced_by=[
                make_edge(project_id=long_id, capability_global_id=f"{long_id}#dep"),
                make_edge(project_id="zgood", capability_global_id="zgood#dep"),
            ],
        )
        catalog = make_catalog(
            reverse_index={global_id: row},
            projects=[make_project_row(long_id, bad_dir), make_project_row("zgood", good_dir)],
            verified_at=fresh_iso(),
        )
        result = _run_compat(
            catalog=catalog, global_id=global_id, authorize_project=[long_id, "zgood"], compat_runs_root=self.runs_root
        )
        by_id = {r["project_id"]: r for r in result["projects"]}
        self.assertIn("zgood", by_id, "the other project's result was lost")
        self.assertEqual(by_id["zgood"]["outcome"], "ok")
        self.assertTrue(Path(by_id["zgood"]["output_path"]).exists())
        self.assertIn(long_id, by_id)
        self.assertEqual(by_id[long_id]["outcome"], "unrecorded")
        codes = [w["code"] for w in by_id[long_id]["warnings"]]
        self.assertIn("output_write_failed", codes)
        self.assertEqual(result["counts"]["unrecorded"], 1)
        self.assertEqual(result["counts"]["ok"], 1)
        self.assertEqual(result["exit_code"], 3)

    def test_output_dir_uncreatable_is_not_reported_as_ok(self) -> None:
        """A write that is REFUSED (not merely OS-failed) must also not
        report outcome 'ok' -- P2-2 in independent review."""
        global_id = "t#x"
        d_dir = make_project_dir(self.tmp, "d")
        write_compat_check(
            d_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        nested_id = "d.json/y"
        nested_dir = make_project_dir(self.tmp, "nested")
        write_compat_check(
            nested_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        row = make_reverse_row(
            in_degree=2,
            referencing_project_ids=["d", nested_id],
            referenced_by=[
                make_edge(project_id="d", capability_global_id="d#dep"),
                make_edge(project_id=nested_id, capability_global_id=f"{nested_id}#dep"),
            ],
        )
        catalog = make_catalog(
            reverse_index={global_id: row},
            projects=[make_project_row("d", d_dir), make_project_row(nested_id, nested_dir)],
            verified_at=fresh_iso(),
        )
        # "d" sorts before "d.json/y" -- "d" writes run_dir/d.json as a
        # FILE first; "d.json/y" then needs run_dir/d.json/ to exist as a
        # DIRECTORY for its own output, a deterministic collision within
        # the same run_dir regardless of the run's random uuid.
        result = _run_compat(
            catalog=catalog, global_id=global_id, authorize_project=["d", nested_id], compat_runs_root=self.runs_root
        )
        by_id = {r["project_id"]: r for r in result["projects"]}
        self.assertEqual(by_id["d"]["outcome"], "ok")
        self.assertNotEqual(by_id[nested_id]["outcome"], "ok")
        self.assertIsNone(by_id[nested_id]["output_path"])
        self.assertEqual(result["counts"]["ok"], 1)


# ---------------------------------------------------------------------------
# P1: a committed `wiki` symlink must not redirect what gets executed.
# ---------------------------------------------------------------------------


class WikiSymlinkContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-run-wikisym-"))
        self.runs_root = self.tmp / "runs"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipIf(os.name != "posix", "symlinks assumed posix")
    def test_wiki_directory_symlink_is_rejected_not_followed(self) -> None:
        global_id = "t#x"
        project_dir = self.tmp / "proj"
        project_dir.mkdir()
        outside = self.tmp / "attacker-writable"
        outside.mkdir()
        canary = outside / "canary"
        _write_json(
            outside / "compat-check.json",
            {
                "schema_version": 1,
                "checks": [
                    make_check_entry(
                        depends_on_ref=global_id,
                        check_command=[sys.executable, "-c", f"open({str(canary)!r}, 'w').write('ran')"],
                    )
                ],
            },
        )
        os.symlink(str(outside), str(project_dir / "wiki"))
        catalog = make_affected_catalog(global_id, "proj", projects=[make_project_row("proj", project_dir)])
        result = _run_compat(
            catalog=catalog, global_id=global_id, authorize_project=["proj"], compat_runs_root=self.runs_root
        )
        self.assertFalse(canary.exists(), "attacker command executed via a symlinked wiki/ directory")
        project_result = result["projects"][0]
        self.assertNotEqual(project_result["outcome"], "ok")
        codes = [w["code"] for w in project_result["warnings"]]
        self.assertIn("compat_check_symlink_component", codes)


# ---------------------------------------------------------------------------
# P2 (invariant #2, verbatim): cwd swapped between validation and use.
# ---------------------------------------------------------------------------


class CwdTocTouReRevalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-run-cwdtoctou-"))
        self.runs_root = self.tmp / "runs"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipIf(os.name != "posix", "symlinks assumed posix")
    def test_cwd_swapped_between_document_validation_and_second_checks_use_is_rejected(self) -> None:
        global_id = "t#x"
        project_dir = make_project_dir(self.tmp, "proj")
        (project_dir / "stage").mkdir()
        outside = self.tmp / "OUTSIDE"
        outside.mkdir()
        marker = outside / "marker.txt"

        swap_code = (
            f"import os; os.rmdir({str(project_dir / 'stage')!r}); "
            f"os.symlink({str(outside)!r}, {str(project_dir / 'stage')!r})"
        )
        detect_code = f"open({str(marker)!r}, 'w').write('escaped')"

        write_compat_check(
            project_dir,
            [
                make_check_entry(
                    id="a", depends_on_ref=global_id, cwd=".", check_command=[sys.executable, "-c", swap_code]
                ),
                make_check_entry(
                    id="b", depends_on_ref=global_id, cwd="stage", check_command=[sys.executable, "-c", detect_code]
                ),
            ],
        )
        catalog = make_affected_catalog(global_id, "proj", projects=[make_project_row("proj", project_dir)])
        result = _run_compat(
            catalog=catalog, global_id=global_id, authorize_project=["proj"], compat_runs_root=self.runs_root
        )
        self.assertFalse(marker.exists(), "second check ran with a cwd that had been swapped to a symlink after validation")
        checks = result["projects"][0]["checks"]
        self.assertEqual(len(checks), 2)
        self.assertIn("cwd re-validation failed", checks[1]["stderr"])


# ---------------------------------------------------------------------------
# P1 (authz-and-toctou): one authorized project's execution must not be
# able to rewrite a LATER authorized project's declared command.
# ---------------------------------------------------------------------------


class CrossProjectInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-run-crossinj-"))
        self.runs_root = self.tmp / "runs"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_authorized_project_cannot_rewrite_a_later_authorized_projects_declared_command(self) -> None:
        global_id = "t#x"
        a_dir = make_project_dir(self.tmp, "downstream-a")
        b_dir = make_project_dir(self.tmp, "downstream-b")
        write_compat_check(
            b_dir,
            [
                make_check_entry(
                    id="b-original",
                    depends_on_ref=global_id,
                    check_command=[sys.executable, "-c", "print('ORIGINAL')"],
                )
            ],
        )
        target_path = str(b_dir / "wiki" / "compat-check.json")
        inject_code = (
            "import json\n"
            "doc = {'schema_version': 1, 'checks': [{'id': 'INJECTED', "
            f"'depends_on_ref': {global_id!r}, "
            f"'check_command': [{sys.executable!r}, '-c', \"print('INJECTED')\"], "
            "'cwd': '.', 'timeout_seconds': 30, 'reviewed_by': 'attacker', "
            "'reviewed_at': '2026-08-23T00:00:00Z'}]}\n"
            f"open({target_path!r}, 'w').write(json.dumps(doc))\n"
        )
        write_compat_check(
            a_dir, [make_check_entry(id="a", depends_on_ref=global_id, check_command=[sys.executable, "-c", inject_code])]
        )
        row = make_reverse_row(
            in_degree=2,
            referencing_project_ids=["downstream-a", "downstream-b"],
            referenced_by=[
                make_edge(project_id="downstream-a", capability_global_id="downstream-a#dep"),
                make_edge(project_id="downstream-b", capability_global_id="downstream-b#dep"),
            ],
        )
        catalog = make_catalog(
            reverse_index={global_id: row},
            projects=[make_project_row("downstream-a", a_dir), make_project_row("downstream-b", b_dir)],
            verified_at=fresh_iso(),
        )
        result = _run_compat(
            catalog=catalog,
            global_id=global_id,
            authorize_project=["downstream-a", "downstream-b"],
            compat_runs_root=self.runs_root,
        )
        by_id = {r["project_id"]: r for r in result["projects"]}
        b_checks = by_id["downstream-b"]["checks"]
        self.assertEqual(len(b_checks), 1)
        self.assertEqual(b_checks[0]["id"], "b-original")
        self.assertIn("ORIGINAL", b_checks[0]["stdout"])


# ---------------------------------------------------------------------------
# P2 (authz-and-toctou): unbounded check count / duplicate ids.
# ---------------------------------------------------------------------------


class PartialOutcomePreservationTests(unittest.TestCase):
    """Defense-in-depth regression: even a runtime exception NOT caught by
    document validation (e.g. an unexpected internal error mid-loop) must
    not discard already-executed checks' results from the same project --
    this is the general fix behind the NUL-byte-specific validation fix
    above."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-run-partial-"))
        self.runs_root = self.tmp / "runs"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exception_after_first_check_preserves_that_checks_result(self) -> None:
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        write_compat_check(
            dep_dir,
            [
                make_check_entry(id="first", depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"]),
                make_check_entry(id="second", depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"]),
            ],
        )
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])

        real_run_one_check = ccc._run_one_check
        call_count = {"n": 0}

        def _boom_on_second(argv, cwd, timeout_seconds):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated internal error")
            return real_run_one_check(argv, cwd, timeout_seconds)

        with mock.patch.object(ccc, "_run_one_check", side_effect=_boom_on_second):
            result = _run_compat(
                catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=self.runs_root
            )
        project_result = result["projects"][0]
        self.assertEqual(project_result["outcome"], "partial")
        self.assertEqual(len(project_result["checks"]), 1)
        self.assertEqual(project_result["checks"][0]["id"], "first")
        codes = [w["code"] for w in project_result["warnings"]]
        self.assertIn("internal_error", codes)
        # Still persisted to disk -- "partial" outcome is not "skipped".
        self.assertTrue(Path(project_result["output_path"]).exists())


class ChecksCountAndDuplicateIdTests(unittest.TestCase):
    def test_too_many_checks_rejected(self) -> None:
        entries = [make_check_entry(id=f"c{i}") for i in range(ccc.MAX_CHECKS_PER_COMPAT_DOCUMENT + 1)]
        validated, reason = ccc._validate_compat_check_document(
            {"schema_version": 1, "checks": entries}, Path(tempfile.gettempdir())
        )
        self.assertIsNone(validated)
        self.assertEqual(reason, "too_many_checks")

    def test_at_the_limit_is_still_accepted(self) -> None:
        entries = [make_check_entry(id=f"c{i}") for i in range(ccc.MAX_CHECKS_PER_COMPAT_DOCUMENT)]
        validated, reason = ccc._validate_compat_check_document(
            {"schema_version": 1, "checks": entries}, Path(tempfile.gettempdir())
        )
        self.assertIsNone(reason)
        self.assertEqual(len(validated), ccc.MAX_CHECKS_PER_COMPAT_DOCUMENT)

    def test_duplicate_ids_rejected(self) -> None:
        entries = [make_check_entry(id="dup"), make_check_entry(id="dup")]
        validated, reason = ccc._validate_compat_check_document(
            {"schema_version": 1, "checks": entries}, Path(tempfile.gettempdir())
        )
        self.assertIsNone(validated)
        self.assertEqual(reason, "entry_duplicate_id:1")


# ---------------------------------------------------------------------------
# P3 (path-and-containment): --compat-runs-root must be absolute.
# ---------------------------------------------------------------------------


class CompatRunsRootAbsoluteTests(unittest.TestCase):
    def test_relative_compat_runs_root_flag_is_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ccc-relroot-"))
        try:
            dep_dir = make_project_dir(tmp, "dep")
            global_id = "t#x"
            write_compat_check(
                dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
            )
            catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
            catalog_path = tmp / "catalog.json"
            _write_json(catalog_path, catalog)
            cwd_before = os.getcwd()
            os.chdir(str(tmp))
            try:
                code, out, err = _run_main(
                    [
                        "run",
                        "--global-id",
                        global_id,
                        "--authorize-project",
                        "dep",
                        "--catalog",
                        str(catalog_path),
                        "--compat-runs-root",
                        "relative-runs",
                    ]
                )
            finally:
                os.chdir(cwd_before)
            self.assertEqual(code, 4, out + err)
            payload = json.loads(out)
            self.assertEqual(payload["reason"], "compat_runs_root_not_absolute")
            self.assertFalse((tmp / "relative-runs").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_legitimate_tempdir_redirection_still_works(self) -> None:
        # The non-adversarial case this flag exists for at all: a plain,
        # real, absolute tempdir with no symlink anywhere in it -- the
        # exact pattern this test suite's own RunEndToEndTests/CliRunSmoke
        # Test fixtures already rely on via self.runs_root / tmp / "runs".
        # Must keep passing unchanged by the new validation.
        tmp = Path(tempfile.mkdtemp(prefix="ccc-realroot-"))
        try:
            dep_dir = make_project_dir(tmp, "dep")
            global_id = "t#x"
            write_compat_check(
                dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
            )
            catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
            catalog_path = tmp / "catalog.json"
            _write_json(catalog_path, catalog)
            runs_root = tmp / "runs"
            code, out, err = _run_main(
                [
                    "run",
                    "--global-id",
                    global_id,
                    "--authorize-project",
                    "dep",
                    "--catalog",
                    str(catalog_path),
                    "--compat-runs-root",
                    str(runs_root),
                ]
            )
            self.assertEqual(code, 0, out + err)
            payload = json.loads(out)
            self.assertEqual(payload["exit_code"], 0)
            self.assertTrue(runs_root.is_dir())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_compat_runs_root_that_is_itself_a_symlink_is_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ccc-symroot-"))
        try:
            dep_dir = make_project_dir(tmp, "dep")
            global_id = "t#x"
            write_compat_check(
                dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
            )
            catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
            catalog_path = tmp / "catalog.json"
            _write_json(catalog_path, catalog)

            real_target = tmp / "actual-elsewhere"
            real_target.mkdir()
            symlinked_root = tmp / "runs-symlink"
            symlinked_root.symlink_to(real_target, target_is_directory=True)

            code, out, err = _run_main(
                [
                    "run",
                    "--global-id",
                    global_id,
                    "--authorize-project",
                    "dep",
                    "--catalog",
                    str(catalog_path),
                    "--compat-runs-root",
                    str(symlinked_root),
                ]
            )
            self.assertEqual(code, 4, out + err)
            payload = json.loads(out)
            self.assertEqual(payload["reason"], "compat_runs_root_symlink_component")
            # Nothing should have been written through the symlink.
            self.assertEqual(list(real_target.iterdir()), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_compat_runs_root_with_a_symlink_component_partway_through_is_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ccc-symmid-"))
        try:
            dep_dir = make_project_dir(tmp, "dep")
            global_id = "t#x"
            write_compat_check(
                dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
            )
            catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
            catalog_path = tmp / "catalog.json"
            _write_json(catalog_path, catalog)

            real_parent = tmp / "actual-parent"
            real_parent.mkdir()
            symlinked_parent = tmp / "parent-symlink"
            symlinked_parent.symlink_to(real_parent, target_is_directory=True)
            # The symlink is only a MIDDLE component -- "runs" itself is a
            # perfectly ordinary, not-yet-existing final segment appended
            # after it.
            compat_runs_root = symlinked_parent / "runs"

            code, out, err = _run_main(
                [
                    "run",
                    "--global-id",
                    global_id,
                    "--authorize-project",
                    "dep",
                    "--catalog",
                    str(catalog_path),
                    "--compat-runs-root",
                    str(compat_runs_root),
                ]
            )
            self.assertEqual(code, 4, out + err)
            payload = json.loads(out)
            self.assertEqual(payload["reason"], "compat_runs_root_symlink_component")
            self.assertEqual(list(real_parent.iterdir()), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_point_of_use_validation_rejects_symlink_even_bypassing_the_cli(self) -> None:
        # Fix-round regression test (review finding P2-A, reproduced): the
        # symlink check used to live ONLY in cmd_run's CLI-flag parsing --
        # a caller that reaches run_compatibility_checks() (the actual
        # write path, via ensure_compat_runs_root()) by any other route,
        # exactly like this test does, got NO validation at all. Calls
        # run_compatibility_checks() directly, the same way _run_compat()
        # does everywhere else in this file, deliberately skipping cmd_run
        # and its CLI-layer check entirely.
        tmp = Path(tempfile.mkdtemp(prefix="ccc-pou-symroot-"))
        try:
            dep_dir = make_project_dir(tmp, "dep")
            global_id = "t#x"
            write_compat_check(
                dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
            )
            catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])

            real_target = tmp / "actual-elsewhere"
            real_target.mkdir()
            symlinked_root = tmp / "runs-symlink"
            symlinked_root.symlink_to(real_target, target_is_directory=True)

            result = _run_compat(
                catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=symlinked_root
            )
            self.assertEqual(result["exit_code"], 4, result)
            self.assertEqual(result["reason"], "compat_runs_root_symlink_component")
            # Nothing should have been written through the symlink.
            self.assertEqual(list(real_target.iterdir()), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_compat_runs_root_unsafe_segment_dotdot_is_rejected(self) -> None:
        # P4 test-coverage gap closed: _absolute_path_has_unsafe_segment()
        # was reachable (confirmed by manual inspection in review) but had
        # no test exercising it through the public validator.
        tmp = Path(tempfile.mkdtemp(prefix="ccc-unsafeseg-"))
        try:
            dep_dir = make_project_dir(tmp, "dep")
            global_id = "t#x"
            write_compat_check(
                dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
            )
            catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
            catalog_path = tmp / "catalog.json"
            _write_json(catalog_path, catalog)

            traversal_root = tmp / "runs" / ".." / "escaped"
            code, out, err = _run_main(
                [
                    "run",
                    "--global-id",
                    global_id,
                    "--authorize-project",
                    "dep",
                    "--catalog",
                    str(catalog_path),
                    "--compat-runs-root",
                    str(traversal_root),
                ]
            )
            self.assertEqual(code, 4, out + err)
            payload = json.loads(out)
            self.assertEqual(payload["reason"], "compat_runs_root_unsafe_segment")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_symlink_ancestor_with_preexisting_leaf_is_a_known_open_residual(self) -> None:
        # Documents (does NOT celebrate) the accepted residual named in
        # _nearest_existing_ancestor_is_symlink()'s own docstring, and
        # independently confirmed still open by review: if an attacker's
        # symlink ancestor ALSO has the final leaf directory pre-created
        # through it (as opposed to the leaf not existing yet, which IS
        # caught -- see test_compat_runs_root_with_a_symlink_component_
        # partway_through_is_rejected above), the nearest-existing-ancestor
        # walk stops at that already-real leaf and never inspects the
        # symlink above it. This test pins that this is CURRENT, KNOWN
        # behavior (so a future change either closes it deliberately, with
        # this test updated, or a regression here is caught immediately) --
        # it is not an assertion that this is acceptable.
        tmp = Path(tempfile.mkdtemp(prefix="ccc-residual-"))
        try:
            dep_dir = make_project_dir(tmp, "dep")
            global_id = "t#x"
            write_compat_check(
                dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
            )
            catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])

            real_parent = tmp / "actual-parent"
            real_parent.mkdir()
            (real_parent / "runs").mkdir()  # attacker pre-creates the leaf through the real target
            symlinked_parent = tmp / "parent-symlink"
            symlinked_parent.symlink_to(real_parent, target_is_directory=True)
            compat_runs_root = symlinked_parent / "runs"

            result = _run_compat(
                catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=compat_runs_root
            )
            self.assertEqual(result["exit_code"], 0, result)
            written = list((real_parent / "runs").rglob("*.json"))
            self.assertTrue(written, "expected the known residual: output written through the attacker's symlink")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# P2/P3 (path-and-containment): ambiguous/relative real_path in projects[].
# ---------------------------------------------------------------------------


class ProjectRootIndexHardeningTests(unittest.TestCase):
    def test_duplicate_project_rows_emit_a_warning_not_silence(self) -> None:
        catalog = make_catalog(
            projects=[
                {"project_id": "p", "real_path": "/tmp/AAA-evil", "status": "ok"},
                {"project_id": "p", "real_path": "/tmp/zzz-real", "status": "ok"},
            ]
        )
        roots, warnings = ccc.build_project_root_index(catalog)
        self.assertEqual(str(roots["p"]), "/tmp/AAA-evil")  # tie-break behavior unchanged
        codes = [w["code"] for w in warnings]
        self.assertIn("ambiguous_project_root", codes)

    def test_relative_real_path_row_is_dropped(self) -> None:
        catalog = make_catalog(projects=[{"project_id": "p", "real_path": "relative/dir", "status": "ok"}])
        roots, warnings = ccc.build_project_root_index(catalog)
        self.assertNotIn("p", roots)

    def test_single_absolute_row_still_works(self) -> None:
        catalog = make_catalog(projects=[{"project_id": "p", "real_path": "/tmp/only", "status": "ok"}])
        roots, warnings = ccc.build_project_root_index(catalog)
        self.assertEqual(str(roots["p"]), "/tmp/only")
        self.assertEqual(warnings, [])


class ShortWriteRegressionTests(unittest.TestCase):
    """atomic_write_within() and acquire_lock() called the raw os.write(fd,
    payload) and discarded its return value. POSIX permits os.write() to
    return fewer bytes than requested for a regular file -- empirically
    reproduced on this exact machine via RLIMIT_FSIZE with no exception
    raised -- and the old code then unconditionally os.fsync()'d and
    os.replace()'d the truncated tmp file onto the LIVE target while the
    caller still reported success. Fixed via _write_all_bytes(), which loops
    os.write() until the full payload lands and raises immediately on zero
    forward progress. Mirrors wiki_edit_guard.py's ShortWriteRegressionTests
    and promote_capability.py's AtomicWriteInDirShortWriteRegressionTests."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-shortwrite-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)

    def test_write_all_bytes_raises_on_zero_progress_write(self) -> None:
        fd, path = tempfile.mkstemp(dir=str(self.tmp))
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        payload = b"x" * 100
        calls = {"n": 0}
        real_write = os.write

        def short_write(fd_, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd_, data[:40])  # short: only 40 of 100
            return 0  # no forward progress on the retry -- must raise, not spin

        with mock.patch.object(ccc.os, "write", side_effect=short_write):
            with self.assertRaises(OSError):
                ccc._write_all_bytes(fd, payload)
        os.close(fd)

    def test_write_all_bytes_loops_to_completion_on_multiple_short_writes(self) -> None:
        fd, path = tempfile.mkstemp(dir=str(self.tmp))
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        payload = b"y" * 100
        real_write = os.write
        chunks = [30, 30, 40]  # sums to 100 -- must NOT raise, must write it all

        def chunked_write(fd_, data):
            n = chunks.pop(0)
            return real_write(fd_, data[:n])

        with mock.patch.object(ccc.os, "write", side_effect=chunked_write):
            ccc._write_all_bytes(fd, payload)
        os.close(fd)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_atomic_write_within_refuses_rather_than_truncates_live_target(self) -> None:
        # End-to-end through atomic_write_within() itself (not just the
        # helper in isolation): a short write must never result in the tmp
        # file being renamed onto the live target, and the live target must
        # be left byte-identical with no leftover tmp file.
        base_dir = self.tmp / "out"
        base_dir.mkdir()
        final_path = base_dir / "target.json"
        original_bytes = b'{"content_version": 1}\n'
        final_path.write_bytes(original_bytes)

        new_payload = b'{"content_version": 2, "padding": "' + (b"z" * 200) + b'"}\n'
        real_write = os.write
        calls = {"n": 0}

        def short_write(fd_, data):
            # First call: write most of it but not all (a genuine short
            # write). Every call after that returns 0 -- no forward
            # progress at all, simulating a persistent condition (e.g. a
            # resource limit already at capacity), not just a slow one.
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd_, data[: max(1, len(data) - 50)])
            return 0

        with mock.patch.object(ccc.os, "write", side_effect=short_write):
            with self.assertRaises(OSError):
                ccc.atomic_write_within(base_dir, final_path, new_payload)

        self.assertEqual(final_path.read_bytes(), original_bytes)
        leftovers = [p for p in base_dir.iterdir() if p.name != final_path.name]
        self.assertEqual(leftovers, [], f"leftover tmp files: {leftovers}")

    def test_acquire_lock_refuses_rather_than_leaves_silently_truncated_stamp(self) -> None:
        base_dir = self.tmp / "lockdir"
        base_dir.mkdir()
        real_write = os.write
        calls = {"n": 0}

        def short_write(fd_, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd_, data[: max(1, len(data) - 5)])
            return 0

        with mock.patch.object(ccc.os, "write", side_effect=short_write):
            with self.assertRaises(ccc.CheckFatal):
                ccc.acquire_lock(base_dir)


# ===========================================================================
# Staging-by-default authorization gate for the "run" subcommand's output
# root (unification with Gate B's PROMOTION_ROOT convention).
# ===========================================================================


class CliRunProductionAuthorizationTest(unittest.TestCase):
    """Isolation convention used throughout this file's sibling test module
    (test_build_mention_evidence.py, lines ~100-106) and this module's own
    CliRunSmokeTest: rebind the module-level default-path constants directly
    in setUp/tearDown, never touch the real production path."""

    def setUp(self) -> None:
        self._orig_staging = ccc.COMPAT_RUNS_ROOT
        self._orig_production = ccc._PRODUCTION_DEFAULT_COMPAT_RUNS_ROOT
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-run-authz-"))
        self.staging_root = self.tmp / "staging" / "compat-runs-pending-authorization"
        self.production_root = self.tmp / "production" / "compat-runs"
        ccc.COMPAT_RUNS_ROOT = self.staging_root
        ccc._PRODUCTION_DEFAULT_COMPAT_RUNS_ROOT = self.production_root

    def tearDown(self) -> None:
        ccc.COMPAT_RUNS_ROOT = self._orig_staging
        ccc._PRODUCTION_DEFAULT_COMPAT_RUNS_ROOT = self._orig_production
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_catalog(self, tmp: Path) -> tuple[Path, str]:
        dep_dir = make_project_dir(tmp, "dep")
        global_id = "t#x"
        write_compat_check(
            dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
        catalog_path = tmp / "catalog.json"
        _write_json(catalog_path, catalog)
        return catalog_path, global_id

    def test_default_invocation_writes_under_staging_root(self) -> None:
        catalog_path, global_id = self._make_catalog(self.tmp)
        code, out, err = _run_main(
            [
                "run",
                "--global-id",
                global_id,
                "--authorize-project",
                "dep",
                "--catalog",
                str(catalog_path),
            ]
        )
        self.assertEqual(code, 0, out + err)
        payload = json.loads(out)
        self.assertEqual(payload["exit_code"], 0)
        self.assertTrue(self.staging_root.exists())
        self.assertFalse(self.production_root.exists())
        self.assertNotIn("NOTICE", err)

    def test_default_invocation_with_authorize_flag_writes_under_production_root(self) -> None:
        catalog_path, global_id = self._make_catalog(self.tmp)
        code, out, err = _run_main(
            [
                "run",
                "--global-id",
                global_id,
                "--authorize-project",
                "dep",
                "--catalog",
                str(catalog_path),
                "--authorize-production-write",
            ]
        )
        self.assertEqual(code, 0, out + err)
        payload = json.loads(out)
        self.assertEqual(payload["exit_code"], 0)
        self.assertTrue(self.production_root.exists())
        self.assertFalse(self.staging_root.exists())
        self.assertIn("NOTICE", err)

    def test_explicit_production_path_without_flag_is_refused(self) -> None:
        catalog_path, global_id = self._make_catalog(self.tmp)
        code, out, err = _run_main(
            [
                "run",
                "--global-id",
                global_id,
                "--authorize-project",
                "dep",
                "--catalog",
                str(catalog_path),
                "--compat-runs-root",
                str(self.production_root),
            ]
        )
        self.assertEqual(code, 4, out + err)
        payload = json.loads(out)
        self.assertEqual(payload["reason"], "compat_runs_root_production_write_not_authorized")
        self.assertFalse(self.production_root.exists())

    def test_explicit_production_path_with_flag_succeeds(self) -> None:
        catalog_path, global_id = self._make_catalog(self.tmp)
        code, out, err = _run_main(
            [
                "run",
                "--global-id",
                global_id,
                "--authorize-project",
                "dep",
                "--catalog",
                str(catalog_path),
                "--compat-runs-root",
                str(self.production_root),
                "--authorize-production-write",
            ]
        )
        self.assertEqual(code, 0, out + err)
        payload = json.loads(out)
        self.assertEqual(payload["exit_code"], 0)
        self.assertTrue(self.production_root.exists())
        self.assertIn("NOTICE", err)

    def test_explicit_non_production_path_still_works_without_flag(self) -> None:
        """Regression: an explicit override to some other, non-production,
        non-staging location continues to work freely with no flag needed
        (unchanged behavior)."""
        catalog_path, global_id = self._make_catalog(self.tmp)
        other_root = self.tmp / "elsewhere" / "runs"
        code, out, err = _run_main(
            [
                "run",
                "--global-id",
                global_id,
                "--authorize-project",
                "dep",
                "--catalog",
                str(catalog_path),
                "--compat-runs-root",
                str(other_root),
            ]
        )
        self.assertEqual(code, 0, out + err)
        payload = json.loads(out)
        self.assertEqual(payload["exit_code"], 0)
        self.assertTrue(other_root.exists())
        self.assertNotIn("NOTICE", err)


class AutoRunAuthorizationTests(unittest.TestCase):
    """authorize-auto-run / revoke-auto-run / is_auto_run_authorized --
    2026-08-26 cross-audit fix: catalog_session_hint.py's SessionStart
    auto-trigger used to satisfy --authorize-project on this project's own
    behalf with no human ever in the loop for that specific decision.
    These are the human-only commands that close it (see the module
    comment above them in check_cross_project_compatibility.py)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-authz-"))
        self.project_root = make_project_dir(self.tmp, "p")
        write_compat_check(self.project_root, [make_check_entry()])

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _marker_path(self) -> Path:
        return self.project_root / ccc.AUTO_RUN_AUTHORIZATION_RELATIVE_PATH

    def test_authorize_writes_a_marker_pinning_the_current_hash(self) -> None:
        code, out, err = _run_main(
            ["authorize-auto-run", "--project-root", str(self.project_root), "--authorized-by", "alice", "--json"]
        )
        self.assertEqual(code, 0, out + err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        marker = json.loads(self._marker_path().read_text(encoding="utf-8"))
        self.assertEqual(marker["schema_version"], ccc.AUTO_RUN_AUTHORIZATION_SCHEMA_VERSION)
        self.assertEqual(marker["authorized_by"], "alice")
        _doc, _reason, current_sha256 = ccc._read_compat_check(self.project_root)
        self.assertEqual(marker["authorized_compat_check_sha256"], current_sha256)
        self.assertTrue(ccc.is_auto_run_authorized(self.project_root)[0])

    def test_authorize_refuses_an_invalid_compat_check_document(self) -> None:
        (self.project_root / "wiki" / "compat-check.json").write_text("not json", encoding="utf-8")
        code, out, err = _run_main(
            ["authorize-auto-run", "--project-root", str(self.project_root), "--authorized-by", "alice", "--quiet"]
        )
        self.assertEqual(code, 4)
        self.assertFalse(self._marker_path().exists())

    def test_authorize_refuses_a_missing_project_root(self) -> None:
        code, out, err = _run_main(
            ["authorize-auto-run", "--project-root", str(self.tmp / "nope"), "--authorized-by", "alice", "--quiet"]
        )
        self.assertEqual(code, 4)

    def test_editing_compat_check_after_authorization_revokes_it(self) -> None:
        _run_main(
            ["authorize-auto-run", "--project-root", str(self.project_root), "--authorized-by", "alice", "--quiet"]
        )
        self.assertTrue(ccc.is_auto_run_authorized(self.project_root)[0])
        write_compat_check(self.project_root, [make_check_entry(check_command=["/bin/echo", "different"])])
        authorized, reason = ccc.is_auto_run_authorized(self.project_root)
        self.assertFalse(authorized)
        self.assertEqual(reason, "auto_run_authorization_hash_mismatch")

    def test_re_authorizing_after_an_edit_pins_the_new_hash(self) -> None:
        _run_main(
            ["authorize-auto-run", "--project-root", str(self.project_root), "--authorized-by", "alice", "--quiet"]
        )
        write_compat_check(self.project_root, [make_check_entry(check_command=["/bin/echo", "different"])])
        code, out, err = _run_main(
            ["authorize-auto-run", "--project-root", str(self.project_root), "--authorized-by", "bob", "--quiet"]
        )
        self.assertEqual(code, 0, out + err)
        self.assertTrue(ccc.is_auto_run_authorized(self.project_root)[0])

    def test_revoke_removes_an_existing_authorization(self) -> None:
        _run_main(
            ["authorize-auto-run", "--project-root", str(self.project_root), "--authorized-by", "alice", "--quiet"]
        )
        code, out, err = _run_main(["revoke-auto-run", "--project-root", str(self.project_root), "--json"])
        self.assertEqual(code, 0, out + err)
        self.assertTrue(json.loads(out)["revoked"])
        self.assertFalse(self._marker_path().exists())
        self.assertFalse(ccc.is_auto_run_authorized(self.project_root)[0])

    def test_revoke_is_idempotent_when_nothing_was_authorized(self) -> None:
        code, out, err = _run_main(["revoke-auto-run", "--project-root", str(self.project_root), "--json"])
        self.assertEqual(code, 0, out + err)
        self.assertFalse(json.loads(out)["revoked"])

    def test_never_authorized_is_unauthorized(self) -> None:
        authorized, reason = ccc.is_auto_run_authorized(self.project_root)
        self.assertFalse(authorized)
        self.assertEqual(reason, "auto_run_authorization_missing")

    def test_missing_compat_check_is_unauthorized(self) -> None:
        (self.project_root / "wiki" / "compat-check.json").unlink()
        authorized, reason = ccc.is_auto_run_authorized(self.project_root)
        self.assertFalse(authorized)
        self.assertEqual(reason, "compat_check_missing")

    def test_symlinked_authorization_dir_component_refused(self) -> None:
        """Same O_NOFOLLOW discipline as _read_compat_check() on the read
        side of wiki/, applied here to .orca/context/."""
        _run_main(
            ["authorize-auto-run", "--project-root", str(self.project_root), "--authorized-by", "alice", "--quiet"]
        )
        outside = self.tmp / "outside-orca-context"
        outside.mkdir()
        shutil.move(str(self.project_root / ".orca" / "context"), str(outside / "context"))
        (self.project_root / ".orca" / "context").symlink_to(outside / "context", target_is_directory=True)
        authorized, reason = ccc.is_auto_run_authorized(self.project_root)
        self.assertFalse(authorized)
        self.assertEqual(reason, "auto_run_authorization_symlink_component")


class AutoRunAuthorizationDirFdHardeningTests(unittest.TestCase):
    """2026-08-26 cross-audit P1: atomic_write_within()'s (and the plain
    final_path.unlink() revoke used to use) os.open(str(path), ...,
    O_NOFOLLOW) only ever guards the FINAL path component -- if auth_dir
    itself (.orca/context) is swapped for a symlink to somewhere OUTSIDE
    project_root in the gap between the earlier _has_symlink_component()
    check and the write/unlink, that check alone cannot stop it: the write
    (or unlink) would silently follow the swapped directory. Simulated
    deterministically (no real thread race needed) by patching
    _has_symlink_component to report "not a symlink" -- exactly what it
    correctly would have reported an instant before the swap -- while
    auth_dir ALREADY is a symlink by the time the write/unlink actually
    happens. Proves the WRITE PATH itself now refuses, not just the earlier
    (already-existing, already-tested) check."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-authz-dirfd-"))
        self.project_root = make_project_dir(self.tmp, "p")
        write_compat_check(self.project_root, [make_check_entry()])
        self.outside = self.tmp / "outside-target"
        self.outside.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _swap_auth_dir_for_symlink_to_outside(self) -> None:
        auth_dir = self.project_root / ".orca" / "context"
        auth_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(auth_dir)
        auth_dir.symlink_to(self.outside, target_is_directory=True)

    def test_authorize_write_refuses_a_racily_symlinked_auth_dir(self) -> None:
        self._swap_auth_dir_for_symlink_to_outside()
        with mock.patch.object(ccc, "_has_symlink_component", return_value=False):
            code, out, err = _run_main(
                [
                    "authorize-auto-run", "--project-root", str(self.project_root),
                    "--authorized-by", "alice", "--quiet",
                ]
            )
        self.assertEqual(code, 4, out + err)
        # The actual exploit this closes: pre-fix, the marker would have
        # been silently written into `outside` (following the symlink)
        # instead of the write refusing outright.
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_revoke_refuses_a_racily_symlinked_auth_dir_and_does_not_touch_the_target(self) -> None:
        code, out, err = _run_main(
            ["authorize-auto-run", "--project-root", str(self.project_root), "--authorized-by", "alice", "--quiet"]
        )
        self.assertEqual(code, 0, out + err)
        marker_bytes = (self.project_root / ccc.AUTO_RUN_AUTHORIZATION_RELATIVE_PATH).read_bytes()
        # A decoy file an attacker's pre-staged `outside` directory might
        # plausibly already contain -- if revoke ever followed the swapped
        # symlink, THIS is the file it would delete instead of refusing.
        decoy = self.outside / ccc.AUTO_RUN_AUTHORIZATION_RELATIVE_PATH.name
        decoy.write_bytes(marker_bytes)
        shutil.rmtree(self.project_root / ".orca" / "context")
        (self.project_root / ".orca" / "context").symlink_to(self.outside, target_is_directory=True)
        with mock.patch.object(ccc, "_has_symlink_component", return_value=False):
            code, out, err = _run_main(
                ["revoke-auto-run", "--project-root", str(self.project_root), "--quiet"]
            )
        self.assertEqual(code, 4, out + err)
        self.assertTrue(decoy.exists(), "revoke must not have followed the symlink and deleted the decoy")


class RequireAutoRunAuthorizationTests(unittest.TestCase):
    """2026-08-26 cross-audit P1 (most severe finding): is_auto_run_
    authorized() used to be reachable ONLY from catalog_session_hint.py's
    own pre-check, never from run_compatibility_checks()/cmd_run() -- the
    one code path that actually executes third-party check_command. That
    left (a) a TOCTOU window between the hook's pre-check and the
    subprocess's own fresh read, and (b) no authorization concept at all
    for a human (or script) invoking `run --authorize-project X` directly.
    --require-auto-run-authorization threads a FRESH, per-project, checked
    immediately-before-Phase-2 re-check into run_compatibility_checks()
    itself."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccc-reqauth-"))
        self.runs_root = self.tmp / "runs"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _authorize(self, project_root: Path) -> None:
        code, out, err = _run_main(
            ["authorize-auto-run", "--project-root", str(project_root), "--authorized-by", "test", "--quiet"]
        )
        self.assertEqual(code, 0, out + err)

    def test_unauthorized_project_is_skipped_not_executed_when_flag_set(self) -> None:
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        marker = self.tmp / "ran.txt"
        write_compat_check(
            dep_dir,
            [
                make_check_entry(
                    depends_on_ref=global_id,
                    check_command=[sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
                )
            ],
        )
        # Deliberately left unauthorized.
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
        result = _run_compat(
            catalog=catalog,
            global_id=global_id,
            authorize_project=["dep"],
            compat_runs_root=self.runs_root,
            require_auto_run_authorization=True,
        )
        self.assertEqual(result["projects"][0]["outcome"], "skipped")
        self.assertEqual(result["projects"][0]["checks"], [])
        codes = [w["code"] for w in result["projects"][0]["warnings"]]
        self.assertIn("auto_run_not_authorized", codes)
        self.assertFalse(marker.exists(), "check_command must not have executed for an unauthorized project")
        # Still produces a normal per-project result entry with its own
        # output file -- a missing authorization is a per-project skip, not
        # a run abort.
        self.assertTrue(Path(result["projects"][0]["output_path"]).exists())

    def test_authorized_project_still_executes_when_flag_set(self) -> None:
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        write_compat_check(
            dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        self._authorize(dep_dir)
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
        result = _run_compat(
            catalog=catalog,
            global_id=global_id,
            authorize_project=["dep"],
            compat_runs_root=self.runs_root,
            require_auto_run_authorization=True,
        )
        self.assertEqual(result["projects"][0]["outcome"], "ok")
        self.assertEqual(result["projects"][0]["checks"][0]["exit_code"], 0)

    def test_one_unauthorized_project_does_not_block_a_second_authorized_one(self) -> None:
        global_id = "t#x"
        bad_dir = make_project_dir(self.tmp, "proj-bad")
        good_dir = make_project_dir(self.tmp, "proj-good")
        write_compat_check(
            bad_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        write_compat_check(
            good_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        self._authorize(good_dir)  # proj-bad deliberately left unauthorized
        row = make_reverse_row(
            in_degree=2,
            referencing_project_ids=["proj-bad", "proj-good"],
            referenced_by=[
                make_edge(project_id="proj-bad", capability_global_id="proj-bad#dep"),
                make_edge(project_id="proj-good", capability_global_id="proj-good#dep"),
            ],
        )
        catalog = make_catalog(
            reverse_index={global_id: row},
            projects=[make_project_row("proj-bad", bad_dir), make_project_row("proj-good", good_dir)],
            verified_at=fresh_iso(),
        )
        result = _run_compat(
            catalog=catalog,
            global_id=global_id,
            authorize_project=["proj-bad", "proj-good"],
            compat_runs_root=self.runs_root,
            require_auto_run_authorization=True,
        )
        by_id = {r["project_id"]: r for r in result["projects"]}
        self.assertEqual(by_id["proj-bad"]["outcome"], "skipped")
        self.assertIn("auto_run_not_authorized", [w["code"] for w in by_id["proj-bad"]["warnings"]])
        self.assertEqual(by_id["proj-good"]["outcome"], "ok")
        self.assertEqual(by_id["proj-good"]["checks"][0]["exit_code"], 0)
        self.assertTrue(Path(by_id["proj-bad"]["output_path"]).exists())
        self.assertTrue(Path(by_id["proj-good"]["output_path"]).exists())

    def test_unauthorized_project_still_executes_when_flag_is_not_set(self) -> None:
        """Human-invoked `run` without the flag is UNCHANGED by design --
        this preserves existing direct-invocation behavior on purpose."""
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        write_compat_check(
            dep_dir, [make_check_entry(depends_on_ref=global_id, check_command=[sys.executable, "-c", "pass"])]
        )
        # Deliberately NOT authorized, and require_auto_run_authorization
        # deliberately omitted (defaults to False).
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
        result = _run_compat(
            catalog=catalog, global_id=global_id, authorize_project=["dep"], compat_runs_root=self.runs_root
        )
        self.assertEqual(result["projects"][0]["outcome"], "ok")
        self.assertEqual(result["projects"][0]["checks"][0]["exit_code"], 0)

    def test_cli_flag_is_threaded_into_run_compatibility_checks(self) -> None:
        """CLI-level proof that argparse --require-auto-run-authorization
        actually reaches run_compatibility_checks() via cmd_run(), not just
        that the pure function respects its own keyword arg (covered
        above)."""
        global_id = "t#x"
        dep_dir = make_project_dir(self.tmp, "dep")
        marker = self.tmp / "ran-cli.txt"
        write_compat_check(
            dep_dir,
            [
                make_check_entry(
                    depends_on_ref=global_id,
                    check_command=[sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
                )
            ],
        )
        catalog = make_affected_catalog(global_id, "dep", projects=[make_project_row("dep", dep_dir)])
        catalog_path = self.tmp / "catalog.json"
        _write_json(catalog_path, catalog)
        code, out, err = _run_main(
            [
                "run", "--global-id", global_id, "--authorize-project", "dep",
                "--catalog", str(catalog_path), "--compat-runs-root", str(self.runs_root),
                "--require-auto-run-authorization",
            ]
        )
        payload = json.loads(out)
        self.assertEqual(payload["projects"][0]["outcome"], "skipped", out + err)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
