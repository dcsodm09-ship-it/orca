#!/usr/bin/env python3
"""Check whether wiki/orca-context-wiki.json still matches its signed pin.

Lifts out, as a standalone CLI callable at any moment (not just during a
full startup-bundle build), the single check verify_reviewed_pack() in
build_startup_bundle.py already performs for the "wiki" shared source: does
sha256(wiki/orca-context-wiki.json) still equal
shared_source_sha256s.wiki inside the manually re-signed
.orca/context/reviewed-startup-pack-manifest.json?

This is the detector for the exact failure mode that made SessionStart
fail closed for hours after an unguarded wiki edit (see
reports/MEMORY-GRAPH-WIKI-AUDIT-3-FRESHNESS-MECHANISM-2026-08-20.md): the
wiki is untracked in git, so no commit hook ever sees it, and the only
freshness signal is this content hash. Running this checker after any wiki
edit surfaces "stale" in seconds instead of waiting for the next
SessionStart hook run.

Deliberately scoped to the schema the LIVE hook actually verifies today.
The deployed ~/.agents/skills/orca-context-bridge/scripts/build_startup_bundle.py
(the module the SessionStart hook's --expected-generator-sha256 pin
actually matches) is schema v2, where shared_source_sha256s.wiki is a
plain 64-hex-character string. The workspace-tracked copy of the same
filename next to this script is schema v3/v4 -- a different, unscheduled
migration where that same key becomes an object
{sha256, content_version, pinned_at}. This checker refuses to guess at
that object shape (see SUPPORTED_MANIFEST_SCHEMA_VERSIONS below and the
"wiki pin is the object form" cannot_check reason) rather than silently
reporting "fresh" against a shape it does not understand.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Sibling import of the copy co-located with this script (workspace-tracked,
# schema v3/v4), used ONLY for the five behaviour-identical helpers below.
# REVIEWED_PACK_MANIFEST_SCHEMA_VERSION is deliberately NOT imported from
# it -- see SUPPORTED_MANIFEST_SCHEMA_VERSIONS.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_startup_bundle import (  # noqa: E402
    DEFAULT_CONTEXT_DIR,
    REVIEWED_PACK_MANIFEST_NAME,
    load_json,
    sha256_file,
    valid_sha256,
)


# NOT imported: the workspace-tracked build_startup_bundle.py is schema 3/4
# while the deployed ~/.agents/skills copy that actually runs at
# SessionStart is schema 2. The v3/v4 migration is a separate, unscheduled
# milestone. If that manifest schema is ever bumped in the live file, this
# checker must fail closed (cannot_check) rather than silently keep
# comparing a hash the new schema may no longer store the same way.
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = (2,)


class FreshnessError(ValueError):
    """Raised when the check cannot be performed at all (exit 4)."""


def resolve_default_knowledge_root() -> Path:
    """Infer the knowledge root from this script's own location.

    parents[2] from orca-context-bridge/scripts/check_wiki_freshness.py is
    the knowledge root (.../完善orca). Accepted only if that directory
    actually contains both wiki/orca-context-wiki.json and
    .orca/context/reviewed-startup-pack-manifest.json, so a copy of this
    script later placed somewhere shallower (e.g.
    ~/.agents/skills/orca-context-bridge/scripts/, where parents[2] would
    wrongly land on ~/.agents/skills) fails closed instead of silently
    checking the wrong files.
    """
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "wiki" / "orca-context-wiki.json").is_file() and (
        candidate / DEFAULT_CONTEXT_DIR / REVIEWED_PACK_MANIFEST_NAME
    ).is_file():
        return candidate
    raise FreshnessError(
        f"could not infer the knowledge root from this script's location "
        f"(guessed {candidate}); pass --knowledge-root, or both --wiki and --manifest, explicitly"
    )


def resolve_paths(
    knowledge_root_arg: Path | None, wiki_arg: Path | None, manifest_arg: Path | None
) -> tuple[Path, Path]:
    if wiki_arg is not None and manifest_arg is not None:
        return (
            wiki_arg.expanduser().resolve(strict=False),
            manifest_arg.expanduser().resolve(strict=False),
        )
    knowledge_root = (
        knowledge_root_arg.expanduser().resolve(strict=False)
        if knowledge_root_arg is not None
        else resolve_default_knowledge_root()
    )
    wiki_path = wiki_arg.expanduser().resolve(strict=False) if wiki_arg else knowledge_root / "wiki" / "orca-context-wiki.json"
    manifest_path = (
        manifest_arg.expanduser().resolve(strict=False)
        if manifest_arg
        else knowledge_root / DEFAULT_CONTEXT_DIR / REVIEWED_PACK_MANIFEST_NAME
    )
    return wiki_path, manifest_path


def peek_wiki_content_version(wiki_path: Path) -> int | None:
    """Informational only, never gating. None when meta is absent (e.g. pre-bootstrap)."""
    payload = load_json(wiki_path)
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    version = meta.get("content_version")
    if isinstance(version, int) and not isinstance(version, bool):
        return version
    return None


def check_freshness(wiki_path: Path, manifest_path: Path) -> dict[str, Any]:
    """Return a result dict; never raises for an ordinary cannot_check case.

    result["result"] is one of "fresh", "stale", "cannot_check".
    """
    base: dict[str, Any] = {
        "wiki_path": str(wiki_path),
        "manifest_path": str(manifest_path),
        "manifest_schema_version": None,
        "pinned_sha256": None,
        "observed_sha256": None,
        "wiki_content_version": peek_wiki_content_version(wiki_path),
    }

    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        return {**base, "result": "cannot_check", "reason": f"manifest at {manifest_path} is missing or not a parseable JSON object"}

    schema_version = manifest.get("schema_version")
    base["manifest_schema_version"] = schema_version
    if schema_version not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
        return {
            **base,
            "result": "cannot_check",
            "reason": f"manifest schema {schema_version!r} out of scope for this checker",
        }

    shared = manifest.get("shared_source_sha256s")
    if not isinstance(shared, dict):
        return {**base, "result": "cannot_check", "reason": "manifest is missing shared_source_sha256s"}

    pinned = shared.get("wiki")
    if isinstance(pinned, dict):
        return {
            **base,
            "result": "cannot_check",
            "reason": (
                "wiki pin is the object form {sha256, content_version, pinned_at}; "
                "that shape belongs to the deferred v3/v4 milestone and this checker "
                "does not interpret it"
            ),
        }
    if not valid_sha256(pinned):
        return {**base, "result": "cannot_check", "reason": "shared_source_sha256s.wiki is missing or not a valid sha256 hex string"}
    base["pinned_sha256"] = pinned

    observed = sha256_file(wiki_path)
    if observed is None:
        return {**base, "result": "cannot_check", "reason": f"wiki file at {wiki_path} is unreadable"}
    base["observed_sha256"] = observed

    # Plain equality is deliberate, matching the deployed verify_reviewed_pack's
    # own comparison at this point: these are public content digests, not
    # secrets, so there is no timing-side-channel concern that would call
    # for secrets.compare_digest.
    if observed == pinned:
        return {**base, "result": "fresh", "reason": None}
    return {**base, "result": "stale", "reason": "observed sha256 does not match the pinned shared_source_sha256s.wiki"}


STALE_NOTICE_TEMPLATE = """\
pinned   shared_source_sha256s.wiki = {pinned}
observed sha256(wiki/orca-context-wiki.json) = {observed}
The wiki has been edited since the manifest was last signed. Re-pin it
with the repo's existing manual manifest re-sign procedure, then re-run
this checker to confirm.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check wiki/orca-context-wiki.json's sha256 against the signed reviewed-pack manifest pin."
    )
    parser.add_argument("--knowledge-root", type=Path, help="override the inferred knowledge root")
    parser.add_argument("--wiki", type=Path, help="override the wiki JSON path")
    parser.add_argument("--manifest", type=Path, help="override the reviewed-pack manifest path")
    parser.add_argument("--json", action="store_true", help="emit a single machine-readable JSON object to stdout")
    parser.add_argument("--quiet", action="store_true", help="suppress human-readable text; rely on the exit code")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        wiki_path, manifest_path = resolve_paths(args.knowledge_root, args.wiki, args.manifest)
    except FreshnessError as exc:
        result = {
            "result": "cannot_check",
            "reason": str(exc),
            "wiki_path": None,
            "manifest_path": None,
            "manifest_schema_version": None,
            "pinned_sha256": None,
            "observed_sha256": None,
            "wiki_content_version": None,
        }
        _emit(args, result)
        return 4

    result = check_freshness(wiki_path, manifest_path)
    _emit(args, result)
    if result["result"] == "fresh":
        return 0
    if result["result"] == "stale":
        return 1
    return 4


def _emit(args: argparse.Namespace, result: dict[str, Any]) -> None:
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.quiet:
        return
    status = result["result"]
    if status == "fresh":
        print(f"fresh: {result['wiki_path']} matches shared_source_sha256s.wiki ({result['observed_sha256']})")
    elif status == "stale":
        print("stale: wiki content does not match the signed manifest pin")
        print(STALE_NOTICE_TEMPLATE.format(pinned=result["pinned_sha256"], observed=result["observed_sha256"]))
    else:
        print(f"cannot_check: {result['reason']}")
    if result.get("wiki_content_version") is not None:
        print(f"wiki meta.content_version (informational, not gating): {result['wiki_content_version']}")


if __name__ == "__main__":
    raise SystemExit(main())
