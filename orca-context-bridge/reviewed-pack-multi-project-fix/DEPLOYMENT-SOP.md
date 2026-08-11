# Hash Reigning & Deployment SOP for orca-context-bridge Fix

## Executive Summary

This document outlines the procedure for deploying the multi-project context sharing fix to production. The fix addresses a critical bug where `verify_reviewed_pack()` was incorrectly comparing the calling project's git state against the knowledge_root's git state, causing all non-"完善orca" projects to fail context verification.

**Key Change**: Separation of `authority_git` (knowledge_root state) from `project_git` (caller project state in manifest).

## Phase 1: Source Code Validation

### 1.1 Code Review Checklist
Before proceeding with hash computation, ensure:

- [ ] Two Python files have been reviewed:
  - `build_startup_bundle.py` - core bundle building logic
  - `startup_context.py` - context injection and verification
  
- [ ] Key changes identified:
  - `verify_reviewed_pack()` now computes authority git state from `knowledge_root`
  - Manifest now contains both `authority_git` (v3) and `project_git` fields (v2 compat)
  - Backward compatibility layer: falls back to `project_git` if `authority_git` missing

- [ ] Test coverage verified:
  - `test_verify_authority_vs_caller_project_git_independence()` passes
  - Real git repositories used in integration tests
  - Both v2 and v3 manifest formats tested

### 1.2 Pre-deployment Syntax Validation
```bash
# Run from /Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/
python3 -m py_compile build_startup_bundle.py startup_context.py
python3 -m pytest test_multi_project_fix.py -v
```

**Success Criteria**: All tests pass, no syntax errors.

## Phase 2: Hash Computation & Multi-Party Verification

### 2.1 Compute SHA-256 Hashes

**Responsible Party**: Release Engineer

```bash
cd /Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/orca-context-bridge/

# Compute hashes for each file
sha256sum build_startup_bundle.py > build_startup_bundle.sha256
sha256sum startup_context.py > startup_context.sha256

# Example output format:
# a1b2c3d4e5f6... (64 hex chars)  build_startup_bundle.py
# f6e5d4c3b2a1... (64 hex chars)  startup_context.py
```

### 2.2 Cross-Party Hash Verification

**Responsible Parties**: Minimum 2 independent reviewers

Each reviewer must independently:

1. Obtain the source files via secure channel
2. Compute SHA-256 hashes locally:
   ```bash
   sha256sum build_startup_bundle.py
   sha256sum startup_context.py
   ```
3. Compare computed hashes against published hashes
4. Sign a verification attestation:
   ```
   Date: 2026-08-12
   Reviewer: [Name]
   build_startup_bundle.py: [MATCH/MISMATCH] - [hash]
   startup_context.py: [MATCH/MISMATCH] - [hash]
   ```

**Abort Condition**: Any hash mismatch → investigate and restart Phase 2.

## Phase 3: Settings Update

### 3.1 Locate Current Settings

The hashes are pinned in:
```
~/.claude/settings.json
```

Look for entries like:
```json
{
  "skills": {
    "orca-context-bridge": {
      "expected-generator-sha256": "OLD_HASH_HERE"
    }
  }
}
```

**Responsible Party**: System Administrator

### 3.2 Atomic Settings Update

⚠️ **CRITICAL**: Perform this as a single atomic operation.

```bash
# Step 1: Backup current settings
cp ~/.claude/settings.json ~/.claude/settings.json.backup-$(date +%Y%m%d-%H%M%S)

# Step 2: Update with new hashes (use editor or jq)
# DO NOT manually edit without validation

# Step 3: Validate JSON syntax
python3 -m json.tool ~/.claude/settings.json > /dev/null && echo "✓ JSON valid" || echo "✗ JSON invalid"

# Step 4: Verify the change took effect
grep "expected-generator-sha256" ~/.claude/settings.json
```

**Validation**: Confirm the new hashes are present and properly formatted.

## Phase 4: Trial Activation & Verification

### 4.1 Trigger a Hook Call

**Responsible Party**: QA or Release Engineer

