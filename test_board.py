import json
import os
import tempfile

import serve


def test_tab_claims_follow_continuation_suffix_only():
    assert serve.claims("W·E2E", ["W·E2E"])
    assert serve.claims("W·E2E-2", ["W·E2E"])
    assert not serve.claims("W·Dev-03-06", ["W·Dev"])


def test_latest_dispatch_wins_and_old_report_is_stale():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as root:
        serve.REGISTRY = root
        os.makedirs(os.path.join(root, "s"))
        report = os.path.join(root, "REPORT.md")
        open(report, "w", encoding="utf-8").write("STATUS: done\n")
        old = os.path.getmtime(report)
        with open(serve.dispatch_path("s", "b"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(worker="w1", brief="a.md", report="OLD.md", at=1)) + "\n")
            fh.write(json.dumps(dict(worker="w1", brief="b.md", report="REPORT.md", at=old + 60)) + "\n")
        task = serve.load_dispatch("s", "b")["w1"]
        assert task["brief"] == "b.md"
        view = serve.worker_view(dict(name="w1", agent_status="idle"), "W·x", {}, root, "s", task)
        assert view["report"]["stale"]
        assert any("还没写" in n["text"] for n in serve.needs(dict(workers=[view], missing=[], pending=[], issues=[])))


def test_workers_group_by_tab_then_story_then_session():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as ws:
        for d in ("PROJ-1/docs", "TASK-2/src"):
            os.makedirs(os.path.join(ws, d))
        tabs = {"t1": "1", "t2": "W·E2E", "t3": "W·UI-Fix", "t4": "W·final", "t5": "W·final-2", "t6": "W·sot"}
        agents = [dict(tab_id="t1", terminal_title_stripped="重构"),
                  dict(name="qa", tab_id="t2", cwd="/elsewhere"),
                  dict(name="fe", tab_id="t3", cwd=os.path.join(ws, "PROJ-1")),
                  dict(name="z", tab_id="t4", cwd="/home"), dict(name="y", tab_id="t5", cwd="/home"),
                  dict(name="s1", tab_id="t6", foreground_cwd=os.path.join(ws, "TASK-2", "src"))]
        boards = [dict(id="PROJ-1", root=os.path.join(ws, "PROJ-1", "docs"), tabs=["W·E2E"])]
        owned, by_story, loose = serve.assign_workers(boards, agents, tabs, [ws])
        assert [a["name"] for a in owned["PROJ-1"]] == ["qa", "fe"]
        assert {os.path.basename(k): [a["name"] for a in v] for k, v in by_story.items()} == {"TASK-2": ["s1"]}
        assert [a["name"] for a in loose] == ["z", "y"]
        board = serve.session_board("s", loose, agents, tabs)
        assert board["name"] == "重构" and board["tabs"] == ["W·final"]
        assert serve.story_board(os.path.join(ws, "TASK-2"), by_story[os.path.join(os.path.realpath(ws), "TASK-2")], tabs)["root"].endswith("TASK-2")


def test_progress_lines_cover_merged_and_skipped_tasks():
    assert serve.task_ids("F2-9/10/11") == ["F2-9", "F2-10", "F2-11"]
    assert serve.task_ids("7") == ["7"]
    with tempfile.TemporaryDirectory(dir=serve.HERE) as root:
        open(os.path.join(root, "plan.md"), "w", encoding="utf-8").write("### Task F2-9: a\n### Task F2-10: b\n### Task 7: c\n### Task 8: d\n")
        open(os.path.join(root, "STATUS.md"), "w", encoding="utf-8").write("STATUS: in-progress\nTask F2-9/10 ✔ abc123 ok\nTask 7 — no work\n")
        tasks, impl, files = serve.plan_part(root, dict(plan="plan.md", progress="STATUS.md"), "")
        assert files["plan"] == ["plan.md"] and files["progress"] == ["STATUS.md"]
        assert serve.root_rel(root, "../outside.md") == ""
        assert [t["done"] for t in tasks] == [True, True, True, False]
        assert tasks[0]["sha"] == "abc123" and impl["state"] == "in-progress"


def test_ac_table_groups_statuses_and_sections():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as root:
        open(os.path.join(root, "BOARD.md"), "w", encoding="utf-8").write(
            "## Other\n| **G9** | | | | | | | |\n| 10001 | AC1 | c | h | q | m | | PASS |\n"
            "## Rows\n| **G1** | | | | | | | |\n| 10001 | AC1 | c | h | q | m | | PASS (with note) |\n"
            "| **Backfill** | | | | | | | |\n| 10001 | AC2 | c | h | q | m | | backfill running |\n| 10001 | AC3 | c | h | q | m | | — |\n"
            "## Not built\n| 10002 | AC1 | c | h | — | — | — | N |\n")
        board = dict(root=root, sources=dict(ac_table=dict(file="BOARD.md", sections={"## Rows": None, "## Not built": "NB"}, empty="EMPTY", other="OTHER")),
                     statuses=[dict(id="PASS", prefix=["PASS"]), dict(id="BF", match=["backfill"])])
        rows = serve.ac_rows(board)
        assert [(r["ac"], r["group"], r["status"]) for r in rows] == [("AC1", "G1", "PASS"), ("AC2", "Backfill", "BF"), ("AC3", "Backfill", "EMPTY"), ("AC1", "—", "NB")]


