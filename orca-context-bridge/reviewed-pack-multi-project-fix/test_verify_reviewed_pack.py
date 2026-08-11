#!/usr/bin/env python3
"""
True integration tests that actually call verify_reviewed_pack().

This test suite validates the fix for multi-project context sharing bug
by creating real git repositories and verifying verify_reviewed_pack()
behavior with real function calls and return value assertions.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Add paths for imports
test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir))
sys.path.insert(0, str(test_dir.parent / "scripts"))

import build_startup_bundle_from_prod as bsbfp
import manifest_v2_v3_compat


class TestVerifyReviewedPackIntegration(unittest.TestCase):
    """Integration tests that truly call verify_reviewed_pack() with real repos."""
    
    def setUp(self):
        """Set up temporary directories for each test."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temp_dir.name)
    
    def tearDown(self):
        """Clean up temporary directories."""
        self.temp_dir.cleanup()
    
    def run_cmd(self, cmd: list[str], cwd: Path, check: bool = True) -> Tuple[int, str, str]:
        """Run a command and return (returncode, stdout, stderr)."""
        try:
            result = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False
            )
            return result.returncode, result.stdout, result.stderr
        except Exception as e:
            return -1, "", str(e)
    
    def setup_git_repo(self, repo_path: Path, name: str = "Test User", 
                       email: str = "test@example.com") -> bool:
        """Initialize a git repository with initial commit."""
        repo_path.mkdir(parents=True, exist_ok=True)
        
        cmds = [
            ["git", "init"],
            ["git", "config", "user.email", email],
            ["git", "config", "user.name", name],
            ["git", "config", "commit.gpgsign", "false"],
        ]
        
        for cmd in cmds:
            rc, stdout, stderr = self.run_cmd(cmd, repo_path)
            if rc != 0:
                return False
        
        # Create initial commit
        (repo_path / "initial.txt").write_text("initial content\n")
        rc, _, _ = self.run_cmd(["git", "add", "."], repo_path)
        if rc != 0:
            return False
        
        rc, _, _ = self.run_cmd(["git", "commit", "-m", "Initial commit"], repo_path)
        return rc == 0
    
    def get_git_state(self, repo_path: Path) -> Dict[str, Any]:
        """Get git state dict for a repository."""
        git_dict = bsbfp.summarize_git(repo_path)
        return git_dict
    
    def make_git_change(self, repo_path: Path, filename: str, content: str) -> bool:
        """Make a git commit in repository."""
        (repo_path / filename).write_text(content)
        rc, _, _ = self.run_cmd(["git", "add", "."], repo_path)
        if rc != 0:
            return False
        
        rc, _, _ = self.run_cmd(["git", "commit", "-m", f"Add {filename}"], repo_path)
        return rc == 0
    
    def create_test_manifest(
        self, 
        manifest_dir: Path,
        knowledge_root_git_state: Dict[str, Any],
        pack_content: bytes = b"test pack data",
        include_shared_sources: bool = True,
        manifest_version: int = 3,
        caller_git_state: Optional[Dict[str, Any]] = None
    ) -> Path:
        """
        Create a test manifest file with given git state.
        
        Args:
            manifest_dir: Directory to place manifest (.orca/context)
            knowledge_root_git_state: Git state to record as authority_git
            pack_content: Content for the pack file
            include_shared_sources: Whether to create shared source files
            manifest_version: 2 or 3
            caller_git_state: Optional caller project git state (for v3)
        
        Returns:
            Path to created manifest file
        """
        manifest_dir.mkdir(parents=True, exist_ok=True)
        
        # Create pack file with RELATIVE path in manifest
        pack_filename = "test.pack"
        pack_file = manifest_dir / pack_filename
        pack_file.write_bytes(pack_content)
        pack_sha = bsbfp.sha256_bytes(pack_content)
        
        # Create shared source files
        capabilities_file = manifest_dir / "capabilities.md"
        wiki_file = manifest_dir / "wiki.md"
        graphify_file = manifest_dir / "graphify.json"
        
        capabilities_content = b"# Capabilities\nTest capabilities"
        wiki_content = b"# Wiki\nTest wiki"
        graphify_content = b'{"available": false}'
        
        capabilities_file.write_bytes(capabilities_content)
        wiki_file.write_bytes(wiki_content)
        graphify_file.write_bytes(graphify_content)
        
        capabilities_sha = bsbfp.sha256_bytes(capabilities_content)
        wiki_sha = bsbfp.sha256_bytes(wiki_content)
        graphify_sha = bsbfp.sha256_bytes(graphify_content)
        
        # Extract only the fields that verify_reviewed_pack compares
        # (available, head, status_sha256)
        authority_git_for_manifest = bsbfp.git_authority_state(knowledge_root_git_state)
        
        # Create manifest payload with RELATIVE paths
        authority_git_key = "authority_git" if manifest_version == 3 else "project_git"
        
        manifest_payload = {
            "schema_version": 2,  # Keep schema version at 2
            "authority": "orca-central-reviewed-l1-l3",
            "pack": {
                "path": pack_filename,  # RELATIVE path
                "sha256": pack_sha,
                "size_bytes": len(pack_content)
            },
            "shared_source_sha256s": {
                "capabilities": capabilities_sha,
                "wiki": wiki_sha,
                "graphify_catalog": graphify_sha
            },
            "shared_source_policy": {
                "capabilities": {"required": True, "reason": None},
                "wiki": {"required": True, "reason": None},
                "graphify_catalog": {"required": False, "reason": "Optional for basic tests"}
            },
            authority_git_key: authority_git_for_manifest  # Store only 3 fields
        }
        
        # For v3, also add project_git if caller_git_state provided
        if manifest_version == 3 and caller_git_state:
            caller_git_for_manifest = bsbfp.git_authority_state(caller_git_state)
            manifest_payload["project_git"] = caller_git_for_manifest
        
        manifest_file = manifest_dir / "reviewed-startup-pack-manifest.json"
        manifest_file.write_text(json.dumps(manifest_payload, indent=2))
        
        return manifest_file
    
    def test_01_knowledge_root_git_change_detected(self):
        """
        Test that changes to knowledge_root are detected and cause verification to fail.
        
        Scenario:
        1. Create knowledge_root repo
        2. Record its initial git state in manifest
        3. Modify knowledge_root (new commit)
        4. Call verify_reviewed_pack() with modified root
        5. Assert ValueError is raised with "git freshness mismatch" message
        """
        # Setup knowledge_root
        kr_path = self.temp_root / "knowledge_root"
        self.assertTrue(self.setup_git_repo(kr_path, "KR Bot", "kr@test.com"))
        
        # Get initial state
        initial_git_state = self.get_git_state(kr_path)
        self.assertTrue(initial_git_state.get("available"))
        
        # Create manifest directory structure
        orca_context = kr_path / ".orca" / "context"
        orca_context.mkdir(parents=True, exist_ok=True)
        
        # Create manifest with initial state
        manifest_file = self.create_test_manifest(
            orca_context,
            initial_git_state,
            pack_content=b"original pack content"
        )
        self.assertTrue(manifest_file.exists())
        
        # Now modify knowledge_root
        self.assertTrue(self.make_git_change(kr_path, "modified.txt", "modified content\n"))
        
        # Get modified state
        modified_git_state = self.get_git_state(kr_path)
        
        # Verify that states are different
        self.assertNotEqual(
            initial_git_state.get("head"),
            modified_git_state.get("head"),
            "Git HEAD should have changed after commit"
        )
        
        # Setup test parameters for verify_reviewed_pack()
        kr_git_for_call = modified_git_state  # Caller's git state (doesn't matter for this test)
        shared_sources = {
            "capabilities": orca_context / "capabilities.md",
            "wiki": orca_context / "wiki.md",
            "graphify_catalog": orca_context / "graphify.json"
        }
        graphify = {"available": False}
        
        # Call verify_reviewed_pack() and expect it to raise ValueError
        with self.assertRaises(ValueError) as ctx:
            bsbfp.verify_reviewed_pack(
                knowledge_root=kr_path,
                expected_root=kr_path.parent,
                git=kr_git_for_call,
                shared_source_paths=shared_sources,
                graphify=graphify
            )
        
        # Verify the error message is about git freshness
        self.assertIn("freshness mismatch", str(ctx.exception).lower())
        print("✓ Test 1 passed: Knowledge root git change detected and rejected")
    
    def test_02_caller_project_git_change_ignored(self):
        """
        Test that changes to caller project git state do NOT cause verification to fail.
        
        Scenario:
        1. Create knowledge_root repo
        2. Create caller project repo
        3. Record knowledge_root's initial state as authority_git
        4. Modify caller project (different repo)
        5. Call verify_reviewed_pack()
        6. Assert it succeeds (returns without exception)
        """
        # Setup knowledge_root
        kr_path = self.temp_root / "knowledge_root"
        self.assertTrue(self.setup_git_repo(kr_path, "KR Bot", "kr@test.com"))
        
        # Setup caller project (separate repo)
        caller_path = self.temp_root / "caller_project"
        self.assertTrue(self.setup_git_repo(caller_path, "Caller Bot", "caller@test.com"))
        
        # Get knowledge_root state to record in manifest
        kr_git_state = self.get_git_state(kr_path)
        self.assertTrue(kr_git_state.get("available"))
        
        # Get initial caller state (for informational purposes)
        caller_git_before = self.get_git_state(caller_path)
        
        # Create manifest in knowledge_root
        orca_context = kr_path / ".orca" / "context"
        orca_context.mkdir(parents=True, exist_ok=True)
        
        manifest_file = self.create_test_manifest(
            orca_context,
            kr_git_state,
            caller_git_state=caller_git_before,
            manifest_version=3
        )
        self.assertTrue(manifest_file.exists())
        
        # Now modify the CALLER project (not knowledge_root)
        self.assertTrue(self.make_git_change(caller_path, "caller_change.txt", "caller data\n"))
        
        # Get modified caller state
        caller_git_after = self.get_git_state(caller_path)
        
        # Verify caller state changed
        self.assertNotEqual(
            caller_git_before.get("head"),
            caller_git_after.get("head"),
            "Caller git HEAD should have changed"
        )
        
        # Knowledge_root should NOT have changed
        kr_git_unchanged = self.get_git_state(kr_path)
        self.assertEqual(
            kr_git_state.get("head"),
            kr_git_unchanged.get("head"),
            "Knowledge root git HEAD should not have changed"
        )
        
        # Setup parameters for verify_reviewed_pack()
        # Pass the modified caller git state (simulating caller's current state)
        shared_sources = {
            "capabilities": orca_context / "capabilities.md",
            "wiki": orca_context / "wiki.md",
            "graphify_catalog": orca_context / "graphify.json"
        }
        graphify = {"available": False}
        
        # Call verify_reviewed_pack() with modified caller state
        # This should SUCCEED because only knowledge_root matters
        try:
            authority, freshness = bsbfp.verify_reviewed_pack(
                knowledge_root=kr_path,
                expected_root=kr_path.parent,
                git=caller_git_after,  # Pass modified caller state
                shared_source_paths=shared_sources,
                graphify=graphify
            )
            # Success! No exception was raised.
            self.assertIsNotNone(authority)
            self.assertIsNotNone(freshness)
            self.assertIn("authority_git", authority)
            print("✓ Test 2 passed: Caller project git change ignored, verification succeeded")
        except ValueError as e:
            self.fail(f"verify_reviewed_pack() should have succeeded but raised: {e}")
    
    def test_03_v2_manifest_compatibility(self):
        """
        Test that v2 manifests (using 'project_git' field) work correctly.
        
        Scenario:
        1. Create knowledge_root repo
        2. Create v2-style manifest with 'project_git' field (not 'authority_git')
        3. Call verify_reviewed_pack()
        4. Assert it succeeds and correctly reads the v2 field
        """
        # Setup knowledge_root
        kr_path = self.temp_root / "knowledge_root"
        self.assertTrue(self.setup_git_repo(kr_path, "KR Bot", "kr@test.com"))
        
        kr_git_state = self.get_git_state(kr_path)
        self.assertTrue(kr_git_state.get("available"))
        
        # Create manifest using v2 format
        orca_context = kr_path / ".orca" / "context"
        orca_context.mkdir(parents=True, exist_ok=True)
        
        manifest_file = self.create_test_manifest(
            orca_context,
            kr_git_state,
            manifest_version=2  # Use v2 format
        )
        self.assertTrue(manifest_file.exists())
        
        # Verify manifest has 'project_git' field
        manifest_payload = json.loads(manifest_file.read_text())
        self.assertIn("project_git", manifest_payload, "v2 manifest should have 'project_git'")
        self.assertNotIn("authority_git", manifest_payload, "v2 manifest should not have 'authority_git'")
        
        # Setup parameters
        shared_sources = {
            "capabilities": orca_context / "capabilities.md",
            "wiki": orca_context / "wiki.md",
            "graphify_catalog": orca_context / "graphify.json"
        }
        graphify = {"available": False}
        
        # Call verify_reviewed_pack() with v2 manifest
        # Should succeed and read 'project_git' as authority_git for v2 compat
        try:
            authority, freshness = bsbfp.verify_reviewed_pack(
                knowledge_root=kr_path,
                expected_root=kr_path.parent,
                git=kr_git_state,
                shared_source_paths=shared_sources,
                graphify=graphify
            )
            # Success!
            self.assertIsNotNone(authority)
            self.assertIsNotNone(freshness)
            
            # Verify the function correctly handled v2 manifest
            self.assertIn("authority_git", authority)
            # The authority_git should match what was in v2's project_git
            self.assertEqual(
                authority["authority_git"]["head"],
                kr_git_state.get("head")
            )
            print("✓ Test 3 passed: v2 manifest compatibility working correctly")
        except ValueError as e:
            self.fail(f"verify_reviewed_pack() should handle v2 manifest but raised: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
