# Independent Round-2 Review — claude-codex-memory-bridge

- **Reviewer:** Claude `opus` / effort `max`, read-only, independent (no contact with the parallel Codex `sol`/`xhigh` round-2 reviewer)
- **Date:** 2026-08-17
- **Candidate:** `01e65fd7f40d00feabdb66d78880fd8ba6297816` (delta vs round-1 `b9ce3e1e62259cb99112d50f972f94fc826c7e9c`)
- **Scope:** `claude-codex-memory-bridge/{claude_memory_hook.py,README.md,tests/test_claude_memory_hook.py}` (`install_bridge.py` untouched this round; verified)
- **Worktree state:** clean at HEAD for the reviewed directory (`git diff HEAD -- claude-codex-memory-bridge/` empty), so the reviewed bytes are the committed bytes.

## Verdict: **GO, conditional**

All three round-1 P1s are addressed. **No P0 and no P1 found in this round.** Two of the three P1s are fully closed and independently re-derived from the binary; the third (P1-1 collision) is closed for the exact attack that was reported, and its residual is Claude Code's own memory-sharing behaviour, not a new boundary crossing.

The conditions are documentation and output-quality defects, not security blockers:

- **N1 must be corrected before install** — the README asserts a security property that is false and reproducible on this machine today.
- **N2 / N3** are round-2 output regressions worth fixing in the same pass.

---

## Method (what I verified myself, not from the task description)

I re-derived the ground truth rather than accepting the candidate's account of it.

1. **Disassembled the installed binary myself.** `~/.local/share/claude/versions/2.1.233` (Mach-O arm64 Bun single-file; `claude --version` → `2.1.233`; `which claude` resolves to the same inode via the SSD symlink). Byte-window extraction at the offsets I located independently:

   | Offset | Extracted |
   |---|---|
   | `269364075` | `xDy`, `fEo`, `WT`, `aT`, `bN`, `QTt`, `lP`, `fWe`, `hJc`, `j3`, `mEo`, `Dqt` |
   | `268726860` | `Iot` (and the parallel `q4c`/`z4c=200` cache-path variant) |
   | `268254731` | `Zu` |
   | `269373074` | `aP=65536`, `Yre=200` |
   | `269355394` | `XTt`, `uEo` |

   Verbatim, as extracted:
   ```js
   function Iot(e){let t=0;for(let r=0;r<e.length;r++)t=(t<<5)-t+e.charCodeAt(r)|0;return t}
   function Zu(e){return e.normalize("NFC")}
   function xDy(e){return Math.abs(Iot(e)).toString(36)}
   function fEo(e){return e.replace(/[^a-zA-Z0-9]/g,"-")}
   function WT(e){let t=fEo(e);if(t.length<=Yre)return t;return `${t.slice(0,Yre)}-${xDy(e)}`}
   function bN(e){return _N.join(aT(),WT(e))}
   function hJc(e,t,r){let n=XTt(e.tail,"relocated","relocatedCwd")??uEo(e.head,"cwd");
     if(n===void 0)return!1;let o=fEo(Zu(n));return r?o.toLowerCase()===t.toLowerCase():o===t}
   ```
   The task description's account of `fEo`/`WT`/`bN`/`Zu`/`Iot`/`Yre` is **accurate**. I did not take it on trust; every constant and branch above came out of my own extraction.

2. **Differential-tested the Python against real JS.** Transcribed the extracted functions verbatim into a Node module (Node v24.19.0) and ran **3,024 cases** through both: hand-picked adversarial inputs (astral emoji, ZWJ sequences, regional indicators, NFC vs NFD pairs, ligatures, boundary lengths 199/200/201, 240-UTF-16-unit emoji runs, CJK) plus a 3,000-case seeded fuzz over an alphabet mixing ASCII, Cyrillic, CJK, astral, combining marks, ZWSP and BOM.

3. **Exercised the real filesystem**: all 75 live `~/.claude/projects/*` directories (read-only), plus five constructed scenarios on a real temp filesystem with real modes and real JSONL.

4. **Ran the suite** on the pinned interpreter.

---

## (d) Test suite — 26/26 genuinely green

