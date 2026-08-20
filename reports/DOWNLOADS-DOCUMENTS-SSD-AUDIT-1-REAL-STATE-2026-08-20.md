# Audit 1: Downloads/Documents 现场真实状态

# Investigation Report — `~/Downloads` (dh-work) and `~/Documents`

All commands below were run directly on this machine (read-only; nothing was mounted, moved, deleted, or modified). Machine: Mac mini, internal APFS boot disk (`disk0`/`disk3`, 245GB), external "Extreme SSD" (`disk6`/`disk7`, 2TB, USB, currently mounted at `/Volumes/Extreme SSD` — this is the drive Claude Code itself is running from).

## Part 1 — `~/Downloads` → `/Volumes/dh-work/...`

**Symlink confirmed:**
```
$ readlink ~/Downloads
/Volumes/dh-work/_mac_migrated/Users/www1adwawd/Downloads
$ ls -la ~/Downloads
lrwxr-xr-x@ 1 www1adwawd staff 57 Jul 18 16:51 /Users/www1adwawd/Downloads -> /Volumes/dh-work/...
```
Symlink itself was created **Jul 18 16:51:44 2026** (`stat`).

**No "dh-work" volume is attached anywhere right now.** `diskutil list`, `mount`, `diskutil apfs list`, and `system_profiler SPStorageDataType` were cross-checked — the complete current volume set is: internal boot volumes (Macintosh HD / Macintosh HD - Data / VM / Recovery), a 1.4GB disk image mounted as `百度网盘`, the external `Extreme SSD` (2TB, 1.71TB free), and a small read-only `Kiro CLI` disk image. Nothing named or resembling `dh-work`.

**No unmounted external drive is physically attached either.** `system_profiler SPUSBDataType` returned no USB devices. `system_profiler SPThunderboltDataType` showed both TB/USB4 ports: `Status: No device connected`. So this is *not* "the drive is plugged in but not mounted" — no extra physical drive is connected at all.

**Root cause found — `dh-work` is a disk-image-backed virtual volume, not a physical drive:**
```
$ find "$HOME" -maxdepth 4 -iname "*dh-work*"
/Users/www1adwawd/Library/LaunchAgents/com.user.dh-work.mount.plist
```
That LaunchAgent (enabled, `RunAtLoad`, confirmed loaded via `launchctl print gui/501/com.user.dh-work.mount`) runs at every login:
```
/usr/bin/hdiutil attach -nobrowse -mountpoint /Volumes/dh-work /Volumes/Extreme SSD/dh-work.sparseimage
```
So `dh-work` is meant to be a sparse disk image (`dh-work.sparseimage`) living **on the very Extreme SSD that is currently mounted and healthy** — not a separate physical drive that could be "plugged back in."

**That image file is missing.**
```
$ ls -la "/Volumes/Extreme SSD/dh-work.sparseimage"
ls: /Volumes/Extreme SSD/dh-work.sparseimage: No such file or directory
$ hdiutil imageinfo "/Volumes/Extreme SSD/dh-work.sparseimage"
hdiutil: imageinfo failed - 无此文件或目录 (No such file or directory)
```
The LaunchAgent's own log confirms this isn't new — its last real run (log file born **Aug 16 17:36:15 2026**) recorded the identical failure:
```
$ cat /tmp/dh-work-mount.log
hdiutil: attach failed - 无此文件或目录
```
`launchctl print` corroborates: `state = not running`, `job state = exited`, `last exit code = 1`, `runs = 1`.

**Searched exhaustively for the file anywhere reachable on this machine** — not found:
- `find / /Volumes -maxdepth 5 -iname "*.sparseimage" -o -iname "*.sparsebundle"` → nothing.
- `mdfind "kMDItemFSName == '*.sparseimage'"` (Spotlight, indexing confirmed enabled on Extreme SSD) → nothing.
- `/Volumes/Extreme SSD/.Trashes/501` → contains other deleted project folders (hgfast-*, orca, __pycache__) but nothing dh-work related.
- 7-day unified log search (`log show --last 7d --predicate 'eventMessage contains "dh-work"'`) → only routine `backgroundtaskmanagementd` bookkeeping entries re-registering the LaunchAgent's existence today; no mount-success events, no extra diagnostic clues.

**Verdict:** Genuinely unreachable, and this is *not* a "reconnect the drive" situation — no such drive exists as a separate physical device. The backing `dh-work.sparseimage` file itself is absent from the one place (the currently-mounted Extreme SSD) it's configured to live. I found zero trace of it under that name anywhere currently accessible on this Mac (not in Trash, not Spotlight-indexed, not found by filesystem search to depth 5). **I could not determine** whether it was deleted, renamed, or never fully migrated onto Extreme SSD in the first place, or whether an untouched copy exists on some other drive not currently connected — that's beyond what's visible on this machine. Per instructions I did not run `hdiutil attach` myself (would mount/modify state); the only image-inspection command I ran (`hdiutil imageinfo`) is read-only and failed solely because the file isn't there.

## Part 2 — `~/Documents`

**Confirmed real local directory, not a symlink:**
```
$ readlink ~/Documents        → (empty, exit 1)
$ file ~/Documents            → /Users/www1adwawd/Documents: directory
```
Lives on the internal boot disk's Data volume (`disk3s5`, physical `APPLE SSD AP0256Z`, disk0), mounted at `/System/Volumes/Data`.

**Contents — exact count and size:**
```
$ find ~/Documents -type f | wc -l   → 12
$ du -sh ~/Documents                 → 196M
```
12 flat entries, no subdirectories (11 real files + `.DS_Store`):

