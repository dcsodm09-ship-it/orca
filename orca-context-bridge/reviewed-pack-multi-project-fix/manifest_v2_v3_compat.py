#!/usr/bin/env python3
"""
Manifest v2 to v3 Compatibility Layer

This module handles the transition from v2 manifests (using 'project_git' field)
to v3 manifests (using 'authority_git' field) for the reviewed startup pack.

Key Design:
- v2 manifests stored a single git state in "project_git" field
- v3 manifests separate into "authority_git" (knowledge_root) and "project_git" (caller)
- Backward compatibility: v3 code can read and validate v2 manifests
- Forward warning: v2 manifest usage prints warning, encouraging upgrade
"""

from typing import Any, Optional
import sys


class ManifestVersion:
    """Constants for manifest versions."""
    V2 = 2
    V3 = 3
    LATEST = V3


class ManifestCompatibilityError(ValueError):
    """Raised when manifest cannot be read even with compatibility layer."""
    pass


def detect_manifest_version(payload: dict[str, Any]) -> int:
    """Detect manifest version based on fields present.
    
    Args:
        payload: Parsed manifest JSON
        
    Returns:
        ManifestVersion.V2 or ManifestVersion.V3
        
    Raises:
        ManifestCompatibilityError: If manifest is neither v2 nor v3
    """
    if payload is None:
        raise ManifestCompatibilityError("manifest payload is None")
    
    has_authority_git = "authority_git" in payload
    has_project_git = "project_git" in payload
    
    # v3: must have authority_git (project_git is optional for compat)
    if has_authority_git:
        return ManifestVersion.V3
    
    # v2: has project_git but no authority_git
    if has_project_git and not has_authority_git:
        return ManifestVersion.V2
    
    # Invalid: has neither
    if not has_project_git and not has_authority_git:
        raise ManifestCompatibilityError(
            "manifest missing both 'project_git' (v2) and 'authority_git' (v3) fields"
        )
    
    # Should not reach here
    raise ManifestCompatibilityError("manifest version detection failed")


def get_authority_git(payload: dict[str, Any], emit_warning: bool = True) -> Optional[Any]:
    """Get the authority git state from manifest, handling v2→v3 transition.
    
    Args:
        payload: Parsed manifest JSON
        emit_warning: If True, print warning for v2 manifests to stderr
        
    Returns:
        Authority git state dict, or None if not present
        
    Raises:
        ManifestCompatibilityError: If manifest is malformed
    """
    if payload is None:
        raise ManifestCompatibilityError("manifest payload is None")
    
    version = detect_manifest_version(payload)
    
    if version == ManifestVersion.V3:
        # v3: authority_git is authoritative
        return payload.get("authority_git")
    
    elif version == ManifestVersion.V2:
        # v2 fallback: use project_git as authority_git
        # This works because v2 stored knowledge_root state in project_git
        if emit_warning:
            print(
                "⚠️  Manifest is v2 format (using 'project_git' field for authority validation). "
                "Please upgrade to v3 format with 'authority_git' field for clearer semantics.",
                file=sys.stderr
            )
        return payload.get("project_git")
    
    else:
        raise ManifestCompatibilityError(f"unknown manifest version: {version}")