def test_archive_and_restore_round_trip():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as home:
        serve.REGISTRY, serve.EVENTS_DIR, serve.ARCHIVE_DIR = home, os.path.join(home, ".state"), os.path.join(home, ".archive")
        os.makedirs(os.path.join(home, "s"))
        registration = os.path.join(home, "s", "b.json")
        json.dump(dict(name="B", root=home, tabs=["W·B"]), open(registration, "w", encoding="utf-8"))
        os.makedirs(os.path.join(home, ".state", "s"))
        open(serve.events_path("s", "b"), "w", encoding="utf-8").write(json.dumps(dict(kind="worker", text="x", at="1")) + "\n")
        view = dict(id="b", session="s", name="B", statuses=[dict(id="P", pass_=True)], rows=[], plans=[], workers=[], issues=[])
        serve.state["data"] = dict(sessions=[dict(name="s", boards=[view])])
        archive = serve.archive_board("s", "b")
        assert archive and not os.path.exists(registration) and not os.path.exists(serve.events_path("s", "b"))
        assert [a["archive"] for a in serve.list_archives()] == [archive]
        snap = json.load(open(os.path.join(serve.archive_dir(archive), "snapshot.json")))
        assert snap["events"][0]["text"] == "x" and snap["archived_at"] > 0
        assert serve.board_root("s", "b") == os.path.realpath(home)
        assert serve.archive_dir("../../etc") is None
        assert serve.restore_board(archive) is None
        assert os.path.exists(registration) and os.path.exists(serve.events_path("s", "b")) and serve.list_archives() == []
        assert not os.path.exists(os.path.join(serve.ARCHIVE_DIR, "s"))
        assert serve.archive_board("s", "missing") is None


def test_demos_list_videos_and_tag_by_filename():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as root:
        os.makedirs(os.path.join(root, "demos"))
        for name in ("10001-void-all.mp4", "F1_dispute.webm", "notes.md", "misc.mov"):
            open(os.path.join(root, "demos", name), "wb").write(b"x")
        board = dict(root=root, cards={"10001": "Void"}, plans=[dict(id="F1")], sources=dict(demos=["demos/*"]))
        open(os.path.join(root, "demos", "10001-void-all.chapters.json"), "w", encoding="utf-8").write(json.dumps([dict(t=1.5, label="AC1", result="PASS")]))
        assert next(d for d in serve.demos(board) if d["title"] == "10001-void-all")["chapters"] == [dict(t=1.5, label="AC1", result="PASS")]
        open(os.path.join(root, "demos", "10001-void-all.vtt"), "w", encoding="utf-8").write("WEBVTT\n")
        assert next(d for d in serve.demos(board) if d["title"] == "10001-void-all")["subtitles"] == "demos/10001-void-all.vtt"
        got = {d["title"]: d["tags"] for d in serve.demos(board)}
        assert got == {"10001-void-all": ["10001"], "F1_dispute": ["F1"], "misc": []}
        assert {d["title"] for d in serve.demos(dict(root=root, sources={}))} == {"10001-void-all", "F1_dispute", "misc"}
        empty = os.path.join(root, "empty")
        os.makedirs(empty)
        assert serve.demos(dict(root=empty, sources={})) is None


def test_closeout_reads_status_checklist_and_tables():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as root:
        open(os.path.join(root, "CLOSEOUT.md"), "w", encoding="utf-8").write(
            "STATUS: done\n\n# x\n\n- [x] merged\n- [ ] archive\n\n## 删除\n| 路径 | 类别 | 理由 |\n| :-- | :-- | :-- |\n| a/ | scratch | tmp |\n\n## 保留\n| 路径 | 理由 |\n| :-- | :-- |\n")
        c = serve.closeout(dict(root=root, sources=dict(closeout="CLOSEOUT.md")))
        assert c["state"] == "done" and [i["done"] for i in c["items"]] == [True, False]
        assert c["tables"] == {"删除": [["a/", "scratch", "tmp"]]}
        assert serve.closeout(dict(root=root, sources=dict(closeout="NOPE.md")))["exists"] is False
        assert serve.closeout(dict(root=root, sources={}))["state"] == "done"
        empty = os.path.join(root, "empty")
        os.makedirs(empty)
        assert serve.closeout(dict(root=empty, sources={})) is None


