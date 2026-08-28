# Independent QA report: Prime Agent generated-lock pin update

Date: 2026-08-22 (Asia/Taipei)  
Reviewed commit: `bafce01bf5ab73e27e8b65d881025bb070c072e7`  
Current workspace HEAD during final checks: `7fd3df39a57bd36c411a2207bca5bff3d9403c82`  
Scope: read-only review of `prime-agent-integration`; no repository files or real user state were modified.

## Executive verdict

**GO for this specific `GENERATED_LOCK_SHA256` update.** I independently reproduced the normalized lock hash `fe4402ae740cc0d2f326baf58f80543ecf8e9668e22f6434f0941bed43c732f5`, verified every immutable anchor against its live first-party endpoint, exhaustively checked all 196 registry-backed generated-lock rows against live npm metadata/integrity, cryptographically verified the three newly published Smithy packages and reviewed their actual old/new tarball contents, obtained three clean real sandbox lifecycle runs, and passed the 206-test suite twice under each Python interpreter plus both py-compile checks.

This is not a blanket endorsement of the broader trust methodology. Publish-time comparison alone cannot prove that nothing else changed, the freshly resolved runtime closure has drifted materially from the release's source lock, npm audit currently reports a high-severity `extract-zip` advisory (apparently unreachable on the supported Darwin path), and the README's statement that GitHub release assets are immutable is stronger than GitHub's own metadata (`immutable: false`). Those are substantive follow-ups, but none falsifies or should block this narrowly scoped corrective pin update.

## 1. Immutable-anchor re-verification

I fetched the artifacts directly on 2026-08-22 rather than reusing prior review output. The GitHub release API for tag `v0.7.2` resolves to the pinned commit `83a0f9f9566219551fcb6ffaf7f519a815749a58`, and that exact commit has a valid GitHub signature. I fetched the release's own `SHA256SUMS` asset and all four release tarballs independently, then compared the sums asset, the installer constants, and hashes of the downloaded bytes:

| Artifact | Installer pin | GitHub `SHA256SUMS` | Downloaded bytes | Result |
|---|---|---|---|---|
| `prime-agent-0.7.2.tgz` | `bc5471f2a626d727b88a45eb745fff93b10c554a3c4fc5912f25d8c64b987f5e` | same | same | PASS |
| `prime-agent-ai-0.7.2.tgz` | `0777108abbe12ffcd3efdbf063e1f321ff2a1b16c08a81867d9a6c0addcd1f8d` | same | same | PASS |
| `prime-agent-core-0.7.2.tgz` | `3d576b5edb4634be821c865c68af2afecaf06f5b6148ee9115f99e685704f4bf` | same | same | PASS |
| `prime-agent-tui-0.7.2.tgz` | `642285cd8f1bd06531cfa2f07c6a171a6efd4f54e8bc63fe9cede3838a0453e5` | same | same | PASS |

For Node.js, I fetched `https://nodejs.org/dist/v24.19.0/SHASUMS256.txt` and `node-v24.19.0-darwin-arm64.tar.gz`. The installer pin, official sums line, and independently computed tarball hash all equal:

```text
8294b7aa9b03997481c06babf1e8b270c859358f27da57a11509afe537ac381d
```

I fetched source files directly from the exact pinned Prime Agent commit and rehashed them:

| Source file | Computed SHA-256 | Installer pin | Result |
|---|---|---|---|
| `package-lock.json` | `7e708d473e01fe3f992e8e12e25c4f815282a1763038f1d0f65a8a0f6d7b37e7` | same | PASS |
| `LICENSE` | `b288615fb31dc504623582fb790a28e6d86bc2f5c1396845af555e43386da5a0` | same | PASS |

Nuance: the release API currently reports `"immutable": false`. The byte-level pins still fail closed and every live byte matched, but README lines 43-44 should not categorically say GitHub release assets are immutable once published. Treat that wording as a documentation/trust-model correction, not evidence that this pin is wrong.

## 2. Independent real isolated npm-lock replay

Before replaying, I read the actual code paths rather than approximating them:

- `managed_npm_environment()` builds a private HOME, TMPDIR, npm cache, user/global configs and state directories; restricts PATH to the pinned Node bin plus `/usr/bin:/bin`; fixes npm's registry to `https://registry.npmjs.org/`; disables audit, funding, update notification, lifecycle scripts, provenance, and the Node compile cache; and fixes the locale.
- The call site holds a release-directory file descriptor and calls the pinned npm with `install --package-lock-only --omit=dev --ignore-scripts --install-links`, cwd set to the private release directory and child umask `077`.
- `normalized_production_lock()` recursively replaces the private absolute release path and its URL-encoded representation, rewrites the four local managed assets to `file:$RELEASE_DIR/assets/...` with `managed-local:<name>@0.7.2` integrity placeholders, and emits sorted, two-space-indented canonical JSON with one final newline.

