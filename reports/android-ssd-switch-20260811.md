# Android heavy-data SSD switch report

Status: **SWITCHED and runtime-validated**. The rollback path was verified read-only as `rollback_ready`; rollback was not executed because that would undo the requested switch.

Transaction: `switch-20260810T160702Z-f01fe69f3d1a` (UTC; local date 2026-08-11)

## Pre-switch gates

Command:

```text
python3 scripts/switch_android_heavy_data_to_ssd.py switch \
  --cold-path '/Volumes/Extreme SSD/Orca/archive/developer-data/android-cold-20260810T152233Z'
```

Readback:

- SSD five-part gate: `uuid=true`, `apfs=true`, `filevault=true`, `unlocked=true`, `owners=true`.
- Expected SSD UUID: `6EF3720E-C57F-4352-BA47-CFE88BFEFDA7`.
- Orca app/runtime: running, reachable, `ready`, app version `1.4.177`.
- Android inventory before mutation: `adb=[]`, `emulator=[]`, `studio=[]`.
- Source lsof before mutation: `~/.android/avd=0`, `~/Library/Android/sdk/system-images=0`.
- Existing AVD: `Orca_Pixel_9_API_36`.
- qemu-img triplet validation: source/cold/active identities equal, three QCOW2 paths, zero `check-errors`.

## Published copy evidence

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| cold `COMPLETED.json` | 54,002 | `1cc58d26095b0dda58f2797318e8fdc245fa31c4583ae3cc43310352d53d1501` |
| active `COMPLETED.json` | 53,914 | `a705bdc844a3cc2463e1f5c75e9d0605ce426268bf6a38e1a3072353ac212f86` |

Paths:

- Cold: `/Volumes/Extreme SSD/Orca/archive/developer-data/android-cold-20260810T152233Z`
- Active: `/Volumes/Extreme SSD/Orca/developer-data/android`

All twelve manifest files are regular `0600` files and matched the digests embedded in their corresponding `COMPLETED.json`.

| Role / manifest | avd SHA-256 | system-images SHA-256 |
|---|---|---|
| source-pre (cold and active) | `828191c4a7920613243081bcee2476a62d0e146fda85b37864861ece75b7f4ee` | `4ee530378e6751e07d781d304bbe92e9dc8199df1a56a291da4c9bf4827b82c0` |
| source-post (cold and active) | `828191c4a7920613243081bcee2476a62d0e146fda85b37864861ece75b7f4ee` | `4ee530378e6751e07d781d304bbe92e9dc8199df1a56a291da4c9bf4827b82c0` |
| cold target (copy-time raw) | `3502c81d4c7771580cd56d8932d430fdd1ca2bdc6fec6d376559c0f22941cbc4` | `d042f4bb7640ccd8dc1bd912cf21121a26ddcb0fdf02a099e6314a0e71669fb9` |
| active target (copy-time raw) | `84c4f8ab4fd274181a30f47aba85955f3d8bb0693f9400a359891f46b7ce6545` | `e62c0251b24a25bddd4b9a26c4d33e63882472f05be541ce7966005f390b67f2` |

Fresh pre-switch directory re-hashing also passed. APFS may change physical `allocated_bytes` and macOS may change only the value hash of `com.apple.provenance`; after applying exactly those two copy-script-declared tolerances, all three identities matched:

| Tree | Source raw SHA-256 | Cold/active raw SHA-256 | Shared identity SHA-256 |
|---|---|---|---|
| avd | `828191c4a7920613243081bcee2476a62d0e146fda85b37864861ece75b7f4ee` | `e09acb8dd91c0b9ed7cee9d0c079c7ba6e909a1faa63197bf9b77f62207df2f4` | `1b1a0c639518ae301522f8b659e61182df4971f81a28fd949b7a8351f1b6c173` |
| system-images | `4ee530378e6751e07d781d304bbe92e9dc8199df1a56a291da4c9bf4827b82c0` | `a588864791f1924f1bcf2a7a5a62ee58eee6c84f0b777f210882da678a7f9c8c` | `8f75b9a3f26eaa409f04a573a1280289824de47e622c052a259d6c1c1352f131` |

## Switch execution

Command:

```text
python3 scripts/switch_android_heavy_data_to_ssd.py switch \
  --cold-path '/Volumes/Extreme SSD/Orca/archive/developer-data/android-cold-20260810T152233Z' \
  --execute
```