def test_conventional_ac_file_needs_no_config():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as root:
        os.makedirs(os.path.join(root, "docs"))
        open(os.path.join(root, "docs", "AC.md"), "w", encoding="utf-8").write(
            "# TASK-10002 AC\n\n| 状态 | AC | 描述 |\n| :-- | :-- | :-- |\n| 通过 | AC1 | a |\n| 阻塞（等刷数） | AC2 | b |\n| | AC3 | c |\n| 部分通过 | AC4 | d |\n")
        rows = serve.ac_rows(dict(id="TASK-10002", name="TASK-10002", root=root))
        assert [(r["card"], r["ac"], r["status"]) for r in rows] == [("10002", "AC1", "PASS"), ("10002", "AC2", "阻塞"), ("10002", "AC3", "未执行"), ("10002", "AC4", "部分通过")]
        assert serve.ac_rows(dict(id="x", name="x", root=os.path.join(root, "docs", "nope"))) == []


def test_only_workers_gone_mid_task_need_attention():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as root:
        for folder in ("qa", "eng"):
            os.makedirs(os.path.join(root, folder))
        open(os.path.join(root, "qa", "REPORT.md"), "w", encoding="utf-8").write("STATUS: in-progress\n")
        open(os.path.join(root, "eng", "LOG.md"), "w", encoding="utf-8").write("STATUS: done\n")
        declared = {"planner": {"role": "Planner"}, "qa": {"role": "QA"}, "eng": {"role": "Engineer"}}
        tasks = {"qa": dict(report="qa/REPORT.md"), "eng": dict(report="eng/LOG.md")}
        assert [m["name"] for m in serve.interrupted(root, declared, tasks, present=set())] == ["qa"]
        assert serve.interrupted(root, declared, tasks, present={"qa"}) == []


def test_report_follows_newer_status_file_in_same_folder():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as root:
        os.makedirs(os.path.join(root, "G2"))
        old, new, notes = (os.path.join(root, "G2", n) for n in ("REPORT.md", "REPORT-NEXT.md", "notes.md"))
        open(old, "w", encoding="utf-8").write("STATUS: in-progress\n")
        os.utime(old, (1000, 1000))
        open(new, "w", encoding="utf-8").write("STATUS: done\n")
        os.utime(new, (2000, 2000))
        open(notes, "w", encoding="utf-8").write("no status line\n")
        got = serve.latest_report(root, serve.report_status(root, "G2/REPORT.md"))
        assert got["file"] == "G2/REPORT-NEXT.md" and got["state"] == "done" and got["followed"]


def test_recent_story_without_agents_is_found():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as ws:
        for story in ("TASK-3/docs", "OLD-4/docs", "EMPTY-5"):
            os.makedirs(os.path.join(ws, story))
        open(os.path.join(ws, "TASK-3", "docs", "AC.md"), "w", encoding="utf-8").write("| AC | 最终状态 |\n")
        old = os.path.join(ws, "OLD-4", "docs", "AC.md")
        open(old, "w", encoding="utf-8").write("| AC | 最终状态 |\n")
        os.utime(old, (1000, 1000))
        base = os.path.realpath(ws)
        assert [os.path.basename(s) for s in serve.recent_stories([ws], set())] == ["TASK-3"]
        assert serve.recent_stories([ws], {os.path.join(base, "TASK-3")}) == []


def test_brief_report_pairs_become_dev_progress():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as root:
        docs = os.path.join(root, "docs")
        os.makedirs(docs)
        for name, body in {"brief-fe.md": "# 任务书：前端带出时区\n", "report-fe.md": "STATUS: done\n",
                           "brief-deploy.md": "# 部署\n", "report-deploy.md": "STATUS: in-progress\n",
                           "brief-deploy-addendum.md": "# 部署补充\n", "brief-api.md": "# 接口\n"}.items():
            open(os.path.join(docs, name), "w", encoding="utf-8").write(body)
        got = {p["id"]: (p["scope"], p["done"], (p["impl"] or {}).get("state")) for p in serve.plans(dict(root=root))}
        assert got == {"api": ("接口", 0, None), "deploy": ("部署", 0, "in-progress"), "fe": ("前端带出时区", 1, "done")}


if __name__ == "__main__":
    test_brief_report_pairs_become_dev_progress()
    test_recent_story_without_agents_is_found()
    test_report_follows_newer_status_file_in_same_folder()
    test_only_workers_gone_mid_task_need_attention()
    test_conventional_ac_file_needs_no_config()
    test_closeout_reads_status_checklist_and_tables()
    test_demos_list_videos_and_tag_by_filename()
    test_archive_and_restore_round_trip()
    test_ac_table_groups_statuses_and_sections()
    test_progress_lines_cover_merged_and_skipped_tasks()
    test_workers_group_by_tab_then_story_then_session()
    test_tab_claims_follow_continuation_suffix_only()
    test_latest_dispatch_wins_and_old_report_is_stale()
    print("ok")