In a test Claude session or hook context:
```bash
# This will invoke the SessionStart hook with new code
# The hook should load the new build_startup_bundle.py with new hash
export CLAUDE_DEBUG_CONTEXT=1
# Trigger a session that calls the hook
```

### 4.2 Verify Hook Behavior

Check the following:

- [ ] Hook loads without "ORCA_CONTEXT_NACK_V1" errors
- [ ] Non-"完善orca" projects now successfully receive context
- [ ] Context bundle ID matches expected format
- [ ] No "central reviewed authority git freshness mismatch" errors for valid knowledge roots

**Success Criteria**: 
- At least 3 different projects (not "完善orca") verify context successfully
- No new error messages related to git validation
- Performance is acceptable (< 2s for context retrieval)

### 4.3 Rollback Plan (if needed)

If verification fails:

```bash
# Immediate Rollback
cp ~/.claude/settings.json.backup-$(date +%Y%m%d) ~/.claude/settings.json

# Kill any running hook processes
pkill -f "startup_context.py"

# Verify rollback
python3 -m json.tool ~/.claude/settings.json | grep expected-generator-sha256
```

## Phase 5: Post-Deployment Monitoring

### 5.1 Observe Real Usage

- Monitor for any "central reviewed authority git freshness mismatch" errors
- Confirm no regression in other projects
- Check performance metrics (context load time should be unchanged)

### 5.2 Retention & Documentation

Keep the following records:
- Backup settings file: `~/.claude/settings.json.backup-*`
- Hash verification attestations from reviewers
- Hook call logs from trial phase
- Date/time of settings update
- Names of authorized personnel who performed update

These records are needed for future audits and rollback decisions.

## Phase 6: Communication & Sign-Off

### 6.1 Notify Users

Once deployed and verified:
- Message to affected teams: "Context sharing fix deployed. Non-'完善orca' projects now receive shared context."
- Link to this SOP for reference
- Contact point for issues

### 6.2 Update Internal Wiki/Docs

Document:
- Deployment date
- Files changed (with commit SHA if applicable)
- Hash values (for audit trail)
- Known issues or caveats

## Appendix A: Troubleshooting

### Issue: "central reviewed authority git freshness mismatch" on v3 manifests

**Cause**: knowledge_root's git state has drifted (new commits, branch changes)
**Action**: Rebuild the manifest from knowledge_root's current state
**Contact**: Knowledge root maintainer

### Issue: "central reviewed authority git freshness mismatch" using v2 manifest

**Cause**: v2 manifest's "project_git" field doesn't match knowledge_root's current state
**Action**: Upgrade manifest to v3, or rebuild if knowledge_root  legitimately changed
**Contact**: Release engineer or manifest owner

### Issue: Backward compatibility warning printed

**Symptom**: stderr contains "WARNING: Using v2 manifest format"
**Cause**: Normal during transition; v2 manifests are automatically supported
**Action**: Gradually migrate projects to v3 manifests (see Section 3 in main docs)

## Appendix B: v2 to v3 Migration Path

For projects using old v2 manifests:

1. The fix automatically detects v2 format (missing `authority_git` field)
2. Falls back to using `project_git` as authority_git
3. Warning is printed to stderr for visibility
4. No immediate action required, but migration is recommended

To complete migration:
- Rebuild the manifest from the knowledge_root with new code
- New build will produce v3 manifest with `authority_git` field
- Old v2 manifest can be deleted after verification

## Sign-Off Template

```
DEPLOYMENT SIGN-OFF
==================

Date: _______________
Release Engineer: _______________
Administrator: _______________
Reviewer 1 (hash verification): _______________
Reviewer 2 (hash verification): _______________

Pre-deployment Testing Status: PASS / FAIL
Hash Verification Status: PASS / FAIL  
Settings Update Status: PASS / FAIL
Trial Hook Activation Status: PASS / FAIL

Approvals:
[ ] Release Engineer approval
[ ] Administrator approval
[ ] At least 1 reviewer approval

Deployment Timestamp (UTC): _______________
Backup Settings File: ~/.claude/settings.json.backup-_______________
```

---

**Document Version**: 1.0  
**Last Updated**: 2026-08-12  
**Next Review Date**: 2026-09-12
