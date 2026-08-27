#!/usr/bin/env python3
"""Zero-Orca unit tests for orca_terminal_dispatch.py.

Same conventions as test_ego_profile_router.py: hand-rolled assertions, the real
filesystem inside a tempdir, and every `orca` round-trip monkeypatched with a
fake world so the suite never touches a live terminal.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time


SCRIPT = Path(__file__).with_name("orca_terminal_dispatch.py")


def load_module(state_dir: str, env: dict | None = None):
    os.environ["ORCA_TERMINAL_DISPATCH_STATE_DIR"] = state_dir
    for key, value in (env or {}).items():
        os.environ[key] = value
    spec = importlib.util.spec_from_file_location("orca_terminal_dispatch_tested", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeArgs:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeOrca:
    """A tiny stand-in for the `orca` CLI, recording every call it is asked to make."""

    def __init__(self, terminals=None, worktrees=None):
        self.terminals = {t["handle"]: t for t in (terminals or [])}
        self.worktrees = worktrees or []
        self.calls: list = []
        self.create_handle = "term_new"
        self.create_ok = True
        self.close_ok = True

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        head = tuple(args[:2])
        if head == ("terminal", "list"):
            return {"ok": True, "result": {"terminals": list(self.terminals.values())}}
        if head == ("worktree", "ps"):
            return {"ok": True, "result": {"worktrees": self.worktrees}}
        if head == ("terminal", "show"):
            handle = args[args.index("--terminal") + 1]
            live = self.terminals.get(handle)
            if live is None:
                return {"ok": False, "error": {"code": "terminal_handle_stale"}}
            return {"ok": True, "result": {"terminal": live}}
        if head == ("terminal", "create"):
            if not self.create_ok:
                return {"ok": False, "error": {"code": "create_failed"}}
            title = args[args.index("--title") + 1]
            # Orca gives each freshly created terminal its own tab; mirroring that
            # matters because --tab closes a whole tab.
            suffix = self.create_handle
            live = {
                "handle": self.create_handle,
                "ptyId": f"pty-{suffix}", "incarnationId": f"inc-{suffix}",
                "tabId": f"tab-{suffix}", "leafId": f"leaf-{suffix}",
                "worktreeId": "wt-new", "worktreePath": "/tmp/wt-new",
                "title": title, "lastOutputAt": int(time.time() * 1000),
            }
            self.terminals[self.create_handle] = live
            return {"ok": True, "result": {"terminal": live}}
        if head == ("terminal", "close"):
            handle = args[args.index("--terminal") + 1]
            if not self.close_ok:
                return {"ok": False, "error": {"code": "close_failed"}}
            self.terminals.pop(handle, None)
            return {"ok": True, "result": {"closed": True}}
        raise AssertionError(f"unexpected orca call: {args}")

    def closed_handles(self):
        return [
            c[c.index("--terminal") + 1]
            for c in self.calls
            if tuple(c[:2]) == ("terminal", "close")
        ]


def main() -> None:
    failures: list = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    with tempfile.TemporaryDirectory() as temp:
        mod = load_module(temp)

        # ---------------------------------------------------------- config layer
        check("default idle", mod.IDLE_SECONDS, 600)
        check("marker required by default", mod.REQUIRE_MARKER, True)
        check("agent ps required by default", mod.REQUIRE_AGENT_PS, True)
        check("env_int rejects out of range", mod.env_int("MISSING_TEST", 7, 1, 10), 7)
        check("env_flag default", mod.env_flag("MISSING_FLAG_TEST", True), True)

        # ------------------------------------------------- private state on disk
        registry = mod.load_registry()
        check("empty registry", registry["terminals"], {})
        registry["terminals"]["term_a"] = {"handle": "term_a", "created_by_this_wrapper": True}
        mod.private_json_write(mod.REGISTRY_PATH, registry)
        check("registry private", oct(mod.REGISTRY_PATH.stat().st_mode & 0o777), "0o600")
        check("registry roundtrip", mod.load_registry()["terminals"]["term_a"]["handle"], "term_a")

        mod.private_json_write(mod.REGISTRY_PATH, {"version": 99, "terminals": {}})
        try:
            mod.load_registry()
            failures.append("load_registry: expected version rejection")
        except mod.DispatchError as exc:
            check("registry version guarded", "unsupported" in str(exc), True)
        mod.private_json_write(mod.REGISTRY_PATH, {"version": 1, "terminals": []})
        try:
            mod.load_registry()
            failures.append("load_registry: expected shape rejection")
        except mod.DispatchError as exc:
            check("registry shape guarded", "malformed" in str(exc), True)
        mod.private_json_write(mod.REGISTRY_PATH, {"version": 1, "terminals": {}})

        mod.log_event("unit test")
        check("log exists", mod.LOG_PATH.exists(), True)
        check("log private", oct(mod.LOG_PATH.stat().st_mode & 0o777), "0o600")

        big = mod.LAUNCHD_LOG_PATHS[1]
        big.write_text("x" * (mod.LAUNCHD_LOG_MAX_BYTES + 10), encoding="utf-8")
        mod.trim_launchd_logs()
        check("launchd log trimmed", big.stat().st_size, 0)

        # ------------------------------------------------------------- markers
        marker = mod.make_marker("Grok")
        check("marker prefix", marker.startswith("[[orca-dispatch:grok:"), True)
        check("marker suffix", marker.endswith("]]"), True)
        check("marker unique", mod.make_marker("grok") != marker, True)
        check("marker sanitises kind", mod.make_marker("../evil").startswith("[[orca-dispatch:evil:"), True)
        check(
            "marker matched as delimited substring",
            mod.marker_present({"marker": marker}, {"title": f"◑ {marker} doing work"}),
            True,
        )
        check("marker absent after rename", mod.marker_present({"marker": marker}, {"title": "renamed"}), False)
        check("marker absent when title is null", mod.marker_present({"marker": marker}, {"title": None}), False)
        check("marker absent when unrecorded", mod.marker_present({}, {"title": "x"}), False)

        # -------------------------------------------------------- identity check
        base_entry = {
            "handle": "term_x", "created_by_this_wrapper": True, "marker": marker,
            "pty_id": "pty-1", "incarnation_id": "inc-1", "tab_id": "tab-1", "leaf_id": "leaf-1",
            "agent_kind": "grok", "last_activity": 0.0, "defer_until": 0, "idle_seconds": 600,
        }
        base_live = {
            "handle": "term_x", "ptyId": "pty-1", "incarnationId": "inc-1",
            "tabId": "tab-1", "leafId": "leaf-1",
            "title": f"{marker} work", "lastOutputAt": 1000,
        }
        check("identity matches", mod.identity_mismatches(base_entry, base_live), [])
        for local_key, live_key in mod.IDENTITY_FIELDS:
            drifted = dict(base_live)
            drifted[live_key] = "different"
            check(f"identity catches {live_key}", mod.identity_mismatches(base_entry, drifted), [live_key])
        blank = dict(base_entry)
        blank["incarnation_id"] = None
        check(
            "unrecorded identity field never counts as a match",
            "incarnationId" in mod.identity_mismatches(blank, base_live),
            True,
        )

        check("pane key joins tab and leaf", mod.pane_key_of(base_entry), "tab-1:leaf-1")
        check("pane key needs both halves", mod.pane_key_of({"tab_id": "t"}), None)

        # ------------------------------------------------------- activity clocks
        now = 10_000.0
        check(
            "lastOutputAt null means NOT idle",
            mod.newest_activity({"last_activity": 1.0}, {"lastOutputAt": None}, None),
            None,
        )
        check(
            "lastOutputAt is milliseconds",
            mod.newest_activity({"last_activity": 1.0}, {"lastOutputAt": 5000}, None),
            5.0,
        )
        check(
            "newest of registry and live wins",
            mod.newest_activity({"last_activity": 9.0}, {"lastOutputAt": 5000}, None),
            9.0,
        )
        check(
            "agent updatedAt counts as activity",
            mod.newest_activity({"last_activity": 1.0}, {"lastOutputAt": 2000}, {"updatedAt": 8000}),
            8.0,
        )
        check(
            "non-numeric lastOutputAt means NOT idle",
            mod.newest_activity({"last_activity": 1.0}, {"lastOutputAt": "soon"}, None),
            None,
        )
        check("per-entry idle limit honoured", mod.entry_idle_limit({"idle_seconds": 3600}, 600), 3600)
        check("absurd per-entry idle limit ignored", mod.entry_idle_limit({"idle_seconds": 5}, 600), 600)

        # ------------------------------------------------------------ due_entries
        due_registry = {
            "terminals": {
                "young": {"handle": "young", "created_by_this_wrapper": True, "last_activity": now - 599, "defer_until": 0},
                "due": {"handle": "due", "created_by_this_wrapper": True, "last_activity": now - 600, "defer_until": 0},
                "deferred": {"handle": "deferred", "created_by_this_wrapper": True, "last_activity": now - 999, "defer_until": now + 1},
                "foreign": {"handle": "foreign", "created_by_this_wrapper": False, "last_activity": now - 999, "defer_until": 0},
                "unmarked": {"handle": "unmarked", "last_activity": now - 999, "defer_until": 0},
                "long": {"handle": "long", "created_by_this_wrapper": True, "last_activity": now - 700, "defer_until": 0, "idle_seconds": 3600},
            }
        }
        check(
            "due_entries respects clock, defer, wrapper flag and per-entry limit",
            sorted(e["handle"] for e in mod.due_entries(due_registry, now, 600)),
            ["due"],
        )

        # ------------------------------------------ evaluate_candidate safety table
        def verdict(entry=None, live_state="found", live=None, agent=None, ps=True, idle=600, **kw):
            return mod.evaluate_candidate(
                entry if entry is not None else dict(base_entry),
                live_state,
                base_live if live is None else live,
                agent,
                ps,
                now,
                idle,
                **kw,
            )

        old_live = dict(base_live, lastOutputAt=int((now - 5000) * 1000))
        check("idle registered terminal closes", verdict(live=old_live)[0], "close")

        not_ours = dict(base_entry)
        not_ours["created_by_this_wrapper"] = False
        check("entry without wrapper marker is dropped", verdict(entry=not_ours, live=old_live)[0], "drop")

        check("vanished terminal is dropped", verdict(live_state="stale", live=None)[0], "drop")
        check(
            "stale-handle contradicted by list defers",
            verdict(live_state="stale-but-listed", live=old_live)[0],
            "defer",
        )

        recycled = dict(old_live, ptyId="pty-someone-else")
        action, reason = verdict(live=recycled)
        check("recycled handle is dropped not closed", action, "drop")
        check("recycled handle names the field", "ptyId" in reason, True)

        renamed = dict(old_live, title="✱ 商业计划书设计调研")
        check("renamed terminal is never closed", verdict(live=renamed)[0], "defer")
        check(
            "marker requirement can be relaxed explicitly",
            verdict(live=renamed, require_marker=False)[0],
            "close",
        )

        check("missing agent state defers", verdict(live=old_live, ps=False)[0], "defer")
        check(
            "missing agent state may be tolerated explicitly",
            verdict(live=old_live, ps=False, require_agent_ps=False)[0],
            "close",
        )

        for state in ("working", "waiting"):
            check(
                f"busy agent pane ({state}) defers",
                verdict(live=old_live, agent={"state": state, "agentType": "grok"})[0],
                "defer",
            )
        check(
            "done agent pane of our own kind closes",
            verdict(live=old_live, agent={"state": "done", "agentType": "grok"})[0],
            "close",
        )
        check(
            "pane taken over by claude defers",
            verdict(live=old_live, agent={"state": "done", "agentType": "claude"})[0],
            "defer",
        )
        check(
            "unknown agent type does not block our own terminal",
            verdict(live=old_live, agent={"state": "done", "agentType": "unknown"})[0],
            "close",
        )

        check("null lastOutputAt defers", verdict(live=dict(old_live, lastOutputAt=None))[0], "defer")
        check("recent output defers", verdict(live=dict(base_live, lastOutputAt=int(now * 1000)))[0], "defer")
        check(
            "deferred entry stays deferred",
            verdict(entry=dict(base_entry, defer_until=now + 60), live=old_live)[0],
            "defer",
        )

        # ------------------------------------------------------ create + rollback
        fake = FakeOrca()
        original_run_orca = mod.run_orca
        mod.run_orca = fake
        created = mod.command_create(
            FakeArgs(
                worktree="active", command="grok -p hi", purpose="probe",
                agent_kind="grok", title=None, idle_seconds=None,
                timeout=90, focus=False,
            )
        )
        check("create reports created", created["action"], "created")
        check("create returns the handle", created["handle"], "term_new")
        entry = mod.load_registry()["terminals"]["term_new"]
        check("registry records wrapper ownership", entry["created_by_this_wrapper"], True)
        check("registry records the marker", entry["marker"], created["marker"])
        check("marker is in the requested title", created["marker"] in created["title"], True)
        check("registry records pty identity", entry["pty_id"], "pty-term_new")
        check("registry records leaf identity", entry["leaf_id"], "leaf-term_new")
        check("registry records the agent kind", entry["agent_kind"], "grok")
        check("registry records a command summary", entry["command_summary"], "grok -p hi")
        check("registry seeds the idle limit", entry["idle_seconds"], 600)
        check("create passed --title to orca", "--title" in fake.calls[0], True)

        long_command = "x" * (mod.COMMAND_SUMMARY_MAX + 50)
        fake.create_handle = "term_long"
        mod.command_create(
            FakeArgs(worktree="active", command=long_command, purpose="p", agent_kind="grok",
                     title=None, idle_seconds=1200, timeout=90, focus=False)
        )
        long_entry = mod.load_registry()["terminals"]["term_long"]
        check("command summary is truncated", len(long_entry["command_summary"]), mod.COMMAND_SUMMARY_MAX)
        check("truncation is flagged", long_entry["command_truncated"], True)
        check("per-dispatch idle limit stored", long_entry["idle_seconds"], 1200)

        try:
            mod.command_create(
                FakeArgs(worktree="active", command="x", purpose="p", agent_kind="grok",
                         title=None, idle_seconds=5, timeout=90, focus=False)
            )
            failures.append("command_create: expected idle-seconds range rejection")
        except mod.DispatchError as exc:
            check("create validates idle-seconds", "between 60 and 86400" in str(exc), True)

        # A terminal we made but could not register must be closed, never leaked:
        # an unregistered terminal is invisible to the reaper forever.
        fake.create_handle = "term_rollback"
        original_write = mod.private_json_write

        def explode(path, value):
            if path == mod.REGISTRY_PATH:
                raise OSError("disk full")
            return original_write(path, value)

        mod.private_json_write = explode
        try:
            mod.command_create(
                FakeArgs(worktree="active", command="x", purpose="p", agent_kind="grok",
                         title=None, idle_seconds=None, timeout=90, focus=False)
            )
            failures.append("command_create: expected registration failure to propagate")
        except OSError:
            pass
        mod.private_json_write = original_write
        check("unregisterable terminal is closed again", "term_rollback" in fake.closed_handles(), True)
        check("unregisterable terminal left no entry", "term_rollback" in mod.load_registry()["terminals"], False)

        # --tab closes the WHOLE tab, so it is only used when the tab is provably
        # ours alone -- a dispatched pane split beside someone else's session must
        # never take that session down with it.
        solo = FakeOrca(terminals=[{"handle": "term_solo", "tabId": "tab-solo"}])
        shared = FakeOrca(terminals=[
            {"handle": "term_shared", "tabId": "tab-shared"},
            {"handle": "term_neighbour", "tabId": "tab-shared"},
        ])
        mod.run_orca = solo
        check("solo tab is not shared", mod.tab_is_shared("term_solo", "tab-solo"), False)
        check("solo tab closes with --tab", "--tab" in mod.close_args_for("term_solo", "tab-solo"), True)
        mod.run_orca = shared
        check("occupied tab is shared", mod.tab_is_shared("term_shared", "tab-shared"), True)
        check("shared tab closes pane-only", "--tab" in mod.close_args_for("term_shared", "tab-shared"), False)
        check("unknown tab is treated as shared", mod.tab_is_shared("term_x", None), True)
        check("unknown tab closes pane-only", "--tab" in mod.close_args_for("term_x", None), False)
        mod.run_orca = fake

        # ------------------------------------------------------------------ close
        try:
            mod.command_close(FakeArgs(terminal="term_not_ours", keep_tab=False, any_owner=True, timeout=90))
            failures.append("command_close: expected unregistered rejection")
        except mod.DispatchError as exc:
            check("close refuses unregistered handles", "not created by this wrapper" in str(exc), True)

        foreign_registry = mod.load_registry()
        foreign_registry["terminals"]["term_new"]["owner_key"] = "pane:someone-else"
        mod.private_json_write(mod.REGISTRY_PATH, foreign_registry)
        try:
            mod.command_close(FakeArgs(terminal="term_new", keep_tab=False, any_owner=False, timeout=90))
            failures.append("command_close: expected foreign-owner rejection")
        except mod.DispatchError as exc:
            check("close protects another pane's dispatch", "another Orca pane" in str(exc), True)

        closed = mod.command_close(FakeArgs(terminal="term_new", keep_tab=False, any_owner=True, timeout=90))
        check("close reports closed", closed["action"], "closed")
        check("close drops the entry", "term_new" in mod.load_registry()["terminals"], False)
        check("close asked for the whole tab", "--tab" in fake.calls[-1], True)

        # A recycled handle must be forgotten, never closed.
        recycle_registry = mod.load_registry()
        recycle_registry["terminals"]["term_long"]["pty_id"] = "pty-from-a-previous-orca-run"
        mod.private_json_write(mod.REGISTRY_PATH, recycle_registry)
        before = len(fake.closed_handles())
        mismatch = mod.command_close(FakeArgs(terminal="term_long", keep_tab=False, any_owner=True, timeout=90))
        check("close detects a recycled handle", mismatch["action"], "identity-mismatch")
        check("close issued no orca close for it", len(fake.closed_handles()), before)
        check("close dropped the stale entry", "term_long" in mod.load_registry()["terminals"], False)

        # A `show` that lies about staleness must NOT make close forget a live
        # terminal: dropping the entry there would hide it from the reaper forever.
        fake.create_handle = "term_liar"
        mod.command_create(
            FakeArgs(worktree="active", command="x", purpose="p", agent_kind="grok",
                     title=None, idle_seconds=None, timeout=90, focus=False)
        )
        liar_live = dict(fake.terminals["term_liar"])
        real_call = fake.__call__

        def lying_show(args, timeout=None):
            if tuple(args[:2]) == ("terminal", "show") and args[args.index("--terminal") + 1] == "term_liar":
                fake.calls.append(list(args))
                return {"ok": False, "error": {"code": "terminal_handle_stale"}}
            return real_call(args, timeout)

        mod.run_orca = lying_show
        liar = mod.command_close(FakeArgs(terminal="term_liar", keep_tab=False, any_owner=True, timeout=90))
        check("close survives a lying stale-handle answer", liar["action"], "closed")
        check("close actually closed the live terminal", "term_liar" in fake.closed_handles(), True)
        check("close dropped the entry", "term_liar" in mod.load_registry()["terminals"], False)
        mod.run_orca = fake
        fake.terminals.pop("term_liar", None)
        del liar_live

        # ------------------------------------------------------------------ touch
        fake.create_handle = "term_touch"
        mod.command_create(
            FakeArgs(worktree="active", command="x", purpose="touch me", agent_kind="gemini",
                     title=None, idle_seconds=None, timeout=90, focus=False)
        )
        aged = mod.load_registry()
        aged["terminals"]["term_touch"]["last_activity"] = time.time() - 5000
        mod.private_json_write(mod.REGISTRY_PATH, aged)
        mod.command_touch(FakeArgs(terminal="term_touch", any_owner=True))
        refreshed = mod.load_registry()["terminals"]["term_touch"]["last_activity"]
        check("touch refreshes activity", time.time() - refreshed < 5, True)

        status = mod.command_status(FakeArgs())
        shown = next(t for t in status["terminals"] if t["handle"] == "term_touch")
        check("status omits the command", "command_summary" in shown, False)
        check("status omits owner metadata", "owner_key" in shown, False)
        check("status reports the purpose", shown["purpose"], "touch me")

        # ------------------------------------------------------------------- reap
        # THE critical boundary: a world full of idle unregistered terminals plus
        # exactly one registered leak. Only the registered one may be touched.
        real_sessions = [
            {"handle": "term_human_1", "ptyId": "p1", "incarnationId": "i1", "tabId": "t1",
             "leafId": "l1", "title": "✱ 商业计划书设计调研", "lastOutputAt": 1},
            {"handle": "term_human_2", "ptyId": "p2", "incarnationId": "i2", "tabId": "t2",
             "leafId": "l2", "title": "Setup", "lastOutputAt": 1},
            {"handle": "term_human_3", "ptyId": "p3", "incarnationId": "i3", "tabId": "t3",
             "leafId": "l3", "title": None, "lastOutputAt": None},
        ]
        leak = {
            "handle": "term_leak", "ptyId": "pl", "incarnationId": "il", "tabId": "tl",
            "leafId": "ll", "title": "", "lastOutputAt": 1,
        }
        reap_fake = FakeOrca(
            terminals=real_sessions + [leak],
            worktrees=[{"agents": [
                {"paneKey": "t3:l3", "agentType": "claude", "state": "done", "updatedAt": 1},
            ]}],
        )
        mod.run_orca = reap_fake
        leak_marker = mod.make_marker("grok")
        leak["title"] = f"{leak_marker} leaked dispatch"
        reap_registry = {"version": 1, "terminals": {
            "term_leak": {
                "handle": "term_leak", "created_by_this_wrapper": True, "marker": leak_marker,
                "pty_id": "pl", "incarnation_id": "il", "tab_id": "tl", "leaf_id": "ll",
                "agent_kind": "grok", "purpose": "leaked", "owner_key": "cwd:/tmp",
                "created_at": time.time() - 9999, "last_activity": time.time() - 9999,
                "defer_until": 0, "idle_seconds": 600,
            }
        }}
        mod.private_json_write(mod.REGISTRY_PATH, reap_registry)

        dry = mod.command_reap(FakeArgs(dry_run=True, idle_seconds=None, quiet=False))
        check("dry run reports would-close", [r["action"] for r in dry["results"]], ["would-close"])
        check("dry run closed nothing", reap_fake.closed_handles(), [])
        check("dry run kept the entry", "term_leak" in mod.load_registry()["terminals"], True)

        live_reap = mod.command_reap(FakeArgs(dry_run=False, idle_seconds=None, quiet=False))
        check("reap closed the leak", [r["action"] for r in live_reap["results"]], ["closed"])
        check("reap closed ONLY the registered leak", reap_fake.closed_handles(), ["term_leak"])
        check("reap dropped the entry", "term_leak" in mod.load_registry()["terminals"], False)
        check(
            "reap left every unregistered terminal alive",
            sorted(reap_fake.terminals),
            ["term_human_1", "term_human_2", "term_human_3"],
        )
        check(
            "reap never even asked about unregistered handles",
            [c for c in reap_fake.calls if tuple(c[:2]) == ("terminal", "show")
             and c[c.index("--terminal") + 1] != "term_leak"],
            [],
        )

        after = mod.command_reap(FakeArgs(dry_run=False, idle_seconds=None, quiet=False))
        check("empty registry is a noop", after["action"], "noop")
        check("noop makes no orca calls", tuple(reap_fake.calls[-1][:2]), ("terminal", "close"))

        # A failed close must not silently drop the entry.
        reap_fake.terminals["term_leak"] = leak
        mod.private_json_write(mod.REGISTRY_PATH, reap_registry)
        reap_fake.close_ok = False
        failed = mod.command_reap(FakeArgs(dry_run=False, idle_seconds=None, quiet=False))
        check("failed close is reported", [r["action"] for r in failed["results"]], ["close-failed"])
        check("failed close keeps the entry", "term_leak" in mod.load_registry()["terminals"], True)
        check(
            "failed close backs off",
            mod.load_registry()["terminals"]["term_leak"]["defer_until"] > time.time(),
            True,
        )
        reap_fake.close_ok = True

        # Deferral is recorded so a stuck entry is visible rather than silent.
        stuck = mod.load_registry()
        stuck["terminals"]["term_leak"]["defer_until"] = 0
        stuck["terminals"]["term_leak"]["marker"] = "[[orca-dispatch:grok:deadbeef]]"
        mod.private_json_write(mod.REGISTRY_PATH, stuck)
        deferred = mod.command_reap(FakeArgs(dry_run=False, idle_seconds=None, quiet=False))
        check("lost marker defers", [r["action"] for r in deferred["results"]], ["deferred"])
        check(
            "deferral reason is persisted",
            "marker" in (mod.load_registry()["terminals"]["term_leak"]["last_reaper_result"] or ""),
            True,
        )

        # A deferral must back off on the entry's OWN scale. An entry asking for a
        # 60s threshold parked for the global 600s would be invisible to the
        # once-a-minute job for ten minutes.
        scaled = mod.load_registry()
        scaled["terminals"]["term_leak"]["defer_until"] = 0
        scaled["terminals"]["term_leak"]["idle_seconds"] = 60
        scaled["terminals"]["term_leak"]["marker"] = "[[orca-dispatch:grok:notthisone]]"
        mod.private_json_write(mod.REGISTRY_PATH, scaled)
        at = time.time()
        mod.command_reap(FakeArgs(dry_run=False, idle_seconds=None, quiet=False))
        backoff = mod.load_registry()["terminals"]["term_leak"]["defer_until"] - at
        check("defer backs off on the entry's own scale", 55 <= backoff <= 65, True)

        # worktree ps unavailable -> defer, never close.
        ps_down = mod.load_registry()
        ps_down["terminals"]["term_leak"]["defer_until"] = 0
        ps_down["terminals"]["term_leak"]["marker"] = leak_marker
        mod.private_json_write(mod.REGISTRY_PATH, ps_down)
        original_agent_states = mod.agent_states
        mod.agent_states = lambda limit=60: None
        before_closes = len(reap_fake.closed_handles())
        ps_result = mod.command_reap(FakeArgs(dry_run=False, idle_seconds=None, quiet=False))
        check("agent state outage defers", [r["action"] for r in ps_result["results"]], ["deferred"])
        check("agent state outage closes nothing", len(reap_fake.closed_handles()), before_closes)
        check("agent state outage is surfaced", ps_result["agent_state_available"], False)
        mod.agent_states = original_agent_states

        # An orca failure on one handle must defer that entry, not abort the pass
        # and not be mistaken for permission to close.
        show_down = mod.load_registry()
        show_down["terminals"]["term_leak"]["defer_until"] = 0
        mod.private_json_write(mod.REGISTRY_PATH, show_down)
        original_terminal_show = mod.terminal_show

        def show_explodes(handle):
            raise mod.DispatchError("orca terminal show timed out")

        mod.terminal_show = show_explodes
        before_closes = len(reap_fake.closed_handles())
        broken = mod.command_reap(FakeArgs(dry_run=False, idle_seconds=None, quiet=False))
        check("show outage defers instead of raising", [r["action"] for r in broken["results"]], ["deferred"])
        check("show outage closes nothing", len(reap_fake.closed_handles()), before_closes)
        check("show outage keeps the entry", "term_leak" in mod.load_registry()["terminals"], True)
        check(
            "show outage is recorded",
            "show-failed" in (mod.load_registry()["terminals"]["term_leak"]["last_reaper_result"] or ""),
            True,
        )
        mod.terminal_show = original_terminal_show

        # ----------------------------------------------- stale-handle cross-check
        cross = FakeOrca(terminals=[])
        cross.terminals["term_ghost"] = {"handle": "term_ghost"}

        def show_lies(args, timeout=None):
            if tuple(args[:2]) == ("terminal", "show"):
                cross.calls.append(list(args))
                return {"ok": False, "error": {"code": "terminal_handle_stale"}}
            return cross(args, timeout)

        mod.run_orca = show_lies
        state, live = mod.terminal_show("term_ghost")
        check("stale show is cross-checked against list", state, "stale-but-listed")
        check("cross-check returns the listed record", live["handle"], "term_ghost")
        state, live = mod.terminal_show("term_really_gone")
        check("genuinely absent handle stays stale", state, "stale")
        check("genuinely absent handle has no record", live, None)

        mod.run_orca = original_run_orca

        # -------------------------------------------------------- reap lock guard
        with mod.registry_lock() as held:
            check("lock acquired", held is not None, True)
            busy = mod.command_reap(FakeArgs(dry_run=False, idle_seconds=None, quiet=False))
            check("reap steps aside while the lock is held", busy["action"], "deferred")

        # ---------------------------------------------------------- main plumbing
        check("main returns 1 on error", mod.main(["close", "--terminal", "term_nope"]), 1)

    # An override of the Ego-style idle env var must be honoured on import.
    with tempfile.TemporaryDirectory() as temp2:
        tuned = load_module(temp2, {"ORCA_TERMINAL_DISPATCH_IDLE_SECONDS": "1800"})
        check("idle env var honoured", tuned.IDLE_SECONDS, 1800)
        del os.environ["ORCA_TERMINAL_DISPATCH_IDLE_SECONDS"]

    if failures:
        print(f"FAIL: {len(failures)}")
        for failure in failures:
            print(f"- {failure}")
        raise SystemExit(1)
    print("PASS: orca terminal dispatch registry + reaper unit tests")


if __name__ == "__main__":
    main()