| File | Size | Type |
|---|---|---|
| 鲸涅-经纪合作合同-审核意见.docx | 188K | Word doc — name indicates a contract review opinion (not opened) |
| 20260820鲸涅-文书撰写-经纪合作合同.docx | 100K | Word doc — contract drafting (not opened) |
| 20260821鲸涅-文书撰写-经纪合作合同-修订版.docx | 36K | Word doc — contract, revised version (not opened) |
| 20260811094317329-APIServer_v5.3.6.zip | 1.4M | Zip archive |
| 20260820久堡招聘JD合集-4主播1运营.pdf | 840K | PDF, 6 pages — recruiting JD compilation |
| 炼刀AI产品介绍 v1.8.34(2).pdf | 34M | PDF, 19 pages — product deck |
| 燃客AI获客增长中台.pdf | 2.2M | PDF, 9 pages |
| ai.xlsx | 20M | Excel spreadsheet |
| phonic-axle-506014-g3-768f94d52338.json | 4.0K | JSON — filename pattern matches a **GCP service-account key file** (project-id + hash); flagged as likely credential, not opened |
| phonic-axle-506014-g3-ea50b49d0d6c.json | 4.0K | JSON — same pattern, second key |
| XTerminal-5.8.6-mac-arm64.dmg | 137M | macOS app installer disk image |
| .DS_Store | 8.0K | Finder metadata |

None of the docx/pdf content was read or quoted, per instructions.

**Boot disk free space, precisely:**
```
$ df -h /System/Volumes/Data
Filesystem      Size    Used   Avail Capacity
/dev/disk3s5   228Gi   177Gi    18Gi    91%
```
Matches the ~92%/~17GB figure closely (91% used, 18GiB avail — small variance is GiB-vs-GB rounding).

**iCloud Desktop & Documents sync — distinguishing general iCloud Drive from the folder-redirect feature:**
```
$ defaults read MobileMeAccounts | grep MOBILE_DOCUMENTS
Name = "MOBILE_DOCUMENTS"; ServiceID = "com.apple.Dataclass.Ubiquity";   ← general iCloud Drive service, present for both accounts
```
This only proves iCloud Drive *in general* is set up. The specific Desktop/Documents redirect toggle lives in Finder's own prefs:
```
$ defaults read com.apple.finder | grep FXICloudDrive
FXICloudDriveDeclinedUpgrade = 0;
FXICloudDriveDesktop = 0;        ← OFF
FXICloudDriveDocuments = 0;      ← OFF
FXICloudDriveEnabled = 1;        ← iCloud Drive general is ON
```
Corroborated directly: `~/Library/Mobile Documents/com~apple~CloudDocs/` was listed in full and contains **no `Desktop` or `Documents` subfolder** — the exact location macOS creates when this feature is active and takes over those folders.

**Verdict:** iCloud Drive is generally active on this account, but Desktop & Documents folder sync is explicitly OFF (`FXICloudDriveDesktop=0`, `FXICloudDriveDocuments=0`). `~/Documents` is confirmed a **fully ordinary, local-only directory** today — a plain symlink swap would not conflict with any currently-active Apple sync daemon. Caveat: this is a toggle a user (or an iCloud prompt) could flip on later, at which point macOS would silently reclaim `~/Documents`; that's a forward risk to note, not a present conflict.

**Open file handles:**
```
$ lsof +D ~/Documents   (ran with a manual 20s guard; no `timeout` binary present on this system)
→ completed in <20s, zero output
```
No process currently has anything open inside `~/Documents`. (Practical to run directly — the directory is small/flat, 12 entries.)

## Part 3 — Spotlight indexing & Time Machine

```
$ mdutil -s /                      → Indexing enabled.
$ mdutil -s /System/Volumes/Data   → Indexing enabled.   (where ~/Documents lives)
$ mdutil -s "/Volumes/Extreme SSD" → Indexing enabled.   (where dh-work.sparseimage is expected, and any migration target)
```
Spotlight indexing is on for both the current source location and the external SSD.

```
$ tmutil destinationinfo
tmutil: No destinations configured.
$ tmutil status
Running = 0;
```
**Time Machine has no destination configured at all on this Mac right now** — neither folder is actually being backed up currently, independent of any inclusion/exclusion setting.

```
$ tmutil isexcluded ~/Documents ~/Downloads
[Included]  /Users/www1adwawd/Documents
[UNKNOWN]   /Users/www1adwawd/Downloads
```
`~/Documents` would be included by default if a TM destination were later configured. `~/Downloads` returns `[UNKNOWN]` — almost certainly because `tmutil` cannot resolve/stat a symlink whose target is currently unreachable (the same dh-work problem from Part 1 surfacing again here).

## Summary — what's directly verified vs. undetermined

**Directly verified:** dh-work is a disk-image-backed virtual volume (not a physical drive), its `.sparseimage` file is absent from its expected location on the currently-mounted Extreme SSD, no such file exists anywhere else reachable on this machine, and the failure is reproducible/logged from the last real login attempt. `~/Documents` is a genuine local-only 196MB/12-file directory on a 91%-full boot disk, not touched by iCloud's Desktop/Documents sync (which is explicitly disabled), with no open handles and no Time Machine destination currently configured for either folder.

**Could not be determined from this machine:** what happened to the missing `dh-work.sparseimage` (deleted vs. moved vs. never fully migrated) or whether an intact copy exists on any drive not currently connected to this Mac.