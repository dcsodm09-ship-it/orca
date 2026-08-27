# orca-terminal-dispatch

A registry + idle-reaper backstop for one-shot `orca terminal create` dispatches.

CLAUDE.md rule 1 requires every non-Claude AI dispatch (Grok, Gemini, Codex, ...)
to go through `orca terminal create`, and to be torn down afterwards with
`orca terminal close --terminal <handle> --tab --json`. Until now that teardown
existed **only as prose**: every dispatching agent hand-built its own two-step
invocation, so a forgotten, crashed, or timed-out second step leaked a terminal
forever. There was no shared code path to attach a safety net to.

This is that shared code path.

```
orca-terminal-dispatch create --worktree active --agent-kind grok \
    --purpose "design panel leg 2" \
    --command 'opencode run "..." --model opencode-go/grok-4.6 --format json --auto'
# -> {"action":"created","handle":"term_...","marker":"[[orca-dispatch:grok:63d745fa]]"}

orca-terminal-dispatch close --terminal term_...      # still the normal, expected path
```

The once-a-minute launchd job is a **backstop, not a replacement**. Always try to
close cleanly; the reaper only exists for when that fails.

## Commands

| command | purpose |
|---|---|
| `create` | `orca terminal create` + a private registry entry. Requires `--purpose`. |
| `close` | Identity-verified close + entry removal. The normal teardown path. |
| `touch` | Mark a still-running long dispatch as alive, resetting its idle clock. |
| `status` | List registered dispatches and their idle age. Omits command text and owner metadata. |
| `reap` | The backstop. `--dry-run` to see decisions without acting. |
| `doctor` | Health check aimed at how the Ego reaper died unnoticed. |

Long dispatches that produce no output for a while should register their own
threshold: `create --idle-seconds 3600`, or call `touch` periodically.

## Safety model

The reaper mirrors `ego_profile_router.py`'s `due_tasks` / `reap_body` logic.
It closes a terminal only when **every** condition holds:

1. **Positive registration.** It iterates *its own registry*, never
   `orca terminal list`. A terminal this wrapper did not create is invisible to
   it, no matter how idle. Orca exposes no `createdAt` / `createdBy` /
   `ownership` field on terminals, so registry membership plus a per-dispatch
   title marker is the only ownership evidence that exists — this is the
   analogue of Ego's `createdBy:'agent'` / `ownership:'agent'` pair.
2. **`created_by_this_wrapper: true`** on the entry.
3. **Identity re-verification.** Handles are runtime-issued and reusable across
   Orca restarts, so `orca terminal show` must still agree on `handle` +
   `ptyId` + `incarnationId` + `tabId` + `leafId`. Any drift **drops the entry
   without closing anything**.
4. **Marker still present** in the live title (`[[orca-dispatch:<kind>:<token>]]`,
   matched as a delimited substring since Orca prefixes status glyphs). A renamed
   terminal is deferred forever rather than closed.
5. **Genuine idleness**, computed from the newest of registry `last_activity`,
   live `lastOutputAt`, and the agent pane's `updatedAt`. A backdated registry
   entry is *not* sufficient on its own.
6. **`lastOutputAt: null` counts as NOT idle.** A live Claude pane in state
   `done` was observed with `lastOutputAt: null`, `title: null` and an empty
   preview; treating null as "infinitely idle" would have killed it.
7. **Agent-state corroboration** from `orca worktree ps`: refuses while the
   joined `tabId:leafId` pane is `working` or `waiting`, and refuses if the pane
   now runs a different known agent than the one dispatched. If `worktree ps` is
   unavailable the pass **defers** — an outage never becomes permission.
8. **Lock interlock.** A tick that collides with an in-flight `create`/`close`
   steps aside (non-blocking `flock`), mirroring Ego's "router lock is busy".

Ambiguity always resolves toward *defer* or *drop*, never toward *close*.
`orca terminal stop --worktree` is never used — it would kill every terminal in
the worktree.

### The stale-handle false negative

`orca terminal show` is known to answer `terminal_handle_stale` for a terminal
that is in fact alive and connected. A stale answer is therefore cross-checked
against `orca terminal list` before it is believed, and a contradiction resolves
toward "still alive".

## Install

```
./install.sh                        # CLI only, to ~/.local/bin
./install.sh --load                 # + load the once-a-minute launchd reaper
./install.sh --load --dry-run-mode  # + reaper only logs what it would close
./install.sh --uninstall            # unload and remove the launchd job
```

