# Graphify Scope Validation Bug Fix

## Bug Summary

File: `scripts/build_startup_bundle.py`, function `summarize_graphify()` (lines 556-720)

### Problem

When `content_source_closure` parameter is `None`, two security checks are bypassed:

1. **Line 660-665**: Scope validation is completely skipped due to `and` short-circuit
   - When `content_source_closure is not None` evaluates to False, the entire condition is False
   - This means ANY `source_root` will be accepted without checking if it belongs to the declared project

2. **Line 685-688**: Git state binding is weakened
   - `summarize_git(source_root)` is called without `content_source_closure` parameter
   - This means the hash is computed from current git state, not from reviewed closure
   - A dirty source_root will still generate a valid hash

### Impact

- In production, `declared_content_source_closure()` can return None (from manifest)
- When None is passed to `summarize_graphify()`, any Graphify asset from any source_root is accepted
- This violates the principle: "A startup authority can only accept evidence for its declared project root"

### Real World Evidence

- Production knowledge_graph.json uses `content_source_closure=None` path
- Same asset is correctly rejected when `content_source_closure` is non-None (source_closure_scope_mismatch)
- This indicates source_root belongs to different project than closure_project
- Security semantic is inverted: live path is more permissive than candidate path

## Fix Strategy

**Principle**: Fail-closed validation. When `content_source_closure` is None, treat it as "unvetted" mode and reject all assets.

**Rationale**: 
- Only one production call site exists (line 1529-1533)
- Production path already passes content_source_closure
- If it's None from manifest, that's a configuration issue that should be detected
- Rejecting None forces proper initialization of manifests

## Implementation

Modify `summarize_graphify()` function:

1. When `content_source_closure is None`:
   - For each asset, set `source_closure_scope_mismatch` rejection reason
   - This is semantically correct: without a verified closure, we cannot verify scope

2. Keep all other logic unchanged:
   - Non-None closure behavior remains identical
   - All other validation checks remain in place
   - Hash validations remain in place

This approach:
- Doesn't change function signature (backward compatible)
- Clearly communicates the issue (rejection reason shows "scope_mismatch")
- Forces callers to provide closure for production use
- Preserves any standalone inspection use cases with explicit understanding
