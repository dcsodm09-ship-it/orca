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
  and its `SHA256SUMS` asset (GitHub release assets are immutable once
  published);
- the Node.js `v24.19.0` darwin-arm64 SHA-256 pin is unchanged -- confirmed
  against nodejs.org's own `SHASUMS256.txt` for that exact release (published
  Node binaries are also immutable);
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
