# Startup context central reviewed pack candidate

Status: local SSD-workspace candidate only. No shared skill, SessionStart hook,
Claude/Codex setting, capability, memory store, or production runtime was
installed or changed.

## Five-gate review

1. Launch binding: the current dispatched launch bundle/challenge matched its
   manifest and the ACK receipt was accepted before edits. Synthetic hook/ACK
   tests cover wrong scope and provider-bound per-launch receipts.
2. Cross-provider shared identity: the exact `bundle_identity` is now computed
   only from the central reviewed authority, Git/storage identity, and shared
   Graphify/capability/Wiki summaries. Private Claude/Codex registry metadata is
   appended afterward. Two distinct Codex account roots and concurrent
   Claude/Codex launches receive the same bundle id with distinct challenges.
3. Privacy: private roots and account ids are absent from the bundle manifest
   and model-visible Markdown. Only availability, byte length, and SHA-256 for
   each private registry/summary remain as isolation metadata; synthetic
   private bodies and paths are asserted absent.
4. Authority/freshness: the fixed SSD authority path is
   `knowledge_root/.orca/context/reviewed-startup-pack-manifest.json`. The
   manifest pins the same-directory pack byte length/SHA-256, Git state, Orca
   capability catalog, Wiki catalog, and Graphify catalog. The 30-second reuse
   path rechecks Git, manifest/pack, Wiki/capability/catalog hashes, and
   Graphify verification state.
5. Storage boundary: expected SSD root, Git common-dir, volume UUID, context
   symlink, reviewed manifest/pack path, and account-home symlink checks remain
   fail-closed. Account hooks use `--require-codex-home-memory`; missing,
   explicit, cross-account, or symlinked roots NACK.

## Candidate file identities

| File | Bytes | SHA-256 |
|---|---:|---|
| `orca-context-bridge/scripts/build_startup_bundle.py` | 36,062 | `64c9ded86837159cd0a5bd2c9ca5e3d84a588507a0e1f3777c5e300f01902218` |
| `orca-context-bridge/scripts/startup_context.py` | 17,637 | `29ff9151eb6e617ddf3f61cee26a2149f1d729b5535de28974d6e544278e8882` |
| `orca-context-bridge/scripts/install_startup_injection.py` | 11,987 | `3c33c5ad22de03986646dd935dacb4d3e35ea2849b86284e9d52ec7caeaf2c91` |
| `tests/test_startup_context.py` | 45,832 | `636e5e25beed8d5a33737f0c418fc2e64a3d9664a82932111b517742d0b6679f` |
| `orca-context-bridge/SKILL.md` | 18,866 | `ab036aa7151d7661f19a291a0ac83eed631509aea406294c9c5b56ef8d3f8f6f` |
| `wiki/Orca启动编排与方法学习四维链设计.md` | 17,977 | `f36739f3f22974a356770e659f5bf6ff4b3564436fad514fe7518a3feabe7348` |

Pre-edit SHA-256 values recorded for the four implementation/test files were:

- `build_startup_bundle.py`: `504943750154bcbd855b04d43f727e49521562e7e1d5ef809f770162031e3741`
- `startup_context.py`: `46ed423d282bcf734c3a0e51a5ce2eea11ef5c05d77dc3e88182371292103166`
- `install_startup_injection.py`: `de9aa4915eb12e0b156e636de3a59932d2e835cc64890a9be18c382d50f1ae82`
- `test_startup_context.py`: `bf8e7559d215bdcb5e116e7de610915e41bc56a2a2bc5830c042d035d7eead57`

## Verification

- Baseline before edits: `15` startup-context tests passed.
- Final focused coverage is part of the full suite and includes `19`
  startup-context tests.
- Final command:
  `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -p 'test_*.py'`
- Result: `148` tests passed in `29.808s`; zero failures or errors.
- No real installer invocation was run. Installer coverage used only temporary
  synthetic settings/hooks and did not read or write real provider settings.

## Production migration blockers

1. The real SSD knowledge root does not yet contain the required central
   reviewed manifest/pack. Its L1-L3 payload, source hashes, reviewer approval,
   and rollback/preimage must be prepared independently; this candidate does
   not mint or self-approve that authority.
2. The candidate scripts have not been promoted to the shared trusted loader,
   and no Claude/Codex SessionStart hook has been installed or updated.
3. Every real Orca Codex account must prove that its launch environment sets
   `CODEX_HOME` to its own exact `.../codex-accounts/<account>/home` root. A
   missing or non-account value intentionally produces a NACK.
4. Promotion needs exact-file SHA review, private same-directory backups,
   atomic serial installation, Codex `/hooks` trust review, and an explicit
   rollback. Existing production hook definitions/settings were not inspected
   or changed in this task.
5. Production acceptance still requires fresh Claude plus every Codex account
   to receive one identical bundle id with independent challenges/capabilities,
   produce valid ACK receipts, and repeat after the 30-second window. SSD
   detach/lock/UUID/symlink and reviewed pack/Wiki/Graphify drift must be
   exercised against the installed runtime before any release claim.