```
$ cd claude-codex-memory-bridge && /usr/bin/python3 -m unittest discover -s tests -v
Ran 26 tests in 0.049s
OK
```
Confirmed on `/usr/bin/python3` = **Python 3.9.6**, which is the interpreter `install_bridge.py:311` pins for the installed hook — so the suite runs on the deployment interpreter, not a different one. Re-run after `rm -rf __pycache__ tests/__pycache__` to rule out stale bytecode: still 26/26.

The 7 new tests are substantive, not tautological. Two observations:

- `test_claude_project_dirname_caps_long_paths_with_hash_suffix` hard-codes `len(result) == 207` (a 6-char suffix). That is correct but input-specific; `abs()` of an int32 maxes at `2147483648 < 36**6`, so 6 is the true upper bound and the accompanying `[0-9a-z]{1,6}` regex is right. Deterministic, so not flaky — just brittle if the literal path changes.
- The suite has **no test for the case that actually occurs in production on this machine** — a collision where *both* cwds have their own transcripts (see F-A below). That gap is why N1 slipped through.

---

## (a) The three round-1 P1s

### P1-2 (transform fidelity) — **FULLY FIXED. Verified.**

```
total cases: 3024   mismatches vs WT(Zu(cwd)):  0
                    mismatches vs WT(raw cwd):  1068
                    djb2 hash mismatches:       0
```

`claude_project_dirname()` is now byte-identical to `WT(Zu(cwd))` across every case. The 1,068-case divergence from `WT(raw)` confirms the NFC step is load-bearing rather than decorative, and the DJB2 hash matches `Iot` exactly including JS `|0` signed wraparound and `Math.abs`.

Independent corroboration from live data: `/Users/www1adwawd/claudecode/本机` → `-Users-www1adwawd-claudecode---`, which is the real on-disk directory name. All 65 `(project dir, recorded cwd)` pairs on this machine round-trip with **0 name mismatches**.

The three sub-claims all check out: 200-char cap + DJB2 base-36 suffix present and correct; UTF-16-code-unit replacement correct (one astral char → two `-`); NFC applied to the input, and the hash taken over the *normalized* cwd — matching `WT`'s `xDy(e)` where `e` is `WT`'s own argument, not the sanitized string.

### P1-3 (compressed IPv6) — **FULLY FIXED. Verified.**

22/22 forms redacted, **0 missed**, including every form the round-1 regex passed through: `2001:db8::1`, `fe80::1`, `::1`, `::`, `2001:db8::`, `::a`, `a::`, `2606:4700:4700::1111`, `[2001:db8::1]`, `http://[2001:db8::1]:8080/x`, `2001:db8::/32`, `64:ff9b::192.0.2.33`, `::ffff:192.168.1.1`. IPv4 handling is unchanged and correct (`999.1.1.1` and `1.2.3` correctly left alone).

Two cosmetic residues, no leak: `::ffff:192.168.1.1` → `::ffff:[REDACTED_IP]` (the v4 pass runs first, so the `::ffff:` shell survives), and `fe80::1%eth0` → `[REDACTED_IP]%eth0` (zone id survives — see N9).

### P1-1 (non-injective dirname) — **CLOSED FOR THE REPORTED ATTACK. Residual is Claude-Code-native.**

Real-filesystem test, five scenarios:

| # | Scenario | Result |
|---|---|---|
| 1 | Colliding cwd with **no** transcript of its own | **no context (fail-closed)** ✅ |
| 1 | Unrelated cwd, different derived name | no context ✅ |
| 1 | Genuine owner | reads its own memory ✅ |
| 2 | Colliding cwd that **does** have its own transcript | **reads the shared memory** ⚠️ |
| 3 | Colliding cwd + prompt-injection of `{"cwd":...}` / `{"type":"relocated",...}` into message content | fail-closed ✅ |
| 4 | Same injection carrying a raw U+2028 to exploit `splitlines()` | fail-closed ✅ |
| 5 | Legit owner buried past the 8-file scan cap | fail-closed (false negative) ⚠️ |

Case 1 is exactly the reported P1-1 vector (`/a/team/app` vs `/a/team-app`) and it is **closed**. `_session_recorded_cwd_matches()` does what it claims.

Cases 3 and 4 deserve emphasis because they are the obvious follow-on attack: they fail for a structural reason, not by luck. Any `"` inside a JSONL record's message content is written as `\"`, so no injected fragment can ever parse as a JSON object, and no injected text can ever match `_RELOCATED_TYPE_RE` (which needs unescaped `"type":"relocated"`). Injection into the transcript cannot forge a cwd.

