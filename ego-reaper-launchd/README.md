# ego-reaper-launchd

Installer for the two Ego cleanup launchd jobs, `com.local.ego-taskspace-reaper`
and `com.local.ego-idle-reaper` — the Ego equivalent of "close it when you're
done".

```sh
./install.sh              # copy scripts to internal disk, render plists, reload both jobs, verify
./install.sh --doctor     # check only
./install.sh --uninstall  # unload and remove both plists
```

## The bug this fixes

Both jobs were silently dead for **~14.8 days** (since 2026-08-13). `launchctl
list` reported `LastExitStatus 2`, and the two stderr logs had grown to 4.1 MB
and 3.8 MB containing, between them, **exactly four distinct lines** — all
variants of:

```
/Applications/Xcode.app/Contents/Developer/usr/bin/python3: can't open file
'/Users/www1adwawd/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py':
[Errno 1] Operation not permitted
```

Two independent problems compounded:

1. **The script path resolved onto a removable volume.** `~/.agents` and
   `~/claudecode` are symlinks onto `/Volumes/Extreme SSD`. macOS TCC gates
   *Files on Removable Volumes*, and a launchd agent has no such grant, so the
   interpreter was denied before it could open the file — it never began
   executing, which is why nothing appeared in any application-level log.
2. **The interpreter was an Apple platform binary.** `/usr/bin/python3` is a
   stub that re-execs `$(xcode-select -p)/usr/bin/python3`. Command Line Tools
   are not installed on this machine, so that stub *only* worked because
   Xcode.app happened to be present — a fragile indirection independent of the
   TCC problem.

The only recent successful reaps came from agents invoking the script by hand
during interactive sessions, which run under a TCC context that *does* have the
grant. That is what made the breakage look like it was working.

## Measurements

Run under a real throwaway launchd job on this machine (2026-08-28), not from an
interactive shell — the distinction matters, because an interactive shell
inherits its terminal's removable-volume grant and cannot reproduce the failure:

| test | result |
|---|---|
| `/bin/cat` reading the source on `/Volumes/...` | **DENIED** (Errno 1) |
| `/usr/bin/python3` running the source on `/Volumes/...` | **DENIED** (exit 2) |
| `/opt/homebrew/bin/python3` running the source on `/Volumes/...` | OK |
| `/usr/bin/python3` running an internal-disk copy | OK |
| `/opt/homebrew/bin/python3` running an internal-disk copy | OK |

The `/bin/cat` result is the important one: the denial is **not** specific to
Xcode's python3. It applies to Apple platform binaries in the launchd session
generally, so "switch to `/usr/bin/python3`" — the real Apple stub — would not
have fixed anything on its own.

## Why copy-to-internal, and not just change the interpreter

Homebrew's ad-hoc-signed python3 escapes the denial today, so
interpreter-swap-alone *appears* sufficient. It was rejected as the primary fix:

- it rests on a TCC **attribution quirk** Apple can tighten in any OS update;
- it requires Homebrew to stay installed at that exact path;
- it still breaks whenever the SSD is unmounted — the archived logs contain
  `[Errno 2] No such file or directory` lines from exactly that;
- it is not what already works here: `~/.local/bin/orca-terminal-dispatch`
  copies to internal disk and has been sitting at `LastExitStatus 0`.

So the fix is **both**, as two independent guards: the launchd-invoked copy
lives on internal disk (`~/.local/libexec/ego-reaper/`), *and* the plists prefer
Homebrew's python3. Either one alone would keep the jobs alive.

Both scripts are stdlib-only, use no `__file__`, and touch only internal-disk
runtime paths (`~/.local/state/...`, `~/.config/orca/...`,
`~/.local/bin/ego-browser` → `~/Applications/ego lite.app/...`), so a plain copy
is a complete relocation — nothing reaches back to the SSD at runtime.

`~/.local/libexec/` is used rather than `~/.local/bin/` deliberately: these are
launchd-only snapshots, and putting a second copy of the router on `PATH` would
invite agents to call the drifting copy instead of the canonical skill path.

## Sources of truth (not in this repo, and not version-controlled)

| source | installed copy |
|---|---|
| `~/.agents/skills/ego-orca-profile/scripts/ego_profile_router.py` | `~/.local/libexec/ego-reaper/ego_profile_router.py` |
| `~/claudecode/本机/tools/ego_idle_reaper.py` | `~/.local/libexec/ego-reaper/ego_idle_reaper.py` |

Both live on the SSD outside any git repository (`local-homes/.agents` and
`external-projects/claudecode` are not git repos), so this installer is the only
version-controlled part. It never edits the sources.

## The trade-off you must know about

The installed copies are **snapshots**. Edit a source script and the running job
keeps using the old code until you re-run `./install.sh`.

That is the same trade-off `orca-terminal-dispatch` accepts, but here it is
backed by a drift check: doctor mode compares each installed copy against its
source by sha256 and fails on mismatch, so a stale copy cannot rot unnoticed the
way the original breakage did. Doctor mode checks four things per job:

1. no `ProgramArguments` absolute path resolves under `/Volumes/`;
2. the installed copy matches its source (or reports the SSD as unmounted, in
   which case the job correctly keeps running against the last good copy);
3. `launchctl list` reports `LastExitStatus 0`;
4. no `Operation not permitted` in the recent stderr tail.

`install.sh` runs doctor automatically after loading and exits non-zero if it
fails — skipping that check is precisely how the original breakage survived
14.8 days.

## Notes

- `plutil -lint` accepts a double hyphen inside an XML comment; `expat` does
  not. The installer validates rendered plists with **both**.
- The pre-fix logs are archived (7.9 MB → 31 KB gzipped) beside the live logs as
  `*.pre-tcc-fix-20260828.gz`. They contain no data other than the four repeated
  error lines; this was verified before truncating.
