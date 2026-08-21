# Prime Agent integration for installed Orca

This is a source-level, fail-closed integration candidate. It installs a pinned
Prime Agent runtime without rebuilding, rewriting, repackaging, or replacing
`/Applications/Orca.app`.

The installed Orca source already contains Prime Agent command detection,
launch, process identity, resume, and agent-directory routing -- this
candidate's evidence (including the real, non-mocked `tests/sandbox_e2e.py`
lifecycle replay) was most recently gathered against installed Orca 1.4.184.
That version number is not itself a live gate: `verify_orca_support()` never
compares an Orca version string, it checks the actual support-file markers
and hashes under `/Applications/Orca.app` at install/verify time, so it stays
correct as Orca is updated. The missing local prerequisite is a trustworthy
`prime-agent` command. This directory supplies that prerequisite, but the
real user install has not been run.

## Pinned upstream evidence

Live checks against official GitHub and Node.js endpoints on 2026-08-14 found:

- latest stable Prime Agent release: `v0.7.2`, published 2026-08-11;
- release tag target: `83a0f9f9566219551fcb6ffaf7f519a815749a58`,
  marked verified by GitHub;
- four official release assets pinned by SHA-256;
- source `package-lock.json` pinned by SHA-256;
- upstream MIT `LICENSE` pinned from the exact release commit by SHA-256;
- Node.js `v24.19.0` darwin-arm64 pinned by SHA-256, including npm `11.17.0`.

The upstream main branch had already advanced beyond the release commit at the
time of inspection. This candidate deliberately follows the stable release,
not moving main. A newer stable release, `v0.7.3` (published 2026-08-17), was
available by the time of the 2026-08-19 re-verification below; this candidate
deliberately stays on `v0.7.2` because all security review rounds verified
behavior against `v0.7.2`'s exact bundled JS, and moving versions would
invalidate that review.

Re-verified 2026-08-19 (v0.7.2 remains current for every item below; nothing
upstream-immutable changes on re-check):

- the four release-tarball SHA-256 hashes above are unchanged -- confirmed
  byte-identical via `gh api repos/PrimeIntellect-ai/prime-agent/releases/tags/v0.7.2`
  and its `SHA256SUMS` asset (GitHub release assets are conventionally
  treated as immutable once published, though see the 2026-08-22
  correction below -- byte-level SHA-256 comparison, not that assumption,
  is what this pin actually relies on);
- the Node.js `v24.19.0` darwin-arm64 SHA-256 pin is unchanged -- confirmed
  against nodejs.org's own `SHASUMS256.txt` for that exact release;
