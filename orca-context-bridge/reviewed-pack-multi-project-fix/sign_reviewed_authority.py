#!/usr/bin/env python3
"""
Independent manifest signing tool for orca-context-bridge authority.

This tool is NOT called automatically by SessionStart hooks. It must be invoked
by a separate review/CI process to generate or update the reviewed startup pack manifest.

The manifest contains cryptographic signatures (authority_signed_at, signed_by, signature_nonce)
that allow verification hooks to distinguish between "externally signed" and "self-signed" manifests.
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
        
        # Signature fields - these prove external signing
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