Final script:

- Path: `scripts/switch_android_heavy_data_to_ssd.py`
- Current SHA-256: `2d7f57107ce7ff3fc322fb27223815ce3bf4b362b3812d9363660e05d0795e14`
- The preimage also records the exact earlier script SHA-256 that performed the switch; the script was subsequently narrowed for the observed macOS provenance behavior and the read-only rollback check.
- Syntax compile passed. A second `switch --execute` returned `already_switched`, proving idempotent current-state handling.

Secure transaction state:

| Artifact | Mode | Bytes | SHA-256 |
|---|---:|---:|---|
| `/Volumes/Extreme SSD/Orca/recovery/android-ssd-switch/switch-20260810T160702Z-f01fe69f3d1a/PREIMAGE.json` | `0600` | 68,569 | `68708b048e7807799595cc1cc4432437b280c719506428d3d07584a436e84925` |
| `/Volumes/Extreme SSD/Orca/recovery/android-ssd-switch/switch-20260810T160702Z-f01fe69f3d1a/SWITCHED.json` | `0600` | 3,403 | `2c55546b7c49c25388fa33568e17fc25c3365058ed153c16350f93130fbef474` |
| `/Volumes/Extreme SSD/Orca/recovery/android-ssd-switch/switch-20260810T160702Z-f01fe69f3d1a/RELOCATION_PREPARED.json` | `0600` | 1,396 | `2b8286d946eb95afae8de3f9c1ba2e3644dce31294fb3cb50fdc11bd61a8cf77` |
| `/Volumes/Extreme SSD/Orca/recovery/android-ssd-switch/switch-20260810T160702Z-f01fe69f3d1a/RELOCATION_COMPLETED.json` | `0600` | 1,180 | `72069c1724208452afc13dc12eb7b5f82aa9487473aa9e91f113e2cceb360a09` |

The authority hierarchy is entirely on SSD device `16777239`: `/Volumes/Extreme SSD/Orca/recovery`, `android-ssd-switch`, and the transaction directory are owner-only `0700`. The first switch wrote 68,237-byte and 3,113-byte internal-disk state files; after the coordinator tightened the authority path, the relocation flow rewrote their embedded authority references, verified every old/new SHA, fsynced a same-SSD staging directory, atomically published it, and then removed only those two verified legacy files plus now-empty directories. `/Users/www1adwawd/Library/Application Support/Orca/android-ssd-switch` is absent. Re-running the relocation command returns `already_relocated`.

Resulting links:

```text
/Users/www1adwawd/.android/avd
  -> /Volumes/Extreme SSD/Orca/developer-data/android/avd

/Users/www1adwawd/Library/Android/sdk/system-images
  -> /Volumes/Extreme SSD/Orca/developer-data/android/system-images
```

Both `realpath` results equal the explicit SSD targets. The link inodes live on internal device `16777232`; the target directories live on SSD device `16777239` and the expected SSD UUID.

Retained originals:

| Tree | Retained path | Root mode | Owner | Device | Volume UUID | Original inode | Logical file bytes | Pre-switch allocated bytes | lsof |
|---|---|---:|---|---:|---|---:|---:|---:|---:|
| avd | `/Users/www1adwawd/.android/avd.orca-ssd-retained-20260810T160702Z-f01fe69f3d1a` | `0555` | `www1adwawd:staff` | 16777232 | `5668965E-62BF-4C49-916E-5E34A2325955` | 45678756 | 9,006,234,168 | 8,942,436,352 | 0 |
| system-images | `/Users/www1adwawd/Library/Android/sdk/system-images.orca-ssd-retained-20260810T160702Z-f01fe69f3d1a` | `0555` | `www1adwawd:staff` | 16777232 | `5668965E-62BF-4C49-916E-5E34A2325955` | 45683375 | 2,419,953,352 | 2,420,027,392 | 0 |

The full retained trees were checked and contained zero write-enabled non-symlink paths. No original content was deleted.

## Orca Android runtime validation

Only the version-matched `orca-emulator-android` workflow and `orca emulator` commands were used; raw adb was not used.

Command/evidence sequence:

1. `orca emulator devices --worktree current --json`
   - Found `Orca_Pixel_9_API_36`, backend `android`, `shutdown`, available.
