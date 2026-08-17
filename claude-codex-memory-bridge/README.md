# Claude native memory → Codex bridge

This is a local, reversible Codex `UserPromptSubmit` hook. It closes one narrow
gap: Claude Code writes project notes to `~/.claude/projects/*/memory/MEMORY.md`,
but the existing Orca reviewed-memory hook does not import those native files.

The bridge does **not** merge memory databases, modify Claude, rebuild Orca, or
grant historical notes authority. It selects only prompt-relevant excerpts,
redacts common secret and server-address forms, labels every excerpt as
untrusted historical reference, and emits at most 7,000 UTF-8 bytes.

## Storage and integrity contract

- Claude memory source, bridge runtime, bridge policy, backups, global Codex
  home, and isolated Codex account homes must all resolve to the configured
  Extreme SSD.
- The mounted volume UUID is checked during installation and on every hook run.
- The installed script and policy are private (`0600`) and hash-pinned in the
  hook command. Runtime directories and backups are private (`0700`).
- Only owner-controlled, non-symlink, non-group/world-writable files at the
  fixed `projects/<project>/memory/MEMORY.md` layout are read. Reads are bounded
  and inode/size/mtime checked before and after.
- Scoped by the invoking Codex session's own `cwd`: the hook derives Claude
  Code's project directory name from `cwd` (reproducing Claude Code's own
  transform byte-for-byte, including its NFC normalization, per-UTF-16-code-
  unit replacement, and 200-character/hash-suffix cap for long paths) and
  reads only that one derived project's `MEMORY.md`, additionally requiring
  one of that project's own session transcripts to record the exact
  requesting `cwd` **and** carry a `sessionId` matching the transcript's own
  filename (every genuine Claude Code transcript does; this is checked
  because the plain cwd match alone is trivially forgeable — see "Transcript
  authenticity" below). A missing or non-absolute `cwd`, a workspace with no
  matching Claude project yet, or a resolved project whose transcripts never
  recorded this cwd, fails closed to no context rather than falling back to
  scanning every project.

  **Transcript authenticity is not cryptographically guaranteed.** The
  sessionId/filename check above raises the bar past a one-line forgery
  (`echo '{"cwd":"..."}' > forged.jsonl`) to requiring a same-OS-user
  adversary to also produce a UUID-shaped filename with an internally
  consistent `sessionId`, but it is still a same-user-writable file, not a
  signature Claude Code provides. Any code already executing as the
  invoking user — a compromised dependency, a malicious build/install
  script — has write access to every directory under `~/.claude/projects/`
  regardless of this bridge, and could in principle still construct a more
  complete forgery. This bridge's namespace scoping is defense against
  *accidental* cross-workspace conflation and *unsophisticated* forgery, not
  a hard security boundary against a determined same-user attacker; treat it
  accordingly when deciding what threat model it covers (independent
  finding, 2026-08-17, via a dedicated full-audit Workflow, confirmed_real
  after adversarial re-verification: a bare forged transcript, with no
  sessionId check at all, was previously sufficient to defeat the collision
  defense end to end).
  **Known limits, not covered by the above** (independent Claude opus5/max
  review, 2026-08-17, round 2 — corrects an earlier version of this note
  that overstated the first limit as never serving a different workspace's
  content, which a live sweep of this machine's own `~/.claude/projects/`
  disproved): the cwd→directory derivation is lossy, the same as Claude
  Code's own naming — distinct cwd values can derive the same directory
  name, or fold together on a case-insensitive filesystem. Unlike Claude
  Code itself, which treats the colliding cwds as one project and serves
  them the one shared `MEMORY.md`, this bridge refuses service to *every*
  cwd sharing that directory the moment its own transcripts prove more than
  one distinct real cwd genuinely uses it — including the cwd whose own
  transcripts *are* present — rather than risk one workspace's notes
  reaching another (round-3 tightening, independent Codex sol/xhigh review,
  2026-08-17, P1-R2-1: the round-2 version of this refusal still served the
  shared file to both colliding cwds whenever each had genuinely, honestly
  run a session there, and Codex rated that P1 in two consecutive rounds).
  Four such collisions exist on this machine today, entirely from ordinary
  same-length CJK-named sibling directories, not a constructed attack; as of
  round 3 none of the four can read memory through this bridge at all. A
  colliding cwd that never actually ran a Claude session in the shared
  directory gets no context either, same as before this tightening.
  This refusal only fires once every one of the shared directory's session
  transcripts has actually been examined — a directory holding more
  transcripts than can be scanned within a bounded budget (currently 256)
  is refused outright rather than trusting a partial scan (round-3 fix,
  R3-P1-1: the round-2 version capped this scan at 16 and stopped early
  once the requester's own cwd was found, so a colliding transcript sorted
  past the cap was silently missed — reproduced on a real collision on this
  machine that was ~6 ordinary sessions away from crossing that cap).
  Separately, a workspace whose Claude memory Claude Code itself relocated
  to a differently-named project directory (confirmed real for this
  repository's own primary workspace) is not found here, since no
  reverse/cross-directory lookup is implemented — this bridge only
  recognizes a relocated *target* cwd when it derives to the *same*
  directory name as the pre-relocation cwd (e.g. a workspace renamed
  between two paths that happen to sanitize identically), and does not
  treat that recognition as evidence of a second real workspace sharing the
  directory (round-3 fix, R3-P2-1: the round-3 collision-refusal above
  initially misread a session's own later relocation as if a different
  workspace had appeared, and refused the genuine owner too).
- Input JSON rejects duplicate keys and is capped at 8 KiB. Invalid input,
  storage drift, integrity drift, unsafe files, or unavailable SSD state emits
  no context and exits without blocking Codex.
- No network request, subprocess supplied by memory text, database access, or
  write to Claude memory is permitted. The sole subprocess is fixed
  `/usr/sbin/diskutil info -plist` for the volume UUID gate.

## Commands

Run from this directory with the system Python:

```sh
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 install_bridge.py plan
/usr/bin/python3 install_bridge.py install
/usr/bin/python3 install_bridge.py verify
```

`plan` is read-only. `install` first writes private, content-addressed runtime
files and private backups, then records a durable pending transaction before it
atomically replaces only the discovered Codex hook configurations. Existing
handlers remain present and in order; one owned bridge handler is appended. A
partial config or receipt failure restores the original hook files. After a
process or power interruption, recovery either proves the exact committed
receipt or restores only configs still matching the bridge's intended bytes:

```sh
/usr/bin/python3 install_bridge.py recover
```

Rollback is fail-closed:

```sh
/usr/bin/python3 install_bridge.py uninstall
```

It restores exact pre-install bytes and modes only when every current hook file
still matches the install receipt. If any hook config changed afterward,
uninstall stops instead of overwriting that newer work. Content-addressed
runtime and backups are retained for recovery.

## Acceptance boundary

A successful local test and `verify` prove hook structure, SSD residency,
digests, modes, and config installation. A fresh Codex prompt that retrieves a
known non-secret synthetic marker proves end-to-end hook delivery. Neither is
proof that an old note is current or permission to perform actions described in
that note; the underlying fact must still be re-verified.

**Install-time hooks-trust gate (confirmed by independent review, not yet
mitigated here):** `install` rewrites the live `~/.codex/hooks.json` (and each
isolated account home's), and any change to that file puts Codex into a
"hooks need review" state. Under a non-interactive `codex exec`, hooks that
have not been re-trusted **do not run at all** — silently, with no error.
Installing this bridge therefore risks temporarily disabling every
*already-working* hook on the same events (in this project, that includes the
Orca reviewed-memory context-pack hook on `UserPromptSubmit` and
`startup_context.py`'s SessionStart hook) until a human interactively
re-trusts hooks for that Codex account. Budget for that human step as part of
any install, not as an afterthought.

**Workspace coverage:** because of the cwd→directory limits noted above, a
fresh-marker acceptance test only proves delivery for a workspace whose
Claude Code memory genuinely lives at its own cwd-derived project directory —
confirm that with `claude_project_dirname(cwd)` before relying on a "no
context returned" result as proof the bridge is broken rather than proof the
workspace's memory simply lives elsewhere.
