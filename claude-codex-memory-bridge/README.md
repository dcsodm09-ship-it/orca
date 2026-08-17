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
- Namespace-scoped by the invoking Codex session's own `cwd`: the hook derives
  Claude Code's project directory name from `cwd` (every non-alphanumeric
  character maps to a literal `-`, matching Claude Code's own naming) and
  reads only that one project's `MEMORY.md` — never any other project's. A
  missing or non-absolute `cwd`, or a workspace with no matching Claude
  project yet, fails closed to no context rather than falling back to
  scanning every project.
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
