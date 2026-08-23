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
import sys
import tempfile
import unittest
from pathlib import Path

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


def make_catalog(capabilities=None, reverse_index=None, verified_at="2026-08-23T00:00:00Z") -> dict:
    catalog: dict = {
        "schema_version": 1,
        "verified_at": verified_at,
        "capabilities": capabilities if capabilities is not None else [],
    }
    if reverse_index is not None:
        catalog["capability_reverse_index"] = reverse_index
    return catalog


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ccc.main(argv)
    return code, out.getvalue(), err.getvalue()


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


if __name__ == "__main__":
    unittest.main()
