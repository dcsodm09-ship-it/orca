# Orca-native multi-Codex scheduling contract

Status: the source candidate is build-verified; the installed runtime still
does not advertise the account-binding capability. This document separates the
implemented local boundary from remaining account-pool scheduling work.

## Authority model

Orca is the only execution authority:

1. One bound Run owns the objective and coordinator inbox.
2. Tasks and dependency edges form the durable DAG.
3. Each attempt is one Dispatch; only its worker may settle it with
   `worker_done`.
4. A new Codex is created only by `orca orchestration worker-start`.
5. Each editing worker is placed in an isolated child worktree. Integrators
   consume committed identities and merge/cherry-pick serially.
6. The coordinator starts every independent Task in the selected wave before
   entering rolling `check --wait`; this is Orca-controlled concurrency, not a
   collection of unmanaged shell processes.

The planner may recommend a wave, but it never changes Run, Task, Dispatch,
terminal, worktree, account, settings, or process state itself.

## Implemented source launch capability

The W10 source implementation adds:

```text
orca orchestration worker-start ... --managed-account-ref <opaque-managed-account-id>
```

It is protected by `orchestration.worker-account-binding.v1`. The CLI checks
that capability before invoking `worker-start`; the local runtime resolves the
opaque id only after Run/Task validation, validates a host-owned managed home
and its credential readiness, and passes that home to a fresh Codex PTY without
changing the global account selection. The persisted Dispatch receipt contains
only requested/effective opaque ids, never a home path.

It is local-only. A federated start with this field fails before effects; an
older runtime fails in the CLI before invoking `worker-start`. Neither path may
discard the field or fall back to the default account.

## Remaining account-pool capability

The account catalog exposed to the coordinator contains only:

- opaque account reference;
- provider/agent and supported model families;
- coarse remaining-quota value or bucket;
- cooldown deadline and health state;
- current lease/Dispatch count.

It must not expose tokens, cookies, auth documents, account-home paths,
principal identifiers derived from credentials, or raw provider responses.
Catalog health/quota/cooldown, account leases, and federated host selection are
not yet implemented; they remain release blockers for account-rotation waves.

## Atomic worker-start transaction

The current local implementation performs, in order:

1. authenticate the coordinator and bind Task to the selected Run;
2. validate Task, placement, agent, terminal reuse, and local managed-home
   ownership/credential readiness;
3. create/reuse only the requested isolated worktree;
4. launch fresh Codex with that managed account's isolated runtime home;
5. persist requested/effective opaque account ids with the Dispatch;
6. return a receipt only after the worker is ready and the dispatch preamble is
   delivered.

Failure records residual resources through the existing worker-start ledger.
The source candidate deliberately does not claim account reservations, quota
selection, cooldown, or federated identity routing.

An active Dispatch is immutable with respect to account, model, and effort.
Quota exhaustion or cooldown affects only a future Dispatch. Reuse of a settled
terminal requires a fresh task Dispatch and must retain the terminal's original
account identity unless the terminal is released and a new one is launched.

## Implemented receipt contract

The worker receipt contains redacted fields:

```json
{
  "accountBinding": {
    "requested": "opaque-managed-account-id",
    "effective": "opaque-managed-account-id"
  }
}
```

The opaque account reference is identity metadata, not a credential. A missing
or different effective field is a failed launch, never a warning-only fallback.

## Wave selection

The current policy candidate applies these hard gates before ranking work:

- `red` capacity or `new_workers_max=0`: zero starts;
- `yellow`: at most one start;
- `green`: bounded by fresh `new_workers_max`;
- dependencies must be ready;
- an editing task must name an isolated child worktree;
- P0/security/authority uses Sol `xhigh`, final acceptance may use Sol `max`;
- cross-file implementation uses Terra `high`;
- bounded tests/docs/fixtures/inventory uses Luna `medium`.

Capacity snapshots are inputs to a single wave only. The coordinator refreshes
them before the next wave. The default `runtime_default` policy has no account
records and never selects or rotates accounts. A future managed-account wave
must add account health/quota/lease checks without allowing the account-pool
size to override physical CPU/memory capacity.

## Parallel and serial boundaries

Parallel work is allowed only for Tasks whose source write sets are isolated in
different child worktrees. The following remain serial decision gates:

- cherry-pick/merge into the integration branch;
- live Hook, memory, Skill, settings/provider, or `.orca/context` writes;
- R2 writer/restore identity publication;
- real archive/copy/delete and internal-source retirement;
- release publishing and remote changes.

The final Integrator first verifies each `worker_done`, Dispatch receipt, commit
SHA, changed-file boundary, and tests. A second independent acceptance Task then
checks the combined scheduler/account isolation and W0-W9 integration result.

## Remaining implementation tests

- A fresh installed runtime advertises the capability and accepts a local
  account-bound worker end-to-end.
- Two simultaneous Codex worker-start operations bind distinct managed-account
  homes and never race through shared global authentication state.
- Same-account concurrent reservation respects the configured lease limit.
- Invalid/cooling/exhausted/incompatible accounts fail before worktree/terminal
  creation.
- Crash at every worker-start stage yields one inspectable mutation-ledger
  outcome and no duplicate worker.
- An active Dispatch cannot hot-switch accounts.
- Release/reuse paths release or preserve an account lease exactly as defined.
- No structured or textual receipt leaks credential material or account-home
  paths.

The source candidate tests the local CLI/RPC gate, trusted distinct homes,
no-path persistence, local-only federation rejection, and build inclusion. A
fresh installed runtime E2E and all account-pool tests above remain required
before account-isolated multi-agent release.

## Current source routing

The source candidate extends the existing launcher rather than adding a second
one:

- `src/cli/specs/orchestration-worker-specs.ts` and
  `src/cli/handlers/orchestration.ts`: CLI grammar and runtime call;
- `src/main/runtime/rpc/methods/orchestration-worker-start-schema.ts`: strict
  worker-start input schema;
- `src/main/runtime/orchestration/worker-account-binding.ts`: validates the
  opaque id to an owned host managed home at the local trust boundary;
- `src/main/runtime/rpc/methods/orchestration-worker-launch-preferences.ts`:
  requested/effective model and effort receipt plus capability checks;
- `src/main/runtime/rpc/methods/orchestration-workers.ts` and
  `orchestration-worker-topology.ts`: local/federated start transaction and
  agent-first worktree creation;
- `src/shared/agent-session-host-authority.ts`: host-owned launch preference
  transport (currently model/effort/mode only);
- `src/main/ipc/pty.ts`: selected Codex home resolution and spawn-time pane
  account attestation;
- `src/main/codex/codex-pane-launch-account.ts`: records which account/home a
  pane actually launched under;
- `src/main/codex-accounts/service.ts` and `runtime-home-service.ts`: trusted
  managed-home validation and per-account runtime materialization;
- `src/cli/handlers/account.ts` and runtime `accounts.list`: redacted catalog
  boundary that currently fails from this CLI environment.

The launch field should remain an opaque account id through CLI/RPC and be
resolved to a trusted managed home only inside the runtime. A home path must not
cross the RPC, receipt, Task spec, or coordinator output boundary.
