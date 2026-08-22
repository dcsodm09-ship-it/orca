#!/usr/bin/env python3
"""Unit tests for wiki_edit_guard.py's guard_wiki_write() contract.

Covers: bootstrap accept/reject, semantic bump required, timestamp-only
must not bump, backwards timestamp, bool-as-int rejection, future
timestamp, meta-as-laundering-channel rejection, resign_required_for()'s
byte-comparison semantics, and the scoped (not location-blind) bookkeeping
field exclusion -- see the "Round-2 regressions" tests in SemanticDiffTests
and the round-2 comment on test_timestamp_only_write_with_unchanged_version_accepted.

Run with: /usr/bin/python3 -m unittest orca-context-bridge/scripts/test_wiki_edit_guard.py -v
(from the knowledge root), or plain `/usr/bin/python3 test_wiki_edit_guard.py`
from this directory.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wiki_edit_guard import (  # noqa: E402
    compute_semantic_diff,
    guard_wiki_write,
    main,
    parse_timestamp,
    resign_required_for,
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _base_no_meta() -> dict:
    return {
        "version": 1,
        "project": {"path": "/x"},
        "pages": [{"id": "a", "title": "A", "path": "a.md", "summary": "s", "status": "live"}],
        "links": [],
    }


def _base_with_meta(content_version: int = 1, updated_at: str | None = None) -> dict:
    payload = _base_no_meta()
    payload = {
        "version": payload["version"],
        "meta": {"content_version": content_version, "updated_at": updated_at or _now()},
        "project": payload["project"],
        "pages": payload["pages"],
        "links": payload["links"],
    }
    return payload


class ParseTimestampTests(unittest.TestCase):
    def test_z_suffix_accepted(self) -> None:
        parsed = parse_timestamp("2026-08-22T03:26:13Z")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.utcoffset(), timedelta(0))

    def test_naive_rejected(self) -> None:
        self.assertIsNone(parse_timestamp("2026-08-22T03:26:13"))

    def test_non_str_rejected(self) -> None:
        self.assertIsNone(parse_timestamp(12345))
        self.assertIsNone(parse_timestamp(None))


class SemanticDiffTests(unittest.TestCase):
    def test_no_meta_vs_meta_with_only_control_fields_is_non_semantic(self) -> None:
        old = _base_no_meta()
        new = copy.deepcopy(old)
        new = {
            "version": new["version"],
            "meta": {"content_version": 1, "updated_at": _now()},
            "project": new["project"],
            "pages": new["pages"],
            "links": new["links"],
        }
        self.assertFalse(compute_semantic_diff(old, new))

    def test_meta_laundering_other_key_is_semantic(self) -> None:
        old = _base_no_meta()
        new = copy.deepcopy(old)
        new["meta"] = {"content_version": 1, "updated_at": _now(), "smuggled": "haha"}
        self.assertTrue(compute_semantic_diff(old, new))

    def test_page_addition_is_semantic(self) -> None:
        old = _base_with_meta()
        new = copy.deepcopy(old)
        new["pages"].append({"id": "b", "title": "B", "path": "b.md", "summary": "s2", "status": "live"})
        self.assertTrue(compute_semantic_diff(old, new))

    def test_page_order_change_is_semantic(self) -> None:
        old = _base_with_meta()
        old["pages"] = [
            {"id": "a", "title": "A", "path": "a.md", "summary": "s", "status": "live"},
            {"id": "b", "title": "B", "path": "b.md", "summary": "s2", "status": "live"},
        ]
        new = copy.deepcopy(old)
        new["pages"] = list(reversed(new["pages"]))
        self.assertTrue(compute_semantic_diff(old, new))

    def test_key_order_within_object_is_not_semantic(self) -> None:
        old = _base_with_meta()
        new = {
            "links": old["links"],
            "pages": old["pages"],
            "project": old["project"],
            "meta": old["meta"],
            "version": old["version"],
        }
        self.assertFalse(compute_semantic_diff(old, new))

    def test_unknown_field_defaults_semantic(self) -> None:
        old = _base_with_meta()
        new = copy.deepcopy(old)
        new["pages"][0]["source_missing"] = True
        self.assertTrue(compute_semantic_diff(old, new))

    def test_known_bookkeeping_field_is_stripped(self) -> None:
        old = _base_with_meta()
        new = copy.deepcopy(old)
        new["pages"][0]["verified_at"] = _now()
        self.assertFalse(compute_semantic_diff(old, new))

    # --- Round-2 regressions: a prior revision stripped the six
    # bookkeeping field names (created_at/updated_at/verified_at/
    # verification_status/source_mtime/migration_sequence) recursively at
    # ANY depth/location, which a dual review caught as a laundering path
    # -- any of those names could be planted inside meta, at the top
    # level, or nested under an unrelated key to change content without
    # triggering a semantic diff. These four cases must now all be
    # semantic; each names the location the earlier revision missed.

    def test_bookkeeping_field_name_inside_meta_is_semantic(self) -> None:
        old = _base_with_meta(content_version=1, updated_at="2026-08-22T00:00:00+00:00")
        new = copy.deepcopy(old)
        new["meta"]["verification_status"] = "totally different"
        self.assertTrue(compute_semantic_diff(old, new))

    def test_bookkeeping_field_name_at_top_level_is_semantic(self) -> None:
        old = _base_with_meta()
        new = copy.deepcopy(old)
        new["created_at"] = _now()
        self.assertTrue(compute_semantic_diff(old, new))

    def test_bookkeeping_field_name_nested_under_unrelated_key_is_semantic(self) -> None:
        old = _base_with_meta()
        new = copy.deepcopy(old)
        new["pages"][0]["summary"] = {"created_at": "smuggled"}
        self.assertTrue(compute_semantic_diff(old, new))

    def test_bookkeeping_field_name_inside_link_is_stripped(self) -> None:
        old = _base_with_meta()
        old["links"] = [{"from": "a", "to": "a", "relation": "self"}]
        new = copy.deepcopy(old)
        new["links"][0]["verified_at"] = _now()
        self.assertFalse(compute_semantic_diff(old, new))


class GuardBootstrapTests(unittest.TestCase):
    def test_bootstrap_requires_opt_in(self) -> None:
        old = _base_no_meta()
        new = copy.deepcopy(old)
        new["meta"] = {"content_version": 1, "updated_at": _now()}
        with self.assertRaisesRegex(ValueError, "refuse to guess a baseline"):
            guard_wiki_write(old, new, allow_bootstrap=False)

    def test_bootstrap_accepts_meta_only_addition(self) -> None:
        old = _base_no_meta()
        new = copy.deepcopy(old)
        new = {
            "version": new["version"],
            "meta": {"content_version": 1, "updated_at": _now()},
            "project": new["project"],
            "pages": new["pages"],
            "links": new["links"],
        }
        self.assertIsNone(guard_wiki_write(old, new, allow_bootstrap=True))
        self.assertTrue(resign_required_for(old, new))

    def test_bootstrap_rejects_content_version_not_one(self) -> None:
        old = _base_no_meta()
        new = copy.deepcopy(old)
        new["meta"] = {"content_version": 2, "updated_at": _now()}
        with self.assertRaisesRegex(ValueError, "must be exactly 1"):
            guard_wiki_write(old, new, allow_bootstrap=True)

    def test_bootstrap_rejects_simultaneous_content_change(self) -> None:
        old = _base_no_meta()
        new = copy.deepcopy(old)
        new["meta"] = {"content_version": 1, "updated_at": _now()}
        new["pages"].append({"id": "b", "title": "B", "path": "b.md", "summary": "s2", "status": "live"})
        with self.assertRaisesRegex(ValueError, "must not change page/link content"):
            guard_wiki_write(old, new, allow_bootstrap=True)


class GuardNormalTests(unittest.TestCase):
    def test_semantic_change_requires_version_bump(self) -> None:
        old = _base_with_meta(content_version=1)
        new = copy.deepcopy(old)
        new["pages"].append({"id": "b", "title": "B", "path": "b.md", "summary": "s2", "status": "live"})
        new["meta"]["updated_at"] = _now()
        with self.assertRaisesRegex(ValueError, "advance by exactly 1"):
            guard_wiki_write(old, new)

    def test_semantic_change_with_correct_bump_and_newer_timestamp_accepted(self) -> None:
        old = _base_with_meta(content_version=1, updated_at="2026-08-22T00:00:00+00:00")
        new = copy.deepcopy(old)
        new["pages"].append({"id": "b", "title": "B", "path": "b.md", "summary": "s2", "status": "live"})
        new["meta"] = {"content_version": 2, "updated_at": "2026-08-22T01:00:00+00:00"}
        self.assertIsNone(guard_wiki_write(old, new))
        self.assertTrue(resign_required_for(old, new))

    def test_semantic_change_with_non_newer_timestamp_rejected(self) -> None:
        old = _base_with_meta(content_version=1, updated_at="2026-08-22T00:00:00+00:00")
        new = copy.deepcopy(old)
        new["pages"].append({"id": "b", "title": "B", "path": "b.md", "summary": "s2", "status": "live"})
        new["meta"] = {"content_version": 2, "updated_at": "2026-08-22T00:00:00+00:00"}
        with self.assertRaisesRegex(ValueError, "strictly newer"):
            guard_wiki_write(old, new)

    def test_timestamp_only_write_must_not_bump_version(self) -> None:
        old = _base_with_meta(content_version=1, updated_at="2026-08-22T00:00:00+00:00")
        new = copy.deepcopy(old)
        new["meta"] = {"content_version": 2, "updated_at": "2026-08-22T01:00:00+00:00"}
        with self.assertRaisesRegex(ValueError, "verification passes must not bump content_version"):
            guard_wiki_write(old, new)

    def test_timestamp_only_write_with_unchanged_version_accepted(self) -> None:
        old = _base_with_meta(content_version=1, updated_at="2026-08-22T00:00:00+00:00")
        new = copy.deepcopy(old)
        new["pages"][0]["verified_at"] = "2026-08-22T01:00:00+00:00"
        new["meta"] = {"content_version": 1, "updated_at": "2026-08-22T01:00:00+00:00"}
        self.assertIsNone(guard_wiki_write(old, new))
        # Round-2 regression test: this write is non-SEMANTIC (guard allows
        # it without a version bump) but it still changes the file's raw
        # bytes (meta.updated_at moved), which is exactly what
        # reviewed-startup-pack-manifest.json's pinned sha256 tracks. A
        # prior revision asserted assertFalse here -- that was the P1 a
        # dual review caught: the guard reported "no re-sign needed" for
        # precisely the write that most needed one.
        self.assertTrue(resign_required_for(old, new))

    def test_true_no_op_write_does_not_require_resign(self) -> None:
        old = _base_with_meta(content_version=1, updated_at="2026-08-22T00:00:00+00:00")
        new = copy.deepcopy(old)
        self.assertFalse(resign_required_for(old, new))

    def test_timestamp_only_write_backwards_in_time_rejected(self) -> None:
        old = _base_with_meta(content_version=1, updated_at="2026-08-22T01:00:00+00:00")
        new = copy.deepcopy(old)
        new["meta"] = {"content_version": 1, "updated_at": "2026-08-22T00:00:00+00:00"}
        with self.assertRaisesRegex(ValueError, "must not move backwards"):
            guard_wiki_write(old, new)

    def test_bool_as_content_version_rejected(self) -> None:
        old = _base_with_meta(content_version=1)
        new = copy.deepcopy(old)
        new["meta"]["content_version"] = True
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            guard_wiki_write(old, new)

    def test_future_timestamp_rejected(self) -> None:
        old = _base_with_meta(content_version=1)
        new = copy.deepcopy(old)
        far_future = (datetime.now(timezone.utc) + timedelta(days=365 * 70)).isoformat()
        new["meta"] = {"content_version": 1, "updated_at": far_future}
        with self.assertRaisesRegex(ValueError, "future"):
            guard_wiki_write(old, new)

    def test_old_version_missing_refused(self) -> None:
        old = _base_no_meta()
        old["meta"] = {"updated_at": _now()}  # meta present, but no content_version
        new = copy.deepcopy(old)
        # new must independently satisfy its own content_version/updated_at
        # shape checks (validation order item 3) before the old-side
        # baseline check (item 7) is ever reached.
        new["meta"] = {"content_version": 1, "updated_at": _now()}
        with self.assertRaisesRegex(ValueError, "refuse to guess a baseline"):
            guard_wiki_write(old, new)

    def test_non_dict_payload_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a JSON object"):
            guard_wiki_write([], {"meta": {"content_version": 1, "updated_at": _now()}})
        with self.assertRaisesRegex(ValueError, "must be a JSON object"):
            guard_wiki_write({"meta": {"content_version": 1, "updated_at": _now()}}, [])


class CliRealBytesTests(unittest.TestCase):
    """Exercises main() end-to-end against real temp files -- the library
    functions above only ever see in-memory payloads, so they cannot catch
    a bug that only shows up when comparing against actual on-disk bytes.
    This is exactly what round 2 of a dual review caught missing: the
    library-level resign_required_for() can false-negative when the wiki's
    real on-disk text is not already in canonical
    json.dumps(indent=2, ensure_ascii=False)+"\\n" form -- these tests
    write NON-canonical bytes to disk on purpose and confirm main() still
    gets resign_required right.
    """

    def _run_main(self, wiki_text: str, new_payload: dict, extra_args: list[str] | None = None) -> tuple[int, dict]:
        with tempfile.TemporaryDirectory() as tmp_dir:
            wiki_path = Path(tmp_dir) / "wiki.json"
            wiki_path.write_text(wiki_text, encoding="utf-8")
            new_path = Path(tmp_dir) / "new.json"
            new_path.write_text(json.dumps(new_payload), encoding="utf-8")
            argv = ["--wiki", str(wiki_path), "--new", str(new_path), "--json"]
            argv.extend(extra_args or [])
            buf = io.StringIO()
            with redirect_stdout(buf):
                exit_code = main(argv)
            return exit_code, json.loads(buf.getvalue())

    def test_non_canonical_disk_indent_still_requires_resign_on_payload_identical_write(self) -> None:
        payload = _base_with_meta(content_version=1, updated_at="2026-08-22T00:00:00+00:00")
        # 4-space indent on disk, guard's canonical form is 2-space -- a
        # payload-IDENTICAL --apply changes zero semantic content and even
        # round-trips resign_required_for() to False, but it still rewrites
        # the file's actual bytes.
        non_canonical_text = json.dumps(payload, indent=4, ensure_ascii=False) + "\n"
        # The payload-only approximation gets this wrong (payload-identical
        # write -> it says no resign needed); main() must not rely on it.
        self.assertFalse(resign_required_for(payload, payload))
        exit_code, result = self._run_main(non_canonical_text, payload)
        self.assertEqual(exit_code, 3)
        self.assertTrue(result["resign_required"])

    def test_missing_trailing_newline_on_disk_still_requires_resign(self) -> None:
        payload = _base_with_meta(content_version=1, updated_at="2026-08-22T00:00:00+00:00")
        non_canonical_text = json.dumps(payload, indent=2, ensure_ascii=False)  # no trailing "\n"
        exit_code, result = self._run_main(non_canonical_text, payload)
        self.assertEqual(exit_code, 3)
        self.assertTrue(result["resign_required"])

    def test_canonical_disk_text_with_payload_identical_write_does_not_require_resign(self) -> None:
        payload = _base_with_meta(content_version=1, updated_at="2026-08-22T00:00:00+00:00")
        canonical_text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        exit_code, result = self._run_main(canonical_text, payload)
        self.assertEqual(exit_code, 0)
        self.assertFalse(result["resign_required"])

    def test_non_ascii_escaped_disk_form_still_requires_resign_on_payload_identical_write(self) -> None:
        # A pure-ASCII fixture can't distinguish ensure_ascii=True from
        # False -- they produce identical bytes -- so this uses a page with
        # real non-ASCII content (matching the real wiki, which is full of
        # Chinese text) to actually exercise the difference.
        payload = _base_with_meta(content_version=1, updated_at="2026-08-22T00:00:00+00:00")
        payload["pages"][0]["title"] = "维基页面"
        payload["pages"][0]["summary"] = "这是一段中文摘要，用来验证 ensure_ascii 差异。"
        escaped_text = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
        self.assertIn("\\u", escaped_text)  # sanity: this really is escaped, not the canonical form
        exit_code, result = self._run_main(escaped_text, payload)
        self.assertEqual(exit_code, 3)
        self.assertTrue(result["resign_required"])

    def test_crlf_line_endings_on_disk_still_require_resign_on_payload_identical_write(self) -> None:
        payload = _base_with_meta(content_version=1, updated_at="2026-08-22T00:00:00+00:00")
        canonical_text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        crlf_text = canonical_text.replace("\n", "\r\n")
        with tempfile.TemporaryDirectory() as tmp_dir:
            wiki_path = Path(tmp_dir) / "wiki.json"
            wiki_path.write_bytes(crlf_text.encode("utf-8"))  # raw bytes: write_text would translate on some platforms
            new_path = Path(tmp_dir) / "new.json"
            new_path.write_text(json.dumps(payload), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                exit_code = main(["--wiki", str(wiki_path), "--new", str(new_path), "--json"])
            result = json.loads(buf.getvalue())
        self.assertEqual(exit_code, 3)
        self.assertTrue(result["resign_required"])

    def test_non_canonical_disk_apply_hash_matches_real_disk_bytes(self) -> None:
        payload = _base_with_meta(content_version=1, updated_at="2026-08-22T00:00:00+00:00")
        non_canonical_text = json.dumps(payload, indent=4, ensure_ascii=False) + "\n"
        with tempfile.TemporaryDirectory() as tmp_dir:
            wiki_path = Path(tmp_dir) / "wiki.json"
            wiki_path.write_text(non_canonical_text, encoding="utf-8")
            new_path = Path(tmp_dir) / "new.json"
            new_path.write_text(json.dumps(payload), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                exit_code = main(["--wiki", str(wiki_path), "--new", str(new_path), "--apply", "--json"])
            result = json.loads(buf.getvalue())
            self.assertEqual(exit_code, 3)
            on_disk_sha256 = hashlib.sha256(wiki_path.read_bytes()).hexdigest()
            self.assertEqual(result["observed_sha256"], on_disk_sha256)

    def test_apply_writes_bytes_matching_reported_observed_sha256(self) -> None:
        old = _base_with_meta(content_version=1, updated_at="2026-08-22T00:00:00+00:00")
        new = copy.deepcopy(old)
        new["pages"].append({"id": "b", "title": "B", "path": "b.md", "summary": "s2", "status": "live"})
        new["meta"] = {"content_version": 2, "updated_at": "2026-08-22T01:00:00+00:00"}
        canonical_text = json.dumps(old, indent=2, ensure_ascii=False) + "\n"
        with tempfile.TemporaryDirectory() as tmp_dir:
            wiki_path = Path(tmp_dir) / "wiki.json"
            wiki_path.write_text(canonical_text, encoding="utf-8")
            new_path = Path(tmp_dir) / "new.json"
            new_path.write_text(json.dumps(new), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                exit_code = main(["--wiki", str(wiki_path), "--new", str(new_path), "--apply", "--json"])
            result = json.loads(buf.getvalue())
            self.assertEqual(exit_code, 3)
            on_disk_sha256 = hashlib.sha256(wiki_path.read_bytes()).hexdigest()
            self.assertEqual(result["observed_sha256"], on_disk_sha256)


if __name__ == "__main__":
    unittest.main()