2. `orca emulator attach 'Orca_Pixel_9_API_36' --worktree current --json`
   - Returned `attached=true`, backend `android`, device `emulator-5554`.
3. `orca emulator devices --worktree current --json`
   - Reported `Orca_Pixel_9_API_36`, device `emulator-5554`, state `booted`.
4. `orca emulator ax --device 'emulator-5554' --worktree current --json`
   - Returned a valid Android AX tree for package `com.hgfast`, including the login page and enabled controls. No text was entered; no credential/token/cookie value was read or persisted.
5. `orca emulator shutdown --device 'emulator-5554' --worktree current --json`
   - Returned `ok=true` for `emulator-5554`.
6. Final `orca emulator devices --worktree current --json`
   - Reported only the available AVD `Orca_Pixel_9_API_36` in `shutdown` state; no booted Android device remained.

One non-blocking Orca transition was observed: immediately after a successful shutdown, a stale `emulator-5554` row can appear once while the AVD row already says `shutdown`; a repeated shutdown during the first run was misrouted to `simctl` and rejected as an invalid iOS device. After state relocation, a second attach returned `runtime_unavailable`, but a read-only status check showed the runtime recovered and a devices check proved the AVD had actually booted; no attach retry was attempted, and `orca emulator shutdown --device emulator-5554` returned `ok=true`. The final devices read was clean with only `Orca_Pixel_9_API_36=shutdown`, so neither incident broadened the backend or required a raw fallback.

## Recovery readiness

Command (read-only; no `--execute`):

```text
python3 scripts/switch_android_heavy_data_to_ssd.py rollback \
  --marker '/Volumes/Extreme SSD/Orca/recovery/android-ssd-switch/switch-20260810T160702Z-f01fe69f3d1a/SWITCHED.json'
```

Readback: `status=rollback_ready`.

- Orca Android gate: expected AVD available and `shutdown`; running Android devices `[]`.
- Processes: `emulator=[]`, `studio=[]`; Orca enumeration left idle adb PID `73527`.
- Retained-source lsof: `avd=0`, `system-images=0`.
- Active-target lsof: `avd=0`, `system-images=0`.
- Retained avd identity SHA-256 after restoring mode fields in-memory: `1b1a0c639518ae301522f8b659e61182df4971f81a28fd949b7a8351f1b6c173`.
- Retained system-images identity SHA-256 after restoring mode fields in-memory: `8f75b9a3f26eaa409f04a573a1280289824de47e622c052a259d6c1c1352f131`.
- macOS changed only `com.apple.provenance` values on rename/chmod (20 avd entries, 47 system-image entries); path sets, xattr byte lengths, file hashes, mtimes, ACLs, all other xattrs, and allocated-path counts passed the narrow copy-script tolerance.

Rollback remains explicit and requires `--execute`. It restores original modes from the 0600 preimage, removes only the exact expected symlinks, atomically renames the retained directories back, and compensates back to the switched state if rollback itself fails.

## Preserved failed staging

No source, failed staging directory, or failure marker was removed. The following six failed staging directories remain:

```text
/Volumes/Extreme SSD/Orca/archive/developer-data/.android-cold-20260810T150253Z.staging-7563901ff393
/Volumes/Extreme SSD/Orca/archive/developer-data/.android-cold-20260810T150648Z.staging-b548fc131f67
/Volumes/Extreme SSD/Orca/archive/developer-data/.android-cold-20260810T151045Z.staging-6fd13407aea0
/Volumes/Extreme SSD/Orca/developer-data/.android.staging-20260810T150253Z-7563901ff393
/Volumes/Extreme SSD/Orca/developer-data/.android.staging-20260810T150648Z-b548fc131f67
/Volumes/Extreme SSD/Orca/developer-data/.android.staging-20260810T151045Z-6fd13407aea0
```

## Remaining boundaries

- The requested switch is live and validated; rollback was only readiness-checked, not executed.
- Idle adb PID `73527` remains as an Orca device-enumeration side effect. It has no open rows on either retained source or active target, while Orca reports no running Android device; no raw adb command was used to stop it.
- The unrelated NFS mount `/Users/www1adwawd/Cloudflare R2` causes an lsof warning, but the two explicit retained roots and two explicit active targets each returned zero open rows.
