# Orca Agent Memory safe-parent APFS SSD compatibility

Status: **local safety candidate verified**. No production venv, store, hook,
manifest, capability, service, or launchd state was written or activated.

## Five live gates

The approved boundary was `/Volumes/Extreme SSD`; its live root metadata was
`root:wheel 0775`. All five gates passed before source work:

- UUID: exact match `6EF3720E-C57F-4352-BA47-CFE88BFEFDA7`
- filesystem: `APFS`
- FileVault: `Yes`
- locked: `No`
- owners: `Enabled`

The next authority directory `/Volumes/Extreme SSD/Orca` was independently
`www1adwawd:staff 0700`.

## Design

`safe_parent.py` is the single parent-chain validator used by the provider CLI,
local model, and embedding lock lanes. It accepts a stop boundary only when:

1. process-start configuration supplies both an exact absolute normalized root
   and a canonical UUID; neither value is hard-coded in the generic library;
2. the root and every target-side parent are non-symlink directories with an
   allowed owner;
3. every directory below the root rejects group/world write and stays on the
   same device as the approved root;
4. the exact root is not world writable (its observed `0775` group-write bit is
   the only exceptional mode bit);
5. a fresh absolute `/usr/sbin/diskutil info -plist <root>` proves exact mount
   point, UUID, APFS, FileVault enabled, unlocked, and global owners enabled.

If the pair is absent, the original walk-to-filesystem-root behavior remains.
If the pair is incomplete, malformed, stale, mismatched, or its live proof is
unavailable, the operation fails closed. The frozen non-secret identity is the
only additional data propagated into the sanitized daemon environment; the
child repeats live proof when it reaches the boundary.

## Verification

- Safety fixtures: `9/9` passed. They cover the real audited SSD root and reject
  a forged directory, wrong UUID, non-APFS, FileVault disabled, Owners disabled,
  locked volume, non-boundary `0775`/`0777`, and a symbolic-link parent.
- Focused regression: `24/24` passed, including a prior environment-clearing
  hook followed by model/embedding validation.
- Sanitized child regression: `2/2` passed; all four temporary variables were
  explicitly supplied to the child.
- Frozen cohort: `249/249` passed in `147.803s`; the test method count remained
  exactly 249 because the new safety fixtures live outside `tests/`.
- `py_compile`: all top-level modules plus the changed test and safety fixture
  passed.
- Format: `tabnanny` plus tab/trailing-whitespace checks passed for changed code;
  new files contain no line longer than 160 characters.
- Secret scan: no private-key, common provider-token, GitHub-token, Slack-token,
  AWS-key, or JWT signature matched across the nine changed source/config files.
- Dangerous-call scan: no `shell=True`, `os.system`, dynamic `eval`/`exec`,
  unsafe deserialization, or destructive shell call matched. The new subprocess
  call is fixed to absolute `diskutil`, has no shell, closes descriptors, and has
  a five-second timeout. Literal `0o777` occurs only in the rejecting fixture.

Final test roots (all owner `www1adwawd`, mode `0700`, under the approved SSD):

- `/Volumes/Extreme SSD/Orca/tmp/agent-memory-safety3.a0sQCE`
- `/Volumes/Extreme SSD/Orca/tmp/agent-memory-child.fedxAh`
- `/Volumes/Extreme SSD/Orca/tmp/agent-memory-compile3.PvJzY0`
- `/Volumes/Extreme SSD/Orca/tmp/agent-memory-cohort249-pass.SZ3gc9`
- `/Volumes/Extreme SSD/Orca/tmp/agent-memory-format.fOQ7ym`

Each contains only its dedicated `pycache` directory after test cleanup.

## Source evidence and directory manifest

Exact preimage/postimage byte counts and SHA-256 values are in
`source-sha256.txt`.

Evidence directory:

```text
reports/agent-memory-safe-parent-20260811/
  report.md
  source-sha256.txt
```

Implementation files changed:

```text
tools/orca-agent-memory/safe_parent.py
tools/orca-agent-memory/small_model_extraction.py
tools/orca-agent-memory/semantic_retrieval.py
tools/orca-agent-memory/cloud_extraction_provider.py
tools/orca-agent-memory/orca_memory.py
tools/orca-agent-memory/pyproject.toml
tools/orca-agent-memory/setup.cfg
tools/orca-agent-memory/tests/test_memory_daemon.py
tools/orca-agent-memory/safety_fixtures/test_safe_parent_boundary.py
```

The enclosing target module was already untracked in Git before this task, so
the report identifies the exact touched files instead of presenting the entire
untracked module as this task's diff.