I then constructed a fresh private replay root under `/tmp`, downloaded and hashed the official inputs, extracted the pinned Node distribution, used the installer's own local-asset patching code, invoked the actual `managed_npm_environment()` and `run_npm()` paths with the exact arguments/umask, and independently reimplemented the normalization algorithm as a cross-check.

```text
node --version: v24.19.0 (exit 0)
npm --version:  11.17.0 (exit 0)
npm lock generation: exit 0
generated package-map entries: 201 total, 200 non-root
composition: 4 managed-local rows + 196 registry rows
raw package-lock SHA-256: b806e9873473f0aa81208f58fe97ab77ebbee22b30109ad982c728fb05502e36
normalized byte length: 92350
installer normalization SHA-256: fe4402ae740cc0d2f326baf58f80543ecf8e9668e22f6434f0941bed43c732f5
independent normalization SHA-256: fe4402ae740cc0d2f326baf58f80543ecf8e9668e22f6434f0941bed43c732f5
pinned SHA-256: fe4402ae740cc0d2f326baf58f80543ecf8e9668e22f6434f0941bed43c732f5
```

Result: **PASS**. My independent replay is a third reproducing execution and matches the newly pinned value exactly.

## 3. Registry and provenance verification

### Exhaustive registry scan

I parsed the independently generated lock and fetched live npm packuments for all 196 non-local rows (184 distinct package names). This was not a sample. There were zero fetch errors and zero discrepancies between the generated lock and npm's live per-version `dist.integrity` or tarball URL.

Using the stated prior-pin cutoff, exactly three rows were newly published:

| Package | Old version / time | New version / time |
|---|---|---|
| `@smithy/node-http-handler` | `4.11.2`, `2026-08-15T17:25:07.387Z` | `4.11.3`, `2026-08-20T16:01:44.533Z` |
| `@smithy/core` | `3.33.2`, `2026-08-15T17:24:51.591Z` | `3.33.3`, `2026-08-20T16:03:38.635Z` |
| `@smithy/signature-v4` | `5.7.2`, `2026-08-15T17:25:11.137Z` | `5.7.3`, `2026-08-20T16:03:42.510Z` |

The other 193 rows all predate that cutoff; their latest group was other Smithy packages published on August 15, followed by AWS packages on August 14. This confirms the claimed `3 of 196` result more strongly than the requested meaningful sample.

### Publisher identity and cryptographic proof

All three registry records list the same known maintainers:

```text
smithy-team <aws-javascript-sdk-team+npm@amazon.com>
aws-sdk-bot <aws-sdk-js-automation@amazon.com>
```

For each new version, `_npmUser` is GitHub Actions `<npm-oidc-no-reply@github.com>`, `trustedPublisher.id` is `github`, and the per-package OIDC configuration identity is stable across the old/new version. All three carry repository `smithy-lang/smithy-typescript` and the same release `gitHead` `e9151f3e043a19032caad09b05937a4962c5bd13`.

I downloaded the actual npm attestation bundles, not just the metadata marker. Each package has two Sigstore bundles (npm publish v0.1 and SLSA provenance v1), each with a transparency-log entry. Decoded DSSE statements have the exact package-version purl as subject; their SHA-512 subject digests match independently downloaded tarballs. The SLSA statements identify:

```text
repository: https://github.com/smithy-lang/smithy-typescript
workflow:   .github/workflows/release-npm-packages.yml
ref:        refs/heads/main
source:     e9151f3e043a19032caad09b05937a4962c5bd13
invocation: actions/runs/32389307592/attempts/1
```

The signing certificates chain to Sigstore, carry the exact workflow URI as SAN, and have validity windows covering the August 20 publication. As an independent verifier, a fresh project using the pinned npm returned exit 0 from `npm audit signatures`:

```text
audited 5 packages in 1s

5 packages have verified registry signatures

4 packages have verified attestations
(use --json --include-attestations to view attestation details)
```

The JSON verification result was `{"invalid":[],"missing":[]}` with exit 0. Therefore genuine cryptographic attestation material is retrievable and validates; this is not merely a registry boolean.

### Actual old/new package-content review

To go beyond metadata, I downloaded both old and new tarballs for all three packages, verified each against its registry SRI, extracted them, and reviewed complete diffs:

- `@smithy/core` 3.33.2 -> 3.33.3: 52 files, 432 insertions, 96 deletions. The substantive changes add numeric-exponent handling in CBOR code and ownership guards to `for...in` loops.
- `@smithy/node-http-handler` 4.11.2 -> 4.11.3: 5 files, 24 insertions, 4 deletions. It adds ownership guards and bumps its Smithy core dependency.
- `@smithy/signature-v4` 5.7.2 -> 5.7.3: 9 files, 60 insertions, 17 deletions. It adds ownership guards and bumps its Smithy core dependency.