**Case 2 is the residual, and it is live on this machine right now.** Sweeping all 75 project directories found **4 real, pre-existing collisions** where two genuinely different workspaces share one Claude project directory:

| Shared directory | Colliding real cwds | Has `memory/MEMORY.md` |
|---|---|---|
| `-Volumes-Extreme-SSD-Orca-workspaces-ai-----------` | `…/ai获取软件/获客软件训练`, `…/ai获取软件/设备控制面板` | **yes** |
| `-Volumes-Extreme-SSD-Orca-workspaces-hgfast-----` | `…/hgfast/节点专用`, `…/hgfast/节点排序` | **yes** |
| `-Volumes-Extreme-SSD-Orca-workspaces-hgfast----` | `…/hgfast/客户端`, `…/hgfast/导航页` | no |
| `-Volumes-Extreme-SSD-Orca-workspaces-hgfast---ui` | `…/hgfast/前端ui`, `…/hgfast/后端ui` | no |

Every CJK path segment collapses to one `-` per character, so **any two sibling CJK directory names of equal length collide**. In a workspace tree named in Chinese this is not an exotic edge case — it is the norm, and it already happened four times without anyone constructing it.

For the two dirs holding memory, both cwds pass `_session_recorded_cwd_matches` (measured: 0 false negatives across all 65 pairs), so the bridge will serve each workspace the other's notes.

**Why this is not a P1.** Claude Code *itself* uses that single shared directory as the memory store for both cwds. There is no separate "获客软件训练 memory" for the bridge to keep out of 设备控制面板 — they are one file, and Claude Code running in either workspace already reads all of it. The bridge propagates a pre-existing Claude Code conflation to Codex; it does not cross a boundary that exists upstream. I reach the same conclusion round-1 opus did on F1/F2, now with a mechanism and four live instances behind it rather than an argument.

**What is a defect is the README claiming otherwise** — see N1.

---

## (b) `_session_recorded_cwd_matches()` — no new security problem; three sharp edges

**Design comparison against the real `fWe`/`hJc`:**

| | Real Claude Code | This bridge |
|---|---|---|
| Comparison | `fEo(Zu(recorded)) == fEo(requesting)`, optionally case-folded | raw NFC string equality |
| Transcripts scanned | **all** `.jsonl` in the dir | first **8** by name |
| Window | head+tail `aP` = 65536 B | head+tail 65536 B ✅ identical |
| Relocated marker | same line must carry `"type":"relocated"` **and** `relocatedCwd`, JSON-verified | value line and gate line decoupled |

The comparison difference matters and is in the safe direction: real Claude compares *sanitized* forms, which are collision-blind by construction (`fEo("/a/b") == fEo("/a-b")`), so `hJc` alone would not distinguish the P1-1 pair at all. The bridge's raw comparison is **strictly stronger** than the binary's. Good — but see N4, because the code comments claim parity rather than strictness.

**Boundary probes, all clean:**

- Non-UTF-8 memory content is rejected at `_read_memory_file` before reaching redaction: latin-1 ✅ rejected, UTF-16 ✅ rejected, lone surrogate (`ed a0 80`) ✅ rejected. BOM and embedded NUL pass through and are safely escaped by `json.dumps`.
- `_read_head_tail` correctly guards `S_ISREG` / symlink / uid / `0o022`, does `O_NOFOLLOW` + post-open `(st_dev, st_ino)` recheck, and uses `os.pread` so no seek races. A directory named `x.jsonl` is rejected by `S_ISREG` (it does burn one of the 8 slots — cosmetic).
- Head/tail windows: a partial leading line in the tail simply fails `json.loads` and is skipped, matching `XTt`'s behaviour when `lastIndexOf("\n")` returns −1.
- **64KB window is not bypassable** in the direction that matters. Pushing a genuine cwd record out of the head window causes the transcript to yield `None` → fail-closed. It cannot cause a *wrong* cwd to be returned.
- **Performance is fine.** 2 × `pread` of ≤64KB × ≤8 files, then `splitlines()` + per-candidate `json.loads` on ≤64KB. Measured across all 61 real project dirs (largest transcript in scope: 15.7 MB) with no perceptible cost. This is materially cheaper than the binary's own uncapped scan.

**N5 (P3) — decoupled relocated gate.** `_session_recorded_cwd` takes the *value* from the most recent line with a top-level string `relocatedCwd` (no `type` check), then separately gates on *any* line matching both regexes. Real `XTt` requires one line to satisfy everything. Demonstrated divergence with a crafted 3-line transcript:

```
bridge _session_recorded_cwd -> '/tmp/attackws'
real hJc/XTt would return    -> '/tmp/legitws'
```

Not reachable in practice: it needs a record carrying top-level `relocatedCwd` with `type != "relocated"`, and across every real transcript on this machine **9/9** such records have `type == "relocated"` (0 deviations). Injection cannot manufacture one (JSON escaping, above). And in my probe every cwd — attacker, legit-relocated and original — ended up denied, i.e. the failure direction is closed. Worth tightening to `XTt`'s exact shape (one line, JSON-verified `type`) for fidelity, not for safety.

**N6 (P3) — the 8-transcript cap can deny a legitimate owner.** Case 5 reproduces it: 12 transcripts from one cwd sorting before the owner's, and the owner is refused. Real `fWe` has no cap. Current real-world impact is **zero** (0 false negatives across all 65 live pairs), but the cap makes correctness depend on UUID sort order, which is arbitrary. Consider raising the cap or scanning newest-mtime-first.

**N7 (P3) — `splitlines()` over-splits relative to JS.** Python splits on `\v \f \x1c \x1d \x1e \x85 U+2028 U+2029`; `XTt`/`uEo` split only on `\n`. `JSON.stringify` does not escape U+2028/U+2029, so a record containing one *is* one physical line for the binary and several for the bridge. Verified non-exploitable (case 4) for the same JSON-escaping reason; the only effect is losing a record → fail-closed.

---

## (c) F3 / F4 / F5 documentation fixes

**F4 (workspace coverage) — accurate, and confirmed by direct observation.** This repository is its own counterexample, exactly as the README says:

- cwd `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca` derives `-Volumes-Extreme-SSD-Orca-workspaces-orca---orca` — 34 transcripts, **no `memory/` subdirectory**.
- This session's actual memory lives in `-Volumes-Extreme-SSD-Orca-projects-orca/memory/` (4 files).

