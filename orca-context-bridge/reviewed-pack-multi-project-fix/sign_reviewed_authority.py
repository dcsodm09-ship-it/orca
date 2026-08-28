#!/usr/bin/env python3
"""
Provenance-labelling tool for the orca-context-bridge authority manifest.

This tool is NOT called automatically by SessionStart hooks. It must be invoked
by a separate review/CI process to generate or update the reviewed startup pack manifest.

WHAT THIS DOES NOT DO (corrected 2026-08-28; this docstring previously claimed
the opposite and contradicted `SKILL.md` -> "Authority Signing and Freshness"):

    The three fields this tool writes -- `authority_signed_at`, `signed_by`,
    and `signature_nonce` -- are NOT cryptographic signatures, and they do NOT
    let a verifier distinguish an externally signed manifest from a self-signed
    one. Look at what is actually produced below:

        authority_signed_at = datetime.now(timezone.utc).isoformat()
        signed_by           = <the reviewer-id string the caller passed in>
        signature_nonce     = secrets.token_hex(16)

    There is no key, no HMAC, no signature value, and nothing to verify any of
    them against. A random nonce with no key authenticates nothing: anyone able
    to write the manifest can write these three fields too, with any values they
    like. They are provenance *labels* -- useful for a human reading the file to
    see who claims to have reviewed it and when -- and nothing more.

    They are also, today, read by nothing. Neither the deployed verifier
    (`scripts/build_startup_bundle.py::verify_reviewed_pack`) nor the deployed
    SessionStart hook (`scripts/startup_context.py`) reads any of the three;
    the only reader anywhere is `scripts/build_knowledge_graph.py`, which copies
    `authority_signed_at` into a graph node as display metadata marked
    `reference_only`. Re-verified by grep over the deployed tree, 2026-08-28:
    `signed_by` 0 hits, `signature_nonce` 0 hits, `authority_signed_at` 1
    cosmetic hit.

    The consequence is a real, still-open gap and is recorded as such: the
    manifest is the trust anchor -- it pins the pack hash, the shared-source
    hashes and the authority git state -- but the manifest's own bytes are
    unauthenticated. Whoever can write it can simply re-pin every hash to
    whatever they just installed and verification still passes.

    Closing that needs a real keyed MAC over the manifest's canonical JSON,
    with the key held outside the manifest's own directory. That is a design
    task, deliberately not attempted here. Starting to *read* these existing
    fields would not close it and would be worse than leaving them alone,
    because it would create the appearance of a check that still authenticates
    nothing.

Note also that this tool emits `schema_version: 3` while the deployed verifier
is v2 and rejects it, and it has no file-write path of its own -- its only
current use is producing values a human hand-copies into the manifest. See
`SKILL.md` -> "Re-signing reviewed-startup-pack-manifest.json" for the
procedure actually in use.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# These would be imported from build_startup_bundle in a real scenario
# For now, we'll assume they're available or define minimal implementations


def git_state_summary(repo_path: Path) -> dict[str, Any]:
    """Get git state of a repository (head hash, status)."""
    import subprocess
    import hashlib
    
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        
        status = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        
        status_sha256 = hashlib.sha256(status.encode()).hexdigest()
        
        return {
            "available": True,
            "head": head,
            "status_sha256": status_sha256,
        }
    except Exception as e:
        return {
            "available": False,
            "error": str(e),
        }


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    """Load existing manifest for update."""
    if not manifest_path.exists():
        return {}
    
    try:
        with open(manifest_path) as f:
            return json.load(f)
    except Exception:
        return {}


def sign_authority(
    knowledge_root: Path,
    expected_root: Path | None,
    reviewer_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Sign or update the central reviewed authority manifest.
    
    Args:
        knowledge_root: Path to knowledge root (usually Orca project)
        expected_root: Expected SSD root for path validation
        reviewer_id: Identifier for the reviewer/signer (default: current user)
        dry_run: If True, print manifest preview without writing
    
    Returns:
        Result dict with status and manifest_hash if successful
    """
    
    # Get current reviewer identity
    if reviewer_id is None:
        reviewer_id = os.getenv("USER", "unknown-reviewer")
    
    # Get git state of knowledge root
    git_state = git_state_summary(knowledge_root)
    if not git_state.get("available"):
        return {
            "status": "failed",
            "reason": f"Knowledge root git state unavailable: {git_state.get('error')}",
        }
    
    # Generate timestamp and nonce
    timestamp = datetime.now(timezone.utc).isoformat()
    nonce = secrets.token_hex(16)
    
    # Load existing manifest or create new one
    manifest_path = knowledge_root / ".orca" / "context" / "reviewed-startup-pack-manifest.json"
    existing = load_manifest(manifest_path)
    
    # Create/update manifest with signature info
    manifest = {
        # Original fields (these would come from existing manifest in real usage)
        "schema_version": 3,
        "authority": "orca-central-reviewed-l1-l3",
        
        # Authority git state - this is what will be verified by hooks
        "authority_git": git_state,
        
        # Provenance labels. These prove NOTHING on their own -- unkeyed and
        # read by no deployed verifier. See the module docstring.
        "authority_signed_at": timestamp,
        "signed_by": reviewer_id,
        "signature_nonce": nonce,
        
        # These fields would be preserved from existing manifest
        # In a real scenario, we'd copy: "pack", "shared_source_sha256s", "shared_source_policy", 
        # "content_source_closure", etc.
        **{k: v for k, v in existing.items() if k not in [
            "schema_version", "authority", "authority_git", 
            "authority_signed_at", "signed_by", "signature_nonce"
        ]}
    }
    
    # Calculate manifest hash
    import hashlib
    manifest_json = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    manifest_hash = hashlib.sha256(manifest_json.encode()).hexdigest()
    
    if dry_run:
        print("=" * 60)
        print("DRY-RUN: Preview of new manifest")
        print("=" * 60)
        print(json.dumps(manifest, indent=2))
        print()
        print(f"Manifest SHA-256: {manifest_hash}")
        print()
        print("NOTE: This is a preview only. Manifest was NOT written to disk.")
        print("To apply these changes, run without --dry-run after review.")
        return {
            "status": "dry-run",
            "manifest_hash": manifest_hash,
            "reviewer": reviewer_id,
            "timestamp": timestamp,
        }
    else:
        # In a real scenario, we would write to manifest_path
        # For safety in this candidate implementation, we just print the preview
        print("=" * 60)
        print("Generated Authority Manifest")
        print("=" * 60)
        print(json.dumps(manifest, indent=2))
        print()
        print(f"Manifest SHA-256: {manifest_hash}")
        print()
        print(f"Reviewer: {reviewer_id}")
        print(f"Timestamp: {timestamp}")
        print(f"Nonce: {nonce}")
        print()
        print("WARNING: This manifest must be reviewed by authorized personnel before installation.")
        print("To install: Copy manifest to .orca/context/reviewed-startup-pack-manifest.json")
        print("           and run install_shared.py --update with the new manifest hash.")
        
        return {
            "status": "ready_for_installation",
            "manifest_hash": manifest_hash,
            "manifest": manifest,
            "reviewer": reviewer_id,
            "timestamp": timestamp,
        }


def main():
    parser = argparse.ArgumentParser(
        description="Sign or update the orca-context-bridge central reviewed authority manifest."
    )
    parser.add_argument(
        "--knowledge-root",
        type=Path,
        required=True,
        help="Path to knowledge root (usually Orca project directory)",
    )
    parser.add_argument(
        "--expected-root",
        type=Path,
        default=None,
        help="Expected SSD root for path validation (optional)",
    )
    parser.add_argument(
        "--reviewer-id",
        type=str,
        default=None,
        help="Identifier for reviewer/signer (default: current user)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview manifest without writing",
    )
    
    args = parser.parse_args()
    
    result = sign_authority(
        knowledge_root=args.knowledge_root,
        expected_root=args.expected_root,
        reviewer_id=args.reviewer_id,
        dry_run=args.dry_run,
    )
    
    if result.get("status") == "failed":
        print(f"Error: {result.get('reason')}", file=__import__("sys").stderr)
        exit(1)
    else:
        exit(0)


if __name__ == "__main__":
    main()