`install.sh` **refuses** to install if the destination or state dir resolves
onto `/Volumes/*`, and proves the chosen interpreter can actually execute the
installed copy before writing the plist.

### Why that check exists

`com.local.ego-taskspace-reaper` was silently dead for ~2 weeks. Its plist ran
`/usr/bin/python3` (an Apple stub resolving to Xcode's python3) against a script
whose real path was on the external volume, because `~/.agents` is a symlink farm
onto `/Volumes/Extreme SSD`. macOS TCC gates "Files on Removable Volumes" and the
launchd context had no grant, so every run failed with
`[Errno 1] Operation not permitted` — 21,380 times, into a 4.1 MB stderr log
nobody read. The "0 lingering task spaces" that made the reaper look healthy came
from agent-invoked manual runs.

Three consequences are baked in here:

* The runtime copy lives in `~/.local/bin` (real internal storage); this tracked
  copy is the source of truth.
* `install.sh` verifies volume residency and interpreter compatibility.
* `reap` truncates its own launchd stdout/stderr logs past 256 KB, and `doctor`
  reports the job's `LastExitStatus` so a silent death is visible.

## State

`~/.local/state/orca-terminal-dispatch/` (0600 throughout, atomic writes copied
field-for-field from Ego's `private_json_write`):

* `terminals.json` — the registry
* `dispatch.lock` — `flock` interlock
* `reaper.log` — rotating action log
* `launchd.{stdout,stderr}.log` — job output, size-capped

## Configuration

| env var | default | meaning |
|---|---|---|
| `ORCA_TERMINAL_DISPATCH_IDLE_SECONDS` | 600 | Idle threshold (mirrors `EGO_ORCA_TASK_IDLE_SECONDS`) |
| `ORCA_TERMINAL_DISPATCH_STATE_DIR` | `~/.local/state/orca-terminal-dispatch` | State location |
| `ORCA_TERMINAL_DISPATCH_REQUIRE_MARKER` | 1 | Require the title marker before closing |
| `ORCA_TERMINAL_DISPATCH_REQUIRE_AGENT_PS` | 1 | Require `worktree ps` corroboration |
| `ORCA_TERMINAL_DISPATCH_ORCA_BIN` | Orca.app's `bin/orca` | `orca` CLI path |
| `ORCA_TERMINAL_DISPATCH_ORCA_TIMEOUT` | 90 | Per-call timeout, seconds |

## Tests

```
python3 test_orca_terminal_dispatch.py
```

Hand-rolled assertions, real filesystem in a tempdir, every `orca` round-trip
replaced by a fake world — the suite never touches a live terminal. The central
case builds a world of idle *unregistered* terminals (including replicas of the
three real traps: a 24-minute-idle session with no agent record, a plain user
shell, and a `title: null` / `lastOutputAt: null` Claude pane) plus one
registered leak, and asserts the reaper closes only the registered one and never
even *asks* about the others.

## Proposed CLAUDE.md rule 1 amendment

Not applied — a global CLAUDE.md edit needs the user's own approval. Suggested
insertion after the existing `orca terminal close ... 不留存悬挂会话` clause:

> （2026-08-28 新增，工具化补强）上面这条"派发完必须自己 close"的要求本身不变，
> 但它此前只以散文形式存在，每个 agent 各自手搓 `orca terminal create` +
> `orca terminal close` 两步，第二步一旦忘记/超时/agent 挂掉就永久泄漏一个终端。
> 现已提供唯一的共享代码路径 `orca-terminal-dispatch`（装在 `~/.local/bin/`，
> 源码在 `完善orca/orca-terminal-dispatch/`）：
> `orca-terminal-dispatch create --worktree active --agent-kind <grok|gemini|codex|...> --purpose "<用途>" --command "<目标 CLI 的 headless 单轮调用>"`，
> 取完结果后 `orca-terminal-dispatch close --terminal <handle>`。
> 它在 create 时把终端登记进一份 0600 私有注册表，close 时销记录；
> 另有一个每分钟运行的 launchd 兜底回收器
> （`com.local.orca-terminal-dispatch-reaper`），只会关闭**这份注册表里**、
> 且身份（handle/ptyId/incarnationId/tabId/leafId + 标题标记）完全未变、
> 且确实空闲超过阈值的终端，绝不触碰任何未登记的交互式会话。
> **兜底不替代手动 close**：仍然要求每次派发后主动 close，回收器只是忘记时的安全网。
> 预期长时间无输出的派发用 `--idle-seconds <秒>` 或定期 `touch` 自行延长阈值。