So the bridge returns **nothing** for this very repo. The README states this plainly, names the reverse-lookup gap as deliberately unimplemented, and adds the right operational warning (don't read "no context" as "bridge broken"). No over-claim.

**F3 (install-time hooks trust gate) — grounded, appropriately hedged.** The live `~/.codex/hooks.json` (0600, 1734 B) contains exactly `SessionStart` (1 entry) and `UserPromptSubmit` (1 entry) — precisely the two events `install_bridge.py:186` rewrites, and precisely the pre-existing hooks the warning names. The premise is real. I could **not** independently verify the mechanism itself ("hooks that have not been re-trusted do not run at all under non-interactive `codex exec`, silently") without installing and running Codex, which is outside a read-only review. The README labels it "confirmed by independent review, not yet mitigated here" rather than asserting it as verified fact, which is the correct posture.

**F5 (citation) — substantively fixed; new citation unverifiable offline.** The bad precedent (`startup_context.py`'s `payload.get("cwd") or Path.cwd()` fallback, which undercut rather than supported the claim) is gone. The replacement cites Codex's own hooks documentation; no local copy exists on this machine and I have no network, so I cannot confirm the quote. This is harmless: the comment itself says "It is required here regardless", and `parse_hook_input` fails closed on a missing/relative `cwd` — the code does not depend on the citation being right.

---

## New findings

### N1 — P2 — README asserts a security property that is false and reproducible

`README.md`, in the "Known limits" block:

> **"neither ever serves a *different* workspace's content, only "nothing" in these cases"**

This is wrong for the collision limb it is attached to. Case 2 above is a direct counterexample, and **two live directories on this machine are in exactly that state today** (`…-ai-----------` and `…-hgfast-----`, both holding `memory/MEMORY.md` and both recording two distinct cwds).

Reproduce: run the bridge with `cwd=/Volumes/Extreme SSD/Orca/workspaces/hgfast/节点排序`; it serves `MEMORY.md` written during `…/hgfast/节点专用` sessions.

This also **contradicts the code**: `read_memory_documents`'s own comment is accurate — "this bridge — like Claude Code itself — treats them as one project" and "closing the *specific* case where an attacker's cwd never really shared a directory with the target at all". The README generalized that into a guarantee the code never made. Two statements in one candidate, one of them false, on the property a reviewer would rely on.

Not a P1 because no boundary is crossed that Claude Code respects upstream (analysis above). But it must be corrected before install, because the false half is the half an operator reads.

Suggested wording: *distinct cwds that derive the same directory name share one Claude project — Claude Code itself stores their memory in the same file, so the bridge serves that shared file to both. What the transcript check closes is a colliding cwd that never actually ran a Claude session there.*

### N2 — P2 — IPv6 over-redaction corrupts ordinary prose and code, and eats adjacent characters

`_IPV6_CANDIDATE_RE = [0-9a-fA-F:.]*:[0-9a-fA-F:.]*` greedily absorbs neighbouring `a`–`f` letters that belong to ordinary words, and `ipaddress` then accepts fragments like `d::`, `e::b`, `cafe::babe`:

```
'std::vector<int>'   -> 'st[REDACTED_IP]vector<int>'
'Foo::bar()'         -> 'Foo[REDACTED_IP]r()'
'namespace::fn'      -> 'namesp[REDACTED_IP]n'
'hello::world'       -> 'hello[REDACTED_IP]world'
'df::stat'           -> '[REDACTED_IP]stat'
'::before'           -> '[REDACTED_IP]ore'
'a::b', 'cafe::babe' -> '[REDACTED_IP]'
```

9 of 20 benign samples altered. This is **new in round 2** — the round-1 regex left all of them untouched.

Fails safe (over-redaction), so not a security finding. But it is a correctness regression in the hook's actual product: any memory note mentioning C++/Rust/PHP scope resolution, a CSS pseudo-element, or a Ruby/Perl module path gets mangled — and `namespace::fn → namesp[REDACTED_IP]n` silently **deletes real characters on both sides** ("ace", "f"), turning a redaction into a corruption. Since the whole point of the bridge is to hand Codex readable prior notes, this degrades the deliverable.

Fix direction: anchor the candidate on IPv6 shape (require `::` or ≥2 `:` groups with a non-hex-word boundary) before handing it to `ipaddress`, or reject candidates whose match is immediately flanked by `[G-Zg-z]`.

### N3 — P2 — MAC-address redaction coverage lost versus round 1

```
'de:ad:be:ef:00:11'  old='[REDACTED_IP]'  new='de:ad:be:ef:00:11'   <-- R2 stopped redacting
'AC:DE:48:00:11:22'  old='[REDACTED_IP]'  new='AC:DE:48:00:11:22'   <-- R2 stopped redacting
'00:1B:44:11:3A:B7'  old='[REDACTED_IP]'  new='00:1B:44:11:3A:B7'   <-- R2 stopped redacting
```

Round 1's `(?:[0-9a-f]{1,4}:){2,7}` incidentally caught MAC addresses; `ipaddress.ip_address` correctly rejects them, so they now pass through to Codex verbatim. The old behaviour was accidental rather than designed, and the README promises "common secret and **server-address** forms" — a MAC is a hardware network identifier that reasonably falls under that. Net redaction coverage moved backwards for this class while moving forwards for IPv6. Add an explicit MAC pattern.

### N4 — P2 — `claude_project_dirname` docstring misstates the Claude Code precedent

> "Claude Code itself does not treat that name alone as proof of project identity either: it cross-checks against the `cwd`/`relocatedCwd` field recorded inside a project's own session transcripts before trusting a match (binary's `hJc`/`uEo`/`XTt`, used via `fWe`). This bridge does the same…"

Both halves are wrong, from my own extraction of `j3` at offset `269364075`:

```js
async function j3(e,t){ if(t)return PDy(e,t);
  let r=bN(e),n=[];
  try{await l5.readdir(r),n.push(r)}catch{}          // <-- existence check only. No fWe. No hJc.
  let o=WT(e); if(o.length<=Yre)return n;            // <-- short paths stop here
  /* fWe/hJc verification applies ONLY to the extra >200-char sibling dirs */ }
```

For the primary cwd-derived directory — the only one this bridge ever looks at — Claude Code **does** trust the name alone; a bare `readdir` is the whole check. `fWe`/`hJc` are reached only for the additional hash-suffixed siblings in the >200-char branch, and via `gJc` for cross-worktree lookup. And "does the same" understates the bridge: it compares raw NFC strings where `hJc` compares `fEo()`-sanitized (optionally case-folded) ones, i.e. **stricter**, which is why it catches collisions `hJc` structurally cannot.

The mechanism is sound; only the justification is wrong. This is the same failure mode as round-1 F5 — citing a precedent that does not say what it is claimed to say — recurring in the very commit that fixed F5. Recommend restating as: *this bridge is deliberately stricter than Claude Code, which trusts the derived directory name alone for the primary lookup.*

### N9 — P3 — IPv6 zone identifier survives redaction

`fe80::1%eth0` → `[REDACTED_IP]%eth0`; `fe80::1%en0` → `[REDACTED_IP]%en0`. The interface name leaks. Minor, one-line fix.

### N8 — P3 — quadratic candidate regex (bounded today)

`_IPV6_CANDIDATE_RE` is O(n²) on a long run of `[0-9a-f.]`: 21 KB → 0.40 s, **127 KB → 11.4 s**. Not reachable now — `split_blocks` caps a block at 4,000 chars and `validate_policy` caps `max_blocks` at 8, giving a measured worst case of **10.4 ms per block / 83 ms total** (full `build_context` worst case: 4 ms). Flagging because the safety margin is a *downstream* cap, not a property of the regex: raising the 4,000-char block cap would turn this into a hang.

---

## (e) Regression check

- `install_bridge.py`, the policy/storage/integrity path, digest pinning, argument parsing and `main()` are all **unchanged** this round (diffstat: README, hook, hook tests, and one backlog report only). Its 7 tests still pass.
- `min(limits.max_file_bytes, limits.max_total_bytes)` is strictly tighter than the previous `limits.max_file_bytes` — correct, and it closes the round-1 note about `max_total_bytes` being silently ignored.
- Regressions found: **N2** (over-redaction corrupting text) and **N3** (MAC coverage lost). Both in the redaction pass, both introduced by the P1-3 fix, neither a security regression.
- No regression in the storage, policy, transform, or scoping paths.

## Observation (not a finding)

The bridge reads only `memory/MEMORY.md`, which under this project's memory convention is a one-line-per-memory *index* (the live file here is 431 bytes for 3 memories); the memory bodies live in sibling `.md` files that are never read. That is a deliberate bounding choice and is consistent throughout, but it means the delivered context is index hooks rather than the facts themselves — worth confirming that matches intent before judging an acceptance test's richness.

---

## Summary

| Round-1 P1 | Status | Evidence |
|---|---|---|
| P1-1 non-injective dirname | **Closed for the reported attack**; residual is Claude-Code-native | 5 real-FS scenarios; 4 live collisions surveyed |
| P1-2 transform fidelity | **Fully closed** | 0/3,024 mismatches vs verbatim binary JS |
| P1-3 compressed IPv6 | **Fully closed** | 0/22 missed |

| New | Sev | Summary |
|---|---|---|
| N1 | P2 | README claims a collision "never serves a different workspace's content" — false, reproducible, 2 live dirs |
| N2 | P2 | IPv6 over-redaction corrupts `std::vector`, `namespace::fn`, … and eats adjacent characters (round-2 regression) |
| N3 | P2 | MAC addresses no longer redacted (round-2 regression) |
| N4 | P2 | Docstring misstates the Claude Code precedent; the bridge is stricter than claimed, not equivalent |
| N5 | P3 | Relocated gate decoupled from value line, unlike `XTt`; unreachable, fail-closed |
| N6 | P3 | 8-transcript cap can deny a legitimate owner; 0 real false negatives today |
| N7 | P3 | `splitlines()` over-splits vs JS `\n`; non-exploitable, fail-closed |
| N8 | P3 | O(n²) candidate regex, bounded to 83 ms by downstream caps |
| N9 | P3 | IPv6 zone id survives redaction |

**No P0. No P1. GO, conditional on fixing N1** (and, recommended in the same pass, N2 / N3 / N4).

*Read-only review. Nothing modified, committed, merged, installed, or activated. All probes ran in a scratch temp directory or against `~/.claude` read-only; the four live collision directories were inspected, not touched.*