No new lifecycle scripts appeared; none of the three has preinstall/install/postinstall/prepare hooks. I found no suspicious additions involving child processes, dynamic evaluation, process-environment harvesting, spawning/execution, or network-fetch code. The corresponding source commits (`a825452...`, `e82a22b...`, and release commit `e9151f3...`) are valid GitHub-verified commits in the expected repository. The content changes are nontrivial enough that “patch-level only” would not have been sufficient evidence by itself, but the source/tarball review supports a benign security-hardening/fix release.

## 4. Skeptical assessment of the trust methodology

The publish-timestamp method is useful triage, but **it does not prove that nothing else could have changed**. It assumes that version documents and timestamps are immutable and that the prior resolution is known. It is blind to post-publication registry metadata mutation and cannot tell whether `resolved`, `integrity`, dependencies, or package contents differ unless a durable prior lock is available.

A defensible re-pin procedure should persist the previous normalized lock (or at least every path/name/version/resolved/integrity tuple), byte-diff it against the candidate, compare every candidate integrity/URL to the live registry, and for each changed tuple download old/new tarballs, verify SRI and attestation subjects, then review the actual content/source diff. I performed those stronger current-state and changed-package checks in this review, but the project should make them durable and automated rather than reconstructing history from timestamps.

There is an even stronger architectural option: seed the production lock from the already downloaded and pinned upstream source lock instead of doing a fresh floating resolution. That would reduce registry drift and the recurring need to refresh this trust-anchor merely because semver ranges gained new releases.

The broader drift is material. Comparing the current generated registry lock with same-named paths in the pinned upstream source lock revealed roughly 36 version changes (excluding local workspace rows), including `zod` 3.25.76 -> 4.4.3, `@types/node` 22 -> 26, `node-addon-api` 7 -> 8, and `undici-types` 6 -> 8, plus a new nested path. Thus the four Prime Agent tarballs are byte-pinned, but the installer should not claim that the entire 196-package runtime closure is the exact dependency tree reviewed at the v0.7.2 source commit.

Finally, a separate explicit audit of this generated lock returned exit 1 for high-severity advisory `GHSA-jmr9-qjv8-65gv` in `extract-zip <=2.0.1`, with no available fix. Prime Agent uses `extract-zip` in the tools-manager ZIP branch; on this installer's supported macOS arm64 path, the downloaded `fd`/`rg` assets are `.tar.gz` and use system `tar`, so the vulnerable ZIP branch appears unreachable. It is not introduced by the new Smithy pin and is not a blocker to this narrow update, but disabling npm audit during lock generation means a separate audited-advisory gate/cadence is needed.

## 5. Code-diff review

`git diff --check bafce01bf5^ bafce01bf5` is clean. An AST comparison of the top-level uppercase constants before and after the commit found exactly one value change among 41 constants:

```text
GENERATED_LOCK_SHA256:
  old d6da... -> new fe4402ae740cc0d2f326baf58f80543ecf8e9668e22f6434f0941bed43c732f5
```

The new value exactly matches my replay. `GENERATED_LOCK_PACKAGE_COUNT = 200` is unchanged and equals the 200 observed non-root packages (4 local + 196 registry). No release-tarball, Node, source-lock, license, version, commit, npm-version, or other pin constant changed. The explanatory code comment's numerical and publisher/provenance claims are accurate, subject to the important methodological qualification above that `3 of 196` is a timestamp-based delta against the previous generated-lock event, not proof that the full closure equals the upstream source lock.

The reviewed commit changes only `README.md` and `install_prime_agent.py`; current later HEAD contains no subsequent code change in this target. Final `git status --short -- prime-agent-integration` and `git diff --name-only -- prime-agent-integration` were empty.

## 6. Three real non-mocked sandbox lifecycle runs

All commands were run from `prime-agent-integration/` as `python3 tests/sandbox_e2e.py`, without a lock override. Each exercised install, enable, verify, disable, recover, and uninstall inside the script's real isolated sandbox. All real-user target paths were absent before and after; the script independently reported `real_user_state_changed: false` each time.

### Run 1

Exit code: `0`

```json
{"command_default_enabled": false, "enable_verify_disable_recover": "passed", "license_sha256": "b288615fb31dc504623582fb790a28e6d86bc2f5c1396845af555e43386da5a0", "node_compile_cache_disabled": true, "ok": true, "production_lock_sha256": "fe4402ae740cc0d2f326baf58f80543ecf8e9668e22f6434f0941bed43c732f5", "real_user_state_changed": false, "release_tree_entries": 26914, "release_tree_sha256": "dcf99c1f125d3c0f6a8d2745f276ff6cc1d6f1d103896a135a09e459c30b5817", "sessions_on_ssd": true, "version": "0.7.2"}
```

