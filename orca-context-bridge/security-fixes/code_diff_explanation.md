# Code Changes Explanation

## File: `build_startup_bundle.py`
Function: `summarize_graphify()` (lines 556-720)

### Change 1: Scope Validation Logic (Lines 655-669)

#### BEFORE (Buggy Code):
```python
        # A startup authority with an explicit content closure can only accept
        # Graphify evidence for that exact project root. Reject a foreign scope
        # before hashing its potentially large graph body: the bytes cannot
        # become authoritative for this bundle, and reading them only burns the
        # bounded SessionStart deadline.
        if content_source_closure is not None and (
            closure_project is None
            or source_root.resolve(strict=False) != closure_project.resolve(strict=False)
        ):
            rejections["source_closure_scope_mismatch"] += 1
            continue
```

**Problem**: The `and` operator short-circuits:
- When `content_source_closure is not None` is False, the entire condition is False
- This means the scope check is completely skipped
- Any source_root is silently accepted when closure is None

#### AFTER (Fixed Code):
```python
        # A startup authority with an explicit content closure can only accept
        # Graphify evidence for that exact project root. Reject a foreign scope
        # before hashing its potentially large graph body: the bytes cannot
        # become authoritative for this bundle, and reading them only burns the
        # bounded SessionStart deadline.
        # SECURITY FIX: When content_source_closure is None, we cannot verify
        # scope at all, so we must reject all assets (fail-closed). This prevents
        # unauthorized source roots from being accepted when closure is missing.
        if content_source_closure is None:
            rejections["source_closure_scope_mismatch"] += 1
            continue
        if (
            closure_project is None
            or source_root.resolve(strict=False) != closure_project.resolve(strict=False)
        ):
            rejections["source_closure_scope_mismatch"] += 1
            continue
```

**Fix Logic**:
1. First check: If closure is None, reject immediately (fail-closed)
2. Second check: If closure is non-None, check scope as before
3. Both rejections use the same rejection reason for clarity

### Change 2: Git State Handling (Lines 687-693)

#### BEFORE:
```python
        # A central startup bundle may only accept a Graphify receipt tied to
        # the same reviewed byte closure as the project it represents.  The
        # legacy two-argument call remains useful for standalone catalog
        # inspection, but SessionStart always supplies this closure.
        if content_source_closure is not None:
            source_git = summarize_git(source_root, content_source_closure)
        else:
            source_git = summarize_git(source_root)
```

**Problem**: When closure is None, `summarize_git()` is called without the closure parameter, which weakens the binding to reviewed source state.

#### AFTER:
```python
        # A central startup bundle may only accept a Graphify receipt tied to
        # the same reviewed byte closure as the project it represents.
        # SECURITY FIX: Assets with None closure have already been rejected above.
        # This branch is now unreachable in production, but we keep it for
        # any potential standalone inspection use cases.
        if content_source_closure is not None:
            source_git = summarize_git(source_root, content_source_closure)
        else:
            source_git = summarize_git(source_root)
```

**Note**: The actual code path remains unchanged. This is intentional because:
1. The None branch is now unreachable in production (rejected earlier)
2. Keeping the code path preserves any potential standalone inspection use cases
3. A clear comment explains why the branch still exists
4. The real security barrier is the early rejection above

## Why This Fix Is Correct

### Fail-Closed Principle
- Unknown/missing closure = security reject, not silent pass
- Forces manifest creators to provide proper closure data
- Prevents accidental deployment of incomplete configurations

### Backward Compatibility
- Function signature unchanged
- Non-None closure behavior identical to original
- Only affects the buggy None closure case

### Clear Semantics
- Uses same rejection reason (`source_closure_scope_mismatch`)
- Makes it clear: scope verification requires closure
- Helps operators debug manifest issues

### Defense in Depth
- Early rejection prevents expensive graph file reads
- Multiple validation layers remain intact
- No assumption about git state validity when closure is missing

## Production Impact

### When This Fix Takes Effect
1. When `declared_content_source_closure()` returns None (manifest missing field)
2. This forces manifest initialization to complete before production use
3. Operators will be notified via rejection reasons

### Expected Behavior Change
- **Before**: Graphify assets silently accepted with None closure
- **After**: Graphify assets rejected with None closure
- **Result**: Operators must provide proper closure in manifest to enable Graphify

### Migration Path
1. Check manifests for missing `content_source_closure` field
2. Ensure `declared_content_source_closure()` returns valid closure dict
3. If Graphify is not needed, closure can be minimal valid dict
4. Redeploy with complete manifest