- the source `package-lock.json` pin (v0.7.2's own *direct* dependencies) is
  unchanged -- only the generated *transitive* closure below had drifted.

The one stale value found and refreshed: the **generated production
closure**. A real, isolated replay of this installer's own
`npm install --package-lock-only` step (pinned Node `v24.19.0`/npm `11.17.0`
toolchain, private HOME/cache/temp, no project or user npm config, same as a
real install) against the unchanged pinned direct dependencies produced a
different transitive resolution than the one captured on 2026-08-14, because
npm always resolves to the *highest currently-published* version satisfying
each floating range and five days had passed:

- package-row count: **200 rows, unchanged** from the 2026-08-14 capture;
- normalized lock SHA-256: **`d6da1eea7d0f2d0a7c14251dde34e31d799edad6c78bea6c08cf33294727ee32`**
  (was `f537ad6d7061987cd56faf2a268c0322b34c433ef7d257eb8052d54d3d2d224c`).

Diff methodology: the 2026-08-14 closure's raw JSON was never persisted
anywhere (only its SHA-256 was pinned, so npm's registry state as of that
exact date cannot be replayed after the fact). Every one of the 196
non-local-asset rows in the freshly generated 2026-08-19 closure was instead
checked against the npm registry's own per-version publish timestamp for
that exact resolved version: a version published *after* 2026-08-14 cannot
possibly be the one an npm resolution on 2026-08-14 picked, so it is a
provable change; a version published on or before 2026-08-14 was, with high
confidence, already the highest-satisfying version back then too (npm
performs a fresh, no-prior-lockfile resolution here every time, so a floating
range simply keeps whatever was already newest unless something newer
appeared). Result: **6 of 196 registry rows changed** (3.1%), all in the
`@smithy/*` scope (the AWS SDK for JS v3 runtime libraries, an existing,
already-pinned dependency family in this same closure) --
`@smithy/core` 3.33.1->3.33.2, `@smithy/credential-provider-imds` 4.5.1->4.5.2,
`@smithy/fetch-http-handler` 5.7.1->5.7.2, `@smithy/node-http-handler`
4.11.1->4.11.2, `@smithy/signature-v4` 5.7.1->5.7.2, `@smithy/types`
4.17.1->4.17.2. Every change is a strict patch-level bump within an already-
pinned major.minor line (no new package, no major/minor jump); all six were
published within the same ~7-minute window on 2026-08-15 by the identical
maintainer pair (`smithy-team` / `aws-sdk-bot`, both `@amazon.com`) via npm's
GitHub Actions OIDC trusted-publisher mechanism, for packages 2.5-3+ years
old with 70-160+ prior published versions each -- sampled directly against
the npm registry API, not merely npm's local cache. No new package, maintainer
change, unusually-new package, or other supply-chain anomaly was found. The
remaining 190 registry rows, and all four locally patched managed-asset rows,
are unchanged. This closure refresh, and the resulting `GENERATED_LOCK_SHA256`
and `install_prime_agent.py` update, is itself the kind of upstream-trust
judgment call this section exists to leave a paper trail for.

Re-verified 2026-08-21 (v0.7.2 remains current for every item below; nothing
upstream-immutable changed on re-check):

- the four release-tarball SHA-256 hashes above are unchanged -- confirmed
  byte-identical via `gh api repos/PrimeIntellect-ai/prime-agent/releases/tags/v0.7.2`
  and its `SHA256SUMS` asset, and independently confirmed a third way by
  downloading all four real tarballs and hashing the bytes directly with
  `shasum -a 256`;
- the Node.js `v24.19.0` darwin-arm64 SHA-256 pin is unchanged -- confirmed
  against nodejs.org's own `SHASUMS256.txt` for that exact release;
- the source `package-lock.json` and MIT `LICENSE` pins (v0.7.2's own commit-
  pinned files) are unchanged -- re-fetched from the exact pinned commit and
  re-hashed;
- two newer stable releases now exist upstream: `v0.7.3` (2026-08-17,
  already known as of the prior refresh) and a new `v0.7.4` (published
  2026-08-19T23:44:42Z, now GitHub's own "latest"). Informational only --
  this candidate deliberately stays on `v0.7.2` for the same reason as
  before: all security review rounds verified behavior against v0.7.2's
  exact bundled JS, and moving versions would invalidate that review.

The one stale value found and refreshed, again: the **generated production
closure**. As before, npm always resolves to the *highest currently-
published* version satisfying each floating range, and roughly two days had
passed since the 2026-08-19 refresh:

- package-row count: **200 rows, unchanged** from the 2026-08-19 capture;
- normalized lock SHA-256: **`fe4402ae740cc0d2f326baf58f80543ecf8e9668e22f6434f0941bed43c732f5`**
  (was `d6da1eea7d0f2d0a7c14251dde34e31d799edad6c78bea6c08cf33294727ee32`).

Reproduction: two fully independent, live-registry, real isolated replays of
this installer's own `npm install --package-lock-only` step (pinned Node
`v24.19.0`/npm `11.17.0`, private HOME/cache/temp, no project or user npm
config -- one reusing `tests/sandbox_e2e.py`'s fixture, the other a
from-scratch driver with its own private sandbox root, independently
monkey-patched paths, and verified-clean contamination checks both before
and after) both produced the identical `fe4402ae74...` hash, each also
cross-checked internally by a second, independently-invoked hashing path
over the same normalized bytes. The two replays agree with each other
bit-for-bit; no divergence was found.

Diff methodology: identical to the 2026-08-19 refresh. All 196 non-local-
asset rows in the freshly generated closure were checked, exhaustively (not
sampled), against the npm registry's own per-version publish timestamp for
that exact resolved version, using the confirmed-good 2026-08-19 pin as the
cutoff. Result: **3 of 196 registry rows changed** (1.5%), all in the
already-pinned `@smithy/*` scope -- `@smithy/core` 3.33.2->3.33.3,
`@smithy/node-http-handler` 4.11.2->4.11.3, `@smithy/signature-v4`
5.7.2->5.7.3, all three published within a ~2-minute window
(2026-08-20T16:01:44Z-16:03:42Z UTC) by the identical maintainer pair
(`smithy-team` / `aws-sdk-bot`, both `@amazon.com`) via npm's GitHub Actions
OIDC trusted-publisher mechanism -- the same pattern as the 2026-08-15
release accepted in the prior refresh. Each is a strict patch-level bump
within an already-pinned major.minor line (no new package, no major/minor
jump). Provenance was independently queried against the registry API per
row, not assumed: `_npmUser` shows `GitHub Actions <npm-oidc-no-reply@github.com>`
with a `trustedPublisher` OIDC config on all three, `dist.signatures` is
present on all three, and the fetched attestation bundle for
`@smithy/core@3.33.3` contains both a valid npm publish attestation and an
SLSA v1 provenance attestation, both Sigstore-signed -- genuine cryptographic
provenance, not merely a metadata field. Repository remains
`github.com/smithy-lang/smithy-typescript`; maturity is 102-162 prior
published versions per package. The remaining 193 registry rows, and all
four locally patched managed-asset rows, are unchanged, confirmed on or
before 2026-08-19 for every one, 0 lookup errors. No new package, maintainer
change, unusually-new package, or other supply-chain anomaly was found. This
closure refresh, and the resulting `GENERATED_LOCK_SHA256` and
`install_prime_agent.py` update, is itself the kind of upstream-trust
judgment call this section exists to leave a paper trail for.

Independent round-53 QA (2026-08-22) re-verified the 2026-08-21 pin above
from scratch -- live anchors, a third independent isolated replay
(reproduced `fe4402ae74...` again), all 196 registry rows checked
(exhaustive, not sampled), the three `@smithy/*` rows' Sigstore/SLSA
attestation bundles downloaded and their SHA-512 subject digests verified
against independently downloaded tarballs, actual old/new tarball content
diffs reviewed (not just registry metadata), three real non-mocked
`sandbox_e2e.py` lifecycle runs, and the full test suite twice under each
Python interpreter -- and reached an independent **GO**. Its full report is
archived at `../reports/PRIME-AGENT-ROUND53-CODEX-QA-REPORT-2026-08-22.md`
(one directory up, outside this package). It also found four concrete,
non-blocking follow-ups, addressed as part of this same 2026-08-22 change:

1. **`extract-zip<=2.0.1` advisory** (`GHSA-jmr9-qjv8-65gv`, high severity,
   no upstream fix): confirmed unreachable on this installer's supported
   darwin-arm64 path (the `fd`/`rg` assets it downloads are `.tar.gz`,
   extracted with the system `tar`, never extract-zip's ZIP branch) --
   but flagged that disabling `npm audit` during hermetic lock generation
   means a separate, explicit audit gate/cadence is needed rather than
   relying on someone remembering to check by hand. Added: `python3
   install_prime_agent.py audit` (`audit_installed_lock()`, see "Operator
   commands" below), a read-only, explicitly opt-in command that first
   runs the complete `verify` gate (so a tampered lock or release tree is
   caught before anything about it is trusted), then runs a real `npm
   audit` against the installed production lock and fails closed on any
   advisory for a package/GHSA-id pair not on a small, reviewed,
   documented allowlist (`ACCEPTED_ADVISORIES` -- currently just
   `extract-zip`'s exact reviewed GHSA id, with the reasoning above
   recorded inline; a *different* future extract-zip advisory is not
   covered by this entry and fails closed too). Deliberately NOT part of
   plan/install/verify/enable, which stay fully offline and hermetic.
2. **Generated-registry-lock drift versus the pinned upstream *source*
   lock** (not versus the prior generated-lock pin -- a different,
   stronger baseline): roughly 36 version differences across the
   registry-resolved closure (e.g. `zod` 3.25.76->4.4.3,
   `@types/node` 22->26), confirming the four Prime Agent tarballs are
   byte-pinned but the full 196-package runtime closure is not claimed to
   be identical to the exact tree reviewed at the v0.7.2 source commit.
   The report's own "even stronger architectural option" -- seed the
   production lock directly from the pinned upstream source lock instead
   of a fresh floating resolution -- was attempted this same round: seeding
   `npm install --package-lock-only` with upstream-pinned versions for the
   63 overlapping-by-name packages found only 10 of 200 final rows
   actually differed from an unseeded floating resolution, and several of
   those differences were themselves immediately re-resolved forward again
   by npm regardless of the seed. Round-54's own dual review (below)
   correctly pushed back on over-claiming this as proof that lock-seeding
   is *inherently* unreliable: the more likely explanation is that this
   particular seed's root package (a synthetic single `file:` dependency)
   was structurally incompatible with the seed lock's own very different
   shape (the whole upstream monorepo, workspaces and all), not that npm
   treats an existing lock as a mere hint -- and npm's `overrides` field is
   a documented, unused, first-class mechanism for forcing exact
   transitive versions that a future round should actually try before
   concluding seeding is infeasible. What IS implemented now, as the
   report's own concretely-actionable recommendation #1 ("persist
   generated-lock evidence and full tuple diffs"): `compute_lock_drift()`
   runs automatically on every real install, recording every registry
   package resolved to a version different from the pinned upstream source
   lock directly in that install's receipt (`lock_drift_from_upstream_
   source`) -- durable, automatic evidence in place of a hand-reconstructed
   one-off diff every future review round, though still name/version-only
   telemetry, not a full path/resolved/integrity tuple diff and not itself
   a drift-elimination mechanism.
3. **GitHub release API metadata correction**: the release currently
   reports `"immutable": false`, contradicting this document's prior,
   more categorical wording (corrected above). The byte-level SHA-256
   comparisons this pin actually relies on were unaffected and all still
   passed.
4. **Pin-staleness cadence**: added `GENERATED_LOCK_PINNED_AT` (the date
   `GENERATED_LOCK_SHA256` was last re-pinned) and
   `generated_lock_pin_age_days()`, surfaced in `plan()`'s evidence.
   Deliberately informational only, never a hard gate -- a stale pin is a
   prompt to re-verify, not proof anything is wrong today.

Round-54's own dual review (Claude opus/max + Codex sol/max, both
independent, 2026-08-22) then found real defects in this same change before
it was allowed to stand: `compute_lock_drift()`'s registry-row filter
silently ignored 122 of 184 comparable upstream package names (a genuine
npm lockfile-v3 property -- many real registry rows carry no `resolved`
field at all), undercounting drift by 16% (31 reported vs. 36 real);
`GENERATED_LOCK_PINNED_AT` was itself off by one day (`bafce01bf5`, the
commit that actually re-pinned `GENERATED_LOCK_SHA256`, is dated
2026-08-21, not 2026-08-22), which made `plan()` briefly report a negative
pin age; and, most seriously (**P1/blocker, both reviewers independently**),
the new `audit` action verified only the Node/npm binaries before running
`npm audit` -- never the lock file it was actually auditing, nor the rest
of the release tree -- and its advisory allowlist matched by bare package
name only, silently accepting any *future* advisory against `extract-zip`
alongside the one actually reviewed. All four are fixed: `audit` now calls
`verify()`'s complete gate first; the allowlist is keyed by the exact
`(package, GHSA id)` pair; the drift filter now also accepts registry rows
with no `resolved` field (rejecting only `file:`/workspace-link rows, and
requiring a `node_modules/`-prefixed lock path so a workspace member's own
non-`node_modules/` definition row is never admitted either);
`GENERATED_LOCK_PINNED_AT` corrected to `2026-08-21` with a
`max(0, ...)` clamp against ever reporting a negative age again.

A subsequent round-55 re-review (opus/max, mutation-tested: each fix was
independently confirmed by reverting it and re-running the suite) found no
new P0/P1 in the code, but did find the drift-filter widening had zero
regression coverage (now added) and a stale, factually-superseded comment
that had been left standing directly above `ACCEPTED_ADVISORIES` even
though it asserted the opposite of what the fixed code does (removed, not
just supplemented). It also caught this document itself still saying "37"
in two places where the real, hash-verified drift count is 36 (corrected
above) -- worth naming plainly: this section exists specifically to leave
an accurate paper trail, and it twice needed correcting by later rounds
after inheriting a manual arithmetic slip forward from an earlier one.

The parallel Codex sol/max leg of that same round-55 re-review -- run
independently alongside the opus/max leg above, not jointly with it --
rated its own review NO-GO and found one more real defect the opus/max
leg's own P2-only list above did not: the `audit`
action reads `package.json`/`package-lock.json`, but neither file had ever
been individually pinned (an accepted residual dating back to before `audit`
existed, when nothing read either file again after install) -- so a
same-UID racer who swapped either file's content during `npm ci`'s own
real, multi-minute subprocess window could have had the poisoned bytes
adopted as the permanent baseline, with `npm audit` then unknowingly
auditing the attacker's own substituted "clean" manifest/lock. Fixed
(round 56): both files are now pinned in `release_relative_pinned_digests`
the same way every other executed/read-again file already is, and removed
from `allowed_unpinned_release_files()`'s exemption set. Two new real,
non-mocked-`run_npm()` regression tests reproduce the exact attack (content
swapped as a side effect of the real `npm ci` window, verified to make
pre-fix `install()` complete successfully with the attacker's bytes baked
into the trusted baseline, and to fail closed post-fix).

Re-pinned 2026-08-22 (round 56; v0.7.2 remains current for every
upstream-immutable item above; nothing upstream-immutable changed on
re-check). The **generated production closure** changed again, for the
same reason as every prior refresh: npm always resolves to the *highest
currently-published* version satisfying each floating range, and this time
the drift was caught live, mid-review -- both this project's own
`sandbox_e2e.py` runs and an independent Codex sol/max round-55 re-review's
own real replay failed closed within the same ~15-minute window that
`@aws-sdk/core@3.977.9` published upstream, roughly a day after the
round-54/55 pin.

- package-row count: **200 rows, unchanged** from the round-54/55 capture;
- normalized lock SHA-256: **`006d6d1493b35f973316e2bcd8724a26b7bda5a427dbe6ca73447440b929e092`**
  (was `fe4402ae740cc0d2f326baf58f80543ecf8e9668e22f6434f0941bed43c732f5`).

Reproduction: the real `install()` code path (not a hand-rolled npm
replica) was independently re-run twice with a fully fresh npm cache/home
each time, producing the identical normalized hash both times; a third,
fully independent confirmation came from the round-55 Codex reviewer's own
concurrent real run. Both the old and new hash were then independently
recomputed directly with the installer's own `normalized_production_lock()`
-- catching and correcting a real mocking mistake along the way (the
generic path-scrub is a no-op unless `RELEASE_DIR` is mocked to the exact
absolute base path a given capture actually used, since two of the four
locally patched packages' own manifests embed sibling `file:` paths built
from it) -- and, once corrected, both matched exactly.

Diff methodology: every node_modules/-prefixed row matched by exact lock
path across both locks, name+version compared exhaustively (not sampled),
plus a full-row (all-keys) equality check on every unchanged-version row to
catch silent drift behind a stable version string (found none, across all
183 unchanged rows). Result: **17 of 200 registry rows changed (8.5%)**,
every one in the already-pinned `@aws-sdk/*` scope --
`@aws-sdk/core`, `credential-provider-{env,http,ini,login,node,process,sso,
web-identity}`, `eventstream-handler-node`,
`middleware-{eventstream,websocket}`, `nested-clients`,
`signature-v4-multi-region`, `types`, `xml-builder`, all strict
patch-level bumps within an already-pinned major.minor line, plus a nested
copy (under `credential-provider-sso`) of `token-providers`
(3.1111.0->3.1116.0, a minor bump still within the same already-pinned
major line -- the separate, top-level hoisted `token-providers` copy,
pinned at 3.1095.0, is untouched). No new package, no major/minor jump on
any directly AWS-published row, no new dependency or lifecycle script on
any of the 17 rows -- only internal `@aws-sdk`/`@smithy` sibling
version-range floors moved. Provenance was checked per row against the
live registry, not assumed: all 17 show byte-identical publisher identity
(`aws-sdk-bot <aws-sdk-js-automation@amazon.com>`) between old and new
version. None of the 17 carry Sigstore/GitHub-OIDC provenance attestation
on either version (`/-/npm/v1/attestations` returns 404, before and after,
for every row, independently confirmed to be a real absence rather than a
broken check by querying the same live endpoint against several
known-provenance packages) -- a pre-existing, package-wide gap for the
entire `@aws-sdk/*` family, unlike `@smithy/*` (whose 2026-08-19/2026-08-21
refreshes did carry genuine Sigstore/SLSA attestation), not a regression
introduced by this bump, and worth continuing to flag separately to the
security-review track rather than treating as newly acceptable. The
remaining 183 registry rows, and all four locally patched managed-asset
rows, are unchanged -- the latter four confirmed byte-for-byte identical
in their fully normalized form. No new package, maintainer change,
unusually-new package, or other supply-chain anomaly was found. This
closure refresh, and the resulting `GENERATED_LOCK_SHA256` and
`install_prime_agent.py` update, is itself the kind of upstream-trust
judgment call this section exists to leave a paper trail for.

## Installation contract

The installer does not execute the upstream `curl | sh` installer. It:

- verifies the exact coding-agent, AI, core, and TUI release tarballs;
- installs a verified copy of the upstream MIT license, which the four release
  tarballs do not themselves contain;
- safely extracts archives with traversal, link, device, duplicate, member-count,
  and expanded-size checks;
- rewrites the three released workspace packages to local, scoped managed
  packages and exactifies direct dependencies from the pinned source lock;
- uses only the pinned Node/npm toolchain; install and exact-version probes share
  a private SSD-backed cwd, HOME, temp directory, cache, user/global config, and
  disabled update notifier instead of consulting project or user npm config;
- accepts only the exact managed npm configuration, public npm registry URLs,
  SHA-512 integrity values, and the independently reproduced production-lock
  digest;
- runs npm with lifecycle scripts disabled;
- stores runtime, state, probe home, sessions/transcripts, receipts, cache, and
  recovery material on the Extreme SSD;
- verifies private ownership/modes (including the runtime root), lexical and
  resolved SSD residency, SSD UUID, the full runtime tree, exact tool versions,
  exact Prime Agent version output, and the exact installed Orca support-file
  identity recorded during installation;
- revalidates the mutable runtime settings path as private, non-symlinked state
  and requires `telemetry.enabled` to remain `false` and `sessionDir` to remain
  the exact private SSD session directory;
- writes telemetry-disabled, SSD-session settings before the first activation;
- recursively syncs every runtime file and each directory from the leaves upward,
  durably records every newly created managed ancestor in its parent, rechecks
  the full tree identity, and only then publishes the durable pending journal
  with create-only no-clobber semantics; the committed receipt uses the same
  first-publication rule. A pre-journal interruption remains an unactivated
  partial tree for recovery quarantine;
- leaves the PATH command disabled until a separate `enable` action;
- serializes install, recovery, enable, and disable with a private lifecycle
  lock. Every managed wrapper launch holds the matching shared lock for the
  child process lifetime.

An existing `~/.prime`, managed state directory, probe home, session directory,
receipt, command, or ambiguous `prime-agent` in Orca's command-resolution search
paths causes a fail-closed refusal during `plan` and before either a fresh
`install` or a pending-install commit. The search covers the process `PATH` and
Orca's NVM, Volta, asdf, fnm, mise, local-bin, pnpm, Yarn, and Bun fallbacks.
Empty or relative PATH components are rejected because their command target
varies with the launch cwd. No existing state or credentials are merged.

The managed wrapper blocks `prime-agent update`. For session starts it also:

- blocks a project-local `.prime/agent/settings.json` unless
  `ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS=1` is explicitly set;
- blocks session-start `--cwd`/`--cwd=...` arguments under that same default,
  so a different project's settings cannot bypass the current-directory check;
- blocks `--resume`, `--resume=...`, `-r`, and attached `-r...` forms under that
  same default, because a saved session can select another effective project
  after the wrapper's launch-directory check;
- blocks the `agents` and `attach` runtime selectors by default for the same
  effective-project reason; explicit project-settings opt-in still retains the
  executable-resource guards;
- treats `model` as a runtime command, so it checks the launch project and
  receives the executable-resource guards rather than bypassing them as a
  public informational command;
- inserts `--no-extensions --no-skills --no-prompt-templates` in an
  upstream-command-aware position unless
  `ORCA_PRIME_AGENT_ALLOW_PROJECT_RESOURCES=1` is explicitly set, preserving the
  required first token for `agents`, `attach`, and `model`;
- rejects `--session-dir` and `--session-dir=...`, overwrites both supported
  session-directory environment aliases, and pins the settings value so session
  transcripts cannot be redirected away from the managed SSD directory;
- fixes `PRIME_AGENT_CODING_AGENT_DIR` to managed SSD state and disables version
  checks;
- records the exact managed wrapper in `PRIME_AGENT_LAUNCHER_PATH`, so detached
  daemons retain a release-specific runtime identity after replacing argv with
  the upstream `prime-agent` process title;
- exports `NODE_DISABLE_COMPILE_CACHE=1` before the upstream CLI starts, and
  applies the same setting to the direct version probe, preventing the upstream
  `enableCompileCache()` call from writing to the default system temp directory;
- exports `DO_NOT_TRACK=1` and `PRIME_AGENT_TELEMETRY=0` in addition to the
  telemetry-disabled managed settings file.

These defaults do not sandbox Prime Agent. Once explicitly enabled and used, it
can execute model-created Python and project commands with the user's authority.
They also do not suppress ordinary project instructions such as `AGENTS.md`.

## Operator commands

Run from this directory:

```sh
/usr/bin/python3 install_prime_agent.py plan
/usr/bin/python3 install_prime_agent.py install
/usr/bin/python3 install_prime_agent.py verify
/usr/bin/python3 install_prime_agent.py enable
```

`plan` is read-only and reports unresolved recovery manifests as explicit
conflicts. `install` downloads and stages the verified runtime, creates the
SSD-backed state link, and commits a receipt, but does not expose the
`prime-agent` command. `enable` first runs the complete verification gate and
then creates `~/.local/bin/prime-agent`.

A separate, explicitly opt-in advisory audit exists too:

```sh
/usr/bin/python3 install_prime_agent.py audit
```

`audit` requires an existing install and live network access. It first runs
the same complete verification gate `verify` does (so a tampered lock or
release tree is caught before anything about it is trusted), then runs a real
`npm audit` against the installed production lock and fails closed on any
advisory for a package/GHSA-id pair not on the small, reviewed allowlist in
`ACCEPTED_ADVISORIES`. It is deliberately never run as part of
`plan`/`install`/`verify`/`enable`, which stay fully offline and hermetic --
run it whenever re-verifying trust (e.g. before a re-pin), on whatever
cadence a human decides.

Durable disable and recovery are separate:

```sh
/usr/bin/python3 install_prime_agent.py uninstall
/usr/bin/python3 install_prime_agent.py recover
```

`uninstall` is intentionally a non-destructive disable: it refuses while a
managed launch/process is active, scans even when the command is already absent,
removes only the exact verified command link, rescans after removal, and restores
the link if the post-removal gate fails. Process discovery covers complete
managed path arguments and the exact upstream `prime-agent` process title used
by detached daemons, without treating path prefixes such as backup names as
active identities. Link verification and
conditional removal require the same exact raw target rather than accepting a
resolved multi-hop alias. Link creation is atomic and no-clobber;
conditional removal first moves the observed inode to a unique name without
overwriting anything, then validates it before deletion. A late unrelated
occupant is restored or retained as recovery evidence. The immutable release,
state, sessions, and authentication material remain retained. `recover` rolls a
fully verified pending transaction forward only when both managed homes and the
session directory are pristine, verifies an already committed install, or moves
an unactivated partial runtime, state, probe-home, and session bundle into a
private SSD recovery directory.
Partial recovery rejects symlinked targets, records a recovery manifest, and
attempts a durable rollback if any bundle move or directory sync fails,
including the final recovery-root commit sync. A post-create link sync failure
also conditionally removes only the exact managed link before returning failure.
Both forward recovery moves and rollback moves are atomic no-clobber operations;
if a late path occupant appears, both paths are preserved and the manifest marks
an incomplete rollback for inspection. A later `recover` or `install` can replay
one fully validated interrupted `moving` manifest and durably finish its
remaining no-clobber moves. An already-visible recovery target re-syncs both the
original source parent and the recovery destination before completion, closing
the rename-before-fsync crash window. The recovery-root entry is committed before
`quarantined` is published, and rollback publishes a durable `rolling_back`
marker before any reverse move. `rolling_back`, `recovery_conflict`,
`rollback_failed`, unknown, invalid, or multiple unresolved manifests block
every later recovery and install attempt for explicit review instead of being
skipped.

No install action logs in, imports credentials, starts a daemon, launches a
Prime Agent session, invokes a model, or changes Orca itself. Both pinned
Node/npm version checks and `verify`'s telemetry-disabled/offline Prime Agent
`--version` check use managed private probe environments.

## Validation

Fast local checks:

```sh
/usr/bin/python3 -m py_compile install_prime_agent.py tests/test_install_prime_agent.py tests/sandbox_e2e.py
/usr/bin/python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The bounded sandbox replay performs real official downloads and the complete
install/verify/enable/verify/disable/verify/recover lifecycle under an owned
temporary Extreme SSD directory:

```sh
/usr/bin/python3 tests/sandbox_e2e.py
```

The current unit suite has 99 tests, including deterministic runtime-command and
resume/session guards, launch-lock, PATH, private npm probe, ancestor and
durable-tree ordering, pending-journal and receipt publication races,
leading-daemon-socket grammar, late-occupant, process-scan, unresolved-manifest,
interrupted-forward/rollback recovery, symlinked-patched-asset-output,
verified-link-ancestor-swap (both creation and dir_fd-bound removal),
removed-path-reoccupation, generated-private-file ancestor-directory-swap
(RELEASE_DIR itself swapped for a symlink to a sibling directory), and
optimized-mode-assert-survival fault
injections. The sandbox patches
every managed target path and asserts in a `finally` gate that the real
`~/.prime`, command, shared-tool root, release, state, probe-home, receipt,
pending-journal, and lifecycle-lock paths are absent both before and after the
replay, including on failure. It also executes the generated wrapper before the
full lifecycle. The
sandbox refuses to run from an ambiguous real baseline. A later real
installation remains a separate operator-approved action.