### Run 2

Exit code: `0`

```json
{"command_default_enabled": false, "enable_verify_disable_recover": "passed", "license_sha256": "b288615fb31dc504623582fb790a28e6d86bc2f5c1396845af555e43386da5a0", "node_compile_cache_disabled": true, "ok": true, "production_lock_sha256": "fe4402ae740cc0d2f326baf58f80543ecf8e9668e22f6434f0941bed43c732f5", "real_user_state_changed": false, "release_tree_entries": 26914, "release_tree_sha256": "28756d1a1d9cada95824a197f33416c5d6cc2a62017095f9c00dfe1e495d69e8", "sessions_on_ssd": true, "version": "0.7.2"}
```

### Run 3

Exit code: `0`

```json
{"command_default_enabled": false, "enable_verify_disable_recover": "passed", "license_sha256": "b288615fb31dc504623582fb790a28e6d86bc2f5c1396845af555e43386da5a0", "node_compile_cache_disabled": true, "ok": true, "production_lock_sha256": "fe4402ae740cc0d2f326baf58f80543ecf8e9668e22f6434f0941bed43c732f5", "real_user_state_changed": false, "release_tree_entries": 26914, "release_tree_sha256": "1b8e3a2927c73793160b3b80e7a437590c0335b2731955e63c237688a2ef7091", "sessions_on_ssd": true, "version": "0.7.2"}
```

The release-tree hashes differ because sandbox-specific absolute paths are embedded in generated wrapper/state files; each run's own receipt/tree verification succeeded. The production normalized-lock hash is identical in all three runs and equals the new pin.

## 7. Full automated suite

`which -a python3` returned:

```text
/opt/homebrew/bin/python3
/usr/bin/python3
/opt/homebrew/bin/python3
```

Interpreter versions were `/usr/bin/python3` 3.9.6 and `/opt/homebrew/bin/python3` 3.14.6. I ran the requested full discovery command twice with each explicit interpreter.

| Interpreter / run | Exit | Exact summary |
|---|---:|---|
| `/usr/bin/python3`, run 1 | 0 | `Ran 206 tests in 43.400s` / `OK` |
| `/usr/bin/python3`, run 2 | 0 | `Ran 206 tests in 38.107s` / `OK` |
| `/opt/homebrew/bin/python3`, run 1 | 0 | `Ran 206 tests in 40.831s` / `OK` |
| `/opt/homebrew/bin/python3`, run 2 | 0 | `Ran 206 tests in 38.662s` / `OK` |

Result: **PASS, 824 test executions total, no failures or skips reported in the final summaries.**

## 8. py_compile

The pin commit's other changed file is Markdown, so I interpreted “both changed files” as the two Python artifacts under test: `install_prime_agent.py` and `tests/sandbox_e2e.py`. I set `PYTHONPYCACHEPREFIX` to a temporary directory to avoid modifying the repository.

```text
/usr/bin/python3 -m py_compile install_prime_agent.py tests/sandbox_e2e.py
exit 0

/opt/homebrew/bin/python3 -m py_compile install_prime_agent.py tests/sandbox_e2e.py
exit 0
```

## 9. Other upstream trust assumptions and recommended cadence

The following deserve explicit re-verification cadence or structural hardening:

1. Persist generated-lock evidence and full tuple diffs, and re-run signature/attestation/content checks whenever the normalized pin changes.
2. Prefer deriving from the pinned upstream lock to resolving floating ranges, or document that the runtime closure is deliberately current-registry rather than release-commit exact.
3. Run a separate production-lock advisory audit even though lifecycle scripts and npm's install-time audit are correctly disabled in the hermetic generation step.
4. Verify Node's signed checksum material when selecting a new Node pin, not only HTTPS plus an embedded SHA-256 constant.
5. Revalidate the release tag/commit signature and GitHub release metadata each time any release pin changes; correct the overbroad release-asset immutability wording.
6. `verify_orca_support()` relies on compatibility markers and records observed Orca file hashes but does not compare them with an independently reviewed hash allowlist. Re-run compatibility/trust review when the Orca application updates.
7. Keep the four local Prime Agent tarball pins, source-lock pin, license pin, Node pin, npm pin, and normalized production lock as separate trust anchors; do not infer that one anchor validates another layer.

## Final decision

**Ready to merge this specific pin update.** The new constant is independently reproducible and well-founded; all requested live evidence, cryptographic provenance, actual package-content review, real lifecycle executions, unit tests, and syntax checks passed. Track the methodology/documentation/advisory items above as follow-up work and do not describe this result as proof that the complete floating dependency closure is permanently immutable or identical to the upstream v0.7.2 source lock.
