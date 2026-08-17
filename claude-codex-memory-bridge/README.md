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
  requesting `cwd`. A missing or non-absolute `cwd`, a workspace with no
  matching Claude project yet, or a resolved project whose transcripts never
  recorded this cwd, fails closed to no context rather than falling back to
  scanning every project.
  **Known limits, not covered by the above** (independent Claude opus5/max
  review, 2026-08-17, round 2 — corrects an earlier version of this note
  that overstated the first limit as never serving a different workspace's
  content, which a live sweep of this machine's own `~/.claude/projects/`
  disproved): the cwd→directory derivation is lossy, the same as Claude
  Code's own naming — distinct cwd values can derive the same directory
  name, or fold together on a case-insensitive filesystem. When that
  happens, this bridge — like Claude Code itself — treats the colliding
  cwds as one project and serves them the one shared `MEMORY.md`; four such
  collisions exist on this machine today, entirely from ordinary same-length
  CJK-named sibling directories, not a constructed attack. What the
  session-transcript check *does* close is a colliding cwd that never
  actually ran a Claude session in that shared directory at all — that case
  gets no context, not the other project's content. Separately, a workspace
  whose Claude memory Claude Code itself relocated to a differently-named
  project directory (confirmed real for this repository's own primary
  workspace) is not found here, since no reverse/cross-directory lookup is
  implemented.
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