def validate_manifest_schema(payload: dict[str, Any], strict: bool = False) -> tuple[bool, Optional[str]]:
    """Validate manifest schema against v2 or v3 spec.
    
    Args:
        payload: Parsed manifest JSON
        strict: If True, require v3 schema (reject v2). If False, accept both.
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if payload is None:
        return False, "manifest payload is None"
    
    # Common required fields in both v2 and v3
    required_common = {"schema_version", "authority", "pack", "shared_source_sha256s", "shared_source_policy"}
    
    if not required_common.issubset(set(payload.keys())):
        missing = required_common - set(payload.keys())
        return False, f"manifest missing required fields: {missing}"
    
    try:
        version = detect_manifest_version(payload)
    except ManifestCompatibilityError as e:
        return False, str(e)
    
    if strict and version != ManifestVersion.V3:
        return False, f"strict mode requires v3 manifest, got v{version}"
    
    # Validate git field(s)
    if version == ManifestVersion.V2:
        if not isinstance(payload.get("project_git"), (dict, type(None))):
            return False, "v2 manifest: 'project_git' must be dict or null"
    
    elif version == ManifestVersion.V3:
        if not isinstance(payload.get("authority_git"), (dict, type(None))):
            return False, "v3 manifest: 'authority_git' must be dict or null"
        # project_git is optional in v3
        if "project_git" in payload and not isinstance(payload.get("project_git"), (dict, type(None))):
            return False, "v3 manifest: optional 'project_git' must be dict or null"
    
    return True, None


def upgrade_manifest_fields(payload_v2: dict[str, Any]) -> dict[str, Any]:
    """Upgrade v2 manifest fields to v3 naming.
    
    This does NOT change schema_version or perform full upgrade;
    it only renames fields for intermediate compatibility.
    
    Args:
        payload_v2: v2 manifest dict
        
    Returns:
        New dict with v3 field names
    """
    payload_v3 = dict(payload_v2)
    
    if "project_git" in payload_v3 and "authority_git" not in payload_v3:
        # Rename project_git to authority_git
        payload_v3["authority_git"] = payload_v3.pop("project_git")
    
    return payload_v3


def migration_guidance_v2_to_v3() -> str:
    """Return human-readable guidance for migrating from v2 to v3."""
    return """
Manifest Migration from v2 to v3
================================

Current Status:
  - Your manifest uses v2 format with 'project_git' field
  - This is still supported but deprecated

What Changed:
  - v2: Single 'project_git' field stored knowledge_root state
  - v3: Separate fields for clarity:
    - 'authority_git': knowledge_root state (required for verification)
    - 'project_git': caller project state (optional, for logging)

How to Migrate:
  1. Rebuild the manifest from scratch:
     $ python3 build_startup_bundle.py [project_path]
  
  2. The new manifest will be v3 format
  
  3. Verify the new manifest works:
     $ python3 -c "from build_startup_bundle import verify_reviewed_pack; ..."
  
  4. Delete the old v2 manifest (if desired)

Timeline:
  - v2 manifests continue to work through 2026-12-31
  - v2 support may be removed in future versions
  - Migration is recommended but not urgent
"""


if __name__ == "__main__":
    # Example usage
    import json
    
    # Test v2 manifest
    v2_manifest = {
        "schema_version": 2,
        "authority": "reviewed-pack-v2",
        "pack": {"path": "pack.json", "sha256": "abc123", "size_bytes": 1000},
        "shared_source_sha256s": {"wiki": "def456"},
        "shared_source_policy": {"wiki": {"required": True, "reason": None}},
        "project_git": {"available": True, "head": "abc123", "status_sha256": "def456"}
    }
    
    print("v2 Manifest Detection:")
    version = detect_manifest_version(v2_manifest)
    print(f"  Detected version: {version}")
    
    print("\nAuthority Git Retrieval:")
    auth_git = get_authority_git(v2_manifest, emit_warning=False)
    print(f"  Authority git: {auth_git}")
    
    print("\nSchema Validation:")
    valid, error = validate_manifest_schema(v2_manifest, strict=False)
    print(f"  Valid (non-strict): {valid}")
    
    valid_strict, error_strict = validate_manifest_schema(v2_manifest, strict=True)
    print(f"  Valid (strict v3): {valid_strict}")
    if error_strict:
        print(f"    Error: {error_strict}")
    
    print("\nField Upgrade (v2→v3):")
    v3_dict = upgrade_manifest_fields(v2_manifest)
    print(f"  Has authority_git now: {'authority_git' in v3_dict}")
    print(f"  Has project_git now: {'project_git' in v3_dict}")
