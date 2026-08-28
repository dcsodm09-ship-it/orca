# Startup context P1 closure candidate (2026-08-11)

Status: `GO_OFFLINE_CANDIDATE_ONLY`. No shared loader, Claude/Codex hook,
provider settings, reviewed pack, Skill, or production runtime was installed or
changed.

## Closed candidate defects

- Per-launch ACK now has an exact 300-second lifetime and binds provider,
  session hash, bundle identity, input-scope identity, reviewed-policy identity,
  launch manifest bytes, and exact generator bytes.
- ACK consumption is one-time: the receipt path is launch-specific and created
  with `O_EXCL`, mode `0600`, file `fsync`, and directory `fsync`. Concurrent or
  later replay returns `ack_replayed` without replacing the winner.
- ACK independently recomputes
  `launch_id = SHA256(provider || NUL || session_sha256 || NUL || challenge)[:32]`
  and requires it to equal both the manifest launch id and the launch-directory
  basename. Coordinated provider rebinding and well-formed session-digest
  replacement now fail closed.
- The launch directory and manifest remain open through ACK. Their original
  `st_dev/st_ino`, owner/private modes, exact manifest bytes, and current
  no-follow lexical identities are rechecked before a receipt is issued.
  Replacing either the launch directory or the manifest inode after the
  fd-anchored read returns `launch_path_changed` and creates no receipt. The
  accepted receipt records both held directory and manifest device/inode
  identities for later capability-gate verification.
- Receipts no longer store the raw challenge. They store its SHA-256 and the
  launch/generator/policy identities needed for audit.
- ACK recomputes the content-addressed bundle/input-scope identities, rechecks
  current reviewed-pack/Git/shared-source freshness, rejects symlinked or
  non-private launch manifests, and rejects generator/source drift.
- Reviewed startup manifests are schema v2. Each shared source has an explicit
  required/optional policy; a required source can no longer use a null digest,
  while an optional source needs a bounded reviewed reason.
- The startup installer requires an exact shared-script SHA-256, puts that
  digest into every installed hook, and the hook rehashes its own bytes before
  delivery. All targets are validated before the first write, serialized under
  one installer lock, settings writes are fsynced, attempted writes roll back on
  failure, and changed installed settings are rehashed.
- Installer ownership matching now requires the exact bridge description and
  no longer removes an unrelated hook merely because its command contains the
  text `startup_context.py`.

## Verification

- Focused ACK/admission regression: 5/5 passed in 9.599 seconds (normal receipt
  identity, concurrent single-use ACK, provider/session rebinding,
  directory/manifest identity swaps, and timeout/generator/reviewed-pack
  drift).
- Independent adversarial boundaries: 9/9 passed (provider, session, bundle,
  input-scope, policy and path-identity tampering; installed-byte hash; exact
  rollback after injected second-target failure).
- Full startup-context module: 25/25 passed in 53.850 seconds.
- Python compile: passed for builder, delivery/ACK, installer, and tests.
- Startup and installer CLI entrypoint checks: 2/2 passed.
- Installer digest/dry-run tests: digest mismatch made zero target writes;
  dry-run made zero target and lock-path writes.
- All `TMPDIR`, `TMP`, `TEMP`, and bytecode cache paths were fresh mode-0700
  directories on `/Volumes/Extreme SSD/Orca/tmp`.

## Candidate identities

| File | Bytes | SHA-256 |
|---|---:|---|
| `orca-context-bridge/scripts/startup_context.py` | 31,306 | `22e80c36d57452cbf021ea4dba0190e1829c9940038c3c90f51dbdd4287e579e` |
| `orca-context-bridge/scripts/build_startup_bundle.py` | 37,268 | `491a0d12144c32e11bd4599a629dc7289192ca2a145239600daba39a78dd5208` |
| `orca-context-bridge/scripts/install_startup_injection.py` | 17,666 | `33b973de60dafb993f95c4886ef9875e468fc71a5973e724c1daae124ef07286` |
| `tests/test_startup_context.py` | 58,970 | `c3412ebe8cfa7603617f62be2f306fdfabda89914fdab608f0106ba97efa5310` |
| `orca-context-bridge/SKILL.md` | 19,697 | `c3e8d173a99261283362bbc5619b8a20d52c0d26a1be07c7afe99e4f79371f29` |

## Remaining production gates

The candidate still does not make a text hook an edit-capability gate. A
trusted Orca runtime must require the accepted receipt before enabling writes.
The central schema-v2 reviewed pack must be independently built/attested, and
the staged script/pack/DSSE/installed bytes must share one release identity.
Multi-account installation needs an authorized preimage/rollback rehearsal and
fresh Claude plus every Codex account E2E. Same-user local hashes are integrity
checks, not issuer authentication; production release/retirement requires the
separate threshold-attestation work.
