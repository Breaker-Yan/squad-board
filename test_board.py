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


def test_unregistered_tabs_of_one_lead_form_one_board():
    agents = [dict(tab_id="t1", terminal_title_stripped="重构"), dict(name="a", tab_id="t7"), dict(name="b", tab_id="t8"), dict(name="c", tab_id="t9")]
    tabs = {"t1": "1", "t7": "W·final", "t8": "W·root-tests", "t9": "W·final-2"}
    boards = serve.auto_boards("s", agents, tabs, [])
    assert len(boards) == 1 and boards[0]["name"] == "重构"
    assert boards[0]["tabs"] == ["W·final", "W·root-tests"]


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
        for name in ("29217-void-all.mp4", "F1_dispute.webm", "notes.md", "misc.mov"):
            open(os.path.join(root, "demos", name), "wb").write(b"x")
        board = dict(root=root, cards={"29217": "Void"}, plans=[dict(id="F1")], sources=dict(demos=["demos/*"]))
        got = {d["title"]: d["tags"] for d in serve.demos(board)}
        assert got == {"29217-void-all": ["29217"], "F1_dispute": ["F1"], "misc": []}
        assert serve.demos(dict(root=root, sources={})) is None


if __name__ == "__main__":
    test_demos_list_videos_and_tag_by_filename()
    test_archive_and_restore_round_trip()
    test_ac_table_groups_statuses_and_sections()
    test_progress_lines_cover_merged_and_skipped_tasks()
    test_unregistered_tabs_of_one_lead_form_one_board()
    test_tab_claims_follow_continuation_suffix_only()
    test_latest_dispatch_wins_and_old_report_is_stale()
    print("ok")
