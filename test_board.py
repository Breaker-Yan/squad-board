import json
import os
import tempfile

import decisions
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
        assert serve.needs(dict(workers=[view], missing=[dict(name="gone", role="gone", report="in-progress")], pending=[], issues=[])) == []


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


def test_ac_content_and_evidence_columns():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as root:
        os.makedirs(os.path.join(root, "docs", "shots"))
        open(os.path.join(root, "docs", "shots", "AC1-a.png"), "wb").write(b"x")
        open(os.path.join(root, "docs", "AC.md"), "w", encoding="utf-8").write(
            "| AC | 内容 | 最终状态 | 证据 |\n| :-- | :-- | :-- | :-- |\n| AC1 | 新建带出时区 | PASS | shots/AC1-*.png |\n")
        row = serve.ac_rows(dict(id="TASK-10", name="TASK-10", root=root))[0]
        assert row["card"] == "TASK-10"
        assert serve.ac_rows(dict(id="PROJ-399-x", name="PROJ-399-x", root=root))[0]["card"] == "399"
        assert row["desc"] == "新建带出时区" and row["qam"] == "" and "evidence" not in row
        assert row["shots"] == ["docs/shots/AC1-a.png"]


def test_event_log_is_trimmed():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as root:
        path = os.path.join(root, "e.jsonl")
        open(path, "w", encoding="utf-8").write("".join(json.dumps(dict(i=i, pad="x" * 300)) + "\n" for i in range(3000)))
        serve.trim_log(path, keep=100)
        lines = open(path, encoding="utf-8").readlines()
        assert len(lines) == 100 and json.loads(lines[-1])["i"] == 2999


def test_stream_view_slims_other_boards():
    full = dict(id="b", session="s", rows=[dict(card="1", ac="AC1", status="PASS", qam="long" * 50)],
                issues=[dict(id="I-1", status="open", round=1, repro="r" * 500)], plans=[dict(id="P", tasks=2, done=1, tracked=True, task_list=[1, 2])])
    data = dict(sessions=[dict(name="s", boards=[full, dict(full, id="c")])])
    view = serve.view_for(data, "s/c")
    b, c = view["sessions"][0]["boards"]
    assert b["slim"] and b["rows"] == [dict(card="1", status="PASS")] and "repro" not in b["issues"][0] and "task_list" not in b["plans"][0]
    assert "slim" not in c and "repro" not in c["issues"][0] and c["issues"][0]["status"] == "open"
    serve.state["data"] = data
    assert serve.find_issue("s/c", "I-1")["repro"] == "r" * 500 and serve.find_issue("s/c", "I-9") is None


def test_archived_story_stays_hidden_until_new_activity():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as home:
        serve.REGISTRY, serve.ARCHIVE_DIR = home, os.path.join(home, ".archive")
        story = os.path.join(home, "TASK-11")
        os.makedirs(os.path.join(story, "docs"))
        ac = os.path.join(story, "docs", "AC.md")
        open(ac, "w", encoding="utf-8").write("| AC | 最终状态 |\n")
        os.utime(ac, (1000, 1000))
        os.makedirs(os.path.join(serve.ARCHIVE_DIR, "s", "story-TASK-11-1"))
        json.dump(dict(archive="s/story-TASK-11-1", session="s", id="story-TASK-11", root=story, story=True, archived_at=2000),
                  open(os.path.join(serve.ARCHIVE_DIR, "s", "story-TASK-11-1", "summary.json"), "w"))
        assert serve.archived_story(story)
        os.utime(ac, (3000, 3000))
        assert not serve.archived_story(story)
        assert serve.restore_board("s/story-TASK-11-1") is None and serve.list_archives() == []


def synthetic_screen(body, prompt="", recap="", done="Brewed for 39s \u00b7 done 4:36 PM"):
    esc = "\x1b"
    rule = esc + "[38;2;136;136;136m" + "\u2500" * 40 + esc + "[0m"
    lines = ["", "  " + esc + "[0m" + body.replace("\n", "\n  "), ""]
    if done:
        lines += [esc + "[38;2;153;153;153m\u273b" + esc + "[0m " + esc + "[38;2;153;153;153m" + done + esc + "[0m", ""]
    if recap:
        lines += [esc + "[38;2;153;153;153m\u203b " + esc + "[1mrecap: " + esc + "[3m" + recap + esc + "[0m", ""]
    lines += [rule, "\u276f " + prompt, rule, "  " + esc + "[38;2;255;107;128m\u23f5\u23f5 bypass permissions on" + esc + "[0m"]
    return "\n".join(lines)


DIM = "\x1b[0m\x1b[2m"


def test_input_state_tells_empty_suggestion_and_typed_apart():
    assert decisions.input_state(synthetic_screen("hi")) == "empty"
    assert decisions.input_state(synthetic_screen("hi", DIM + "go on\x1b[0m")) == "suggestion"
    assert decisions.input_state(synthetic_screen("hi", "half typed")) == "typed"
    assert decisions.input_state("no box here\n") == "unknown"


def test_dim_suggestion_is_returned_and_typed_text_is_not():
    body = "\u23fa Merge the PR first?"
    shown = decisions.parse_screen(synthetic_screen(body, DIM + "yes, merge\x1b[0m"), "idle")
    assert shown["suggestion"] == "yes, merge" and shown["kind"] == "question"
    assert shown["done_at"] == "4:36 PM"
    assert decisions.parse_screen(synthetic_screen(body, "my own words"), "idle")["suggestion"] == ""
    assert decisions.parse_screen(synthetic_screen(body), "idle")["suggestion"] == ""


def test_report_without_question_is_a_stop():
    report = decisions.parse_screen(synthetic_screen("\u23fa All checks passed.\nRegression is complete."), "idle")
    assert report["kind"] == "report" and report["question"].endswith("Regression is complete.")
    short = decisions.parse_screen(synthetic_screen("\u23fa Background job finished, both checks were written back to the table.\n\nStopping here."), "idle")
    assert short["question"].startswith("Background job finished") and short["question"].endswith("Stopping here.")
    after_tool = decisions.parse_screen(synthetic_screen("\u23fa Write(docs/AC.md)\n  \u23bf  Added 2 lines\n\n\u23fa Both checks pass.\n\nStopping here."), "idle")
    assert after_tool["question"] == "Both checks pass.\nStopping here."
    after_reply = decisions.parse_screen(synthetic_screen("\u23fa Stopping here.\n\n\u276f Thanks, we are done.\n\n\u23fa OK."), "idle")
    assert after_reply["question"] == "OK."
    hinted = synthetic_screen("\u23fa Regression is complete.").replace("\u273b Brewed", "\u273b Brewed").replace("\x1b[38;2;136;136;136m\u2500", "     \u25d0 medium \u00b7 /effort\n\x1b[38;2;136;136;136m\u2500", 1)
    assert decisions.parse_screen(hinted, "idle")["question"] == "Regression is complete."
    assert decisions.parse_screen(synthetic_screen("\u23fa Ready.", done=""), "idle") is None
    assert decisions.parse_screen(synthetic_screen("\u23fa Merge the PR first?"), "working") is None


def test_recap_asking_for_a_decision_counts_without_question_mark():
    body = "\u23fa Tests are green.\nPR is ready."
    recap = "PR is ready. \u4e0b\u4e00\u6b65\u9700\u8981\u4f60\u786e\u8ba4\u662f\u5426\u5408\u5e76\u3002"
    found = decisions.parse_screen(synthetic_screen(body, recap=recap), "idle")
    assert found["question"] == recap and found["recap"] == recap
    hinted = decisions.parse_screen(synthetic_screen(body, recap=recap + " (disable recaps in /config)"), "idle")
    assert hinted["recap"] == recap
    assert decisions.parse_screen(synthetic_screen(body, recap="PR is ready, nothing pending."), "idle")["kind"] == "report"


def test_dash_options_follow_the_question():
    body = "\u23fa Which way should the cache go?\n- Keep it in memory: simple,\n  lost on restart\n- Use Redis: shared"
    found = decisions.parse_screen(synthetic_screen(body), "idle")
    assert found["question"] == "Which way should the cache go?"
    assert found["options"] == ["Keep it in memory: simple, lost on restart", "Use Redis: shared"]


def test_numbered_questions_stay_in_the_question_text():
    body = "\u23fa Things only you can decide\n1. Is the old endpoint still needed?\n2. Who owns the follow-up card?"
    found = decisions.parse_screen(synthetic_screen(body), "done")
    assert found["options"] == []
    assert found["question"] == "Things only you can decide\n1. Is the old endpoint still needed?\n2. Who owns the follow-up card?"


def test_blocked_choice_box_and_permission_prompt():
    esc = "\x1b"
    choice = "\n".join(["  Which database?", "", esc + "[36m\u276f 1. Postgres" + esc + "[0m", "  2. MySQL", "", "  Enter to select"])
    found = decisions.parse_screen(choice, "blocked")
    assert (found["kind"], found["question"], found["options"]) == ("choice", "Which database?", ["Postgres", "MySQL"])
    approval = "\n".join(["  Run command?", "  rm -rf build", "", "  Do you want to proceed?", "  \u276f 1. Yes", "    2. No"])
    found = decisions.parse_screen(approval, "blocked")
    assert (found["kind"], found["question"], found["options"]) == ("approval", "Do you want to proceed?", ["Yes", "No"])


def test_short_question_takes_options_from_the_list_above():
    steps = "\u23fa Next steps\n\n  1. Record the demo\n  2. Comment on the card\n  3. Move the card on"
    body = steps + "\n\n  Start these three now?"
    found = decisions.parse_screen(synthetic_screen(body), "idle")
    assert found["question"] == "Start these three now?"
    assert found["options"] == ["Record the demo", "Comment on the card", "Move the card on"]
    own = "\u23fa Steps\n\n  1. Record the demo\n  2. Comment\n\n  Which first?\n  - Demo\n  - Comment"
    assert decisions.parse_screen(synthetic_screen(own), "idle")["options"] == ["Demo", "Comment"]
    long_question = "\u23fa Steps\n\n  1. Record the demo\n\n  " + "Should we start these now, given everything above? " * 2
    assert decisions.parse_screen(synthetic_screen(long_question), "idle")["options"] == []

def test_decision_belongs_to_subscribable_board():
    ws = ["/w"]
    boards = [dict(id="alpha", root="/w/alpha", tabs=["W·alpha"]), dict(id="beta", name="Beta", root="", tabs=["W·work"])]
    pick = lambda **d: serve.board_of_decision(dict(dict(story="", tab=""), **d), boards, {"/w/gamma": []}, ["alpha", "beta", "story-gamma"], ws)
    assert pick(story="alpha") == "alpha"
    assert pick(story="gamma") == "story-gamma"
    assert pick(tab="work") == "beta"
    assert pick(tab="Beta") == "beta"
    assert pick(tab="1") == ""
    assert serve.board_of_decision(dict(story="", tab="1"), [], {}, ["auto"], ws) == "auto"
    other_lead = dict(story="", tab="DONE-1")
    assert serve.board_of_decision(other_lead, [dict(id="NEW-2", root="/w/NEW-2", tabs=["W·NEW-2"])], {}, ["NEW-2"], ws, lead_count=2) == ""

def test_permission_prompt_question_skips_separators_and_tips():
    screen = "\n".join([
        "\u2500" * 40,
        " Bash command",
        " Tip: auto mode handles these prompts for you",
        " Create test file",
        "\u254c" * 40,
        " touch notes.txt",
        "\u254c" * 40,
        " Do you want to proceed?",
        " \u276f 1. Yes",
        "   2. Yes, and always allow access to this folder",
        "   3. No",
        " Esc to cancel",
    ])
    parsed = decisions.parse_screen(screen, "blocked")
    assert parsed["kind"] == "approval"
    assert "\u254c" not in parsed["question"] and "Tip" not in parsed["question"]
    assert "touch notes.txt" in parsed["question"] and parsed["question"].startswith("Bash command")
    assert parsed["options"][0] == "Yes" and parsed["options"][-1] == "No"

def test_past_decision_in_recap_is_not_a_question():
    report = "\n".join([
        "\u23fa Lint is still running; I will tell you when the deploy is done.",
        "\u273b Crunched for 37s \u00b7 done 1:19 PM",
        "\u203b recap: \u4f60\u51b3\u5b9a\u5728\u672c\u5361\u4fee\u7684\u95ee\u9898\u6b63\u5728\u4fee\uff0c\u90e8\u7f72\u540e\u6211\u4f1a\u6d3e QA \u56de\u5f52\uff0c\u518d\u7ed9\u4f60\u6700\u7ec8\u7ed3\u8bba\u3002",
        "\u2500" * 40, "\u276f ", "\u2500" * 40,
    ])
    assert decisions.parse_screen(report, "done")["kind"] == "report"
    assert decisions.parse_screen(report.replace("\u2500" * 40 + "\n\u276f \n" + "\u2500" * 40, "\u2500" * 40 + "\n\u276f \n" + "\u2500" * 40 + "\n  bypass permissions on \u00b7 1 shell \u00b7 \u2190 2 agents"), "done")["shells"] == 1
    asking = report.replace("\u4f60\u51b3\u5b9a\u5728", "\u4e0b\u4e00\u6b65\u9700\u8981\u4f60\u51b3\u5b9a\u5728")
    assert decisions.parse_screen(asking, "done")["kind"] == "question"

def test_notices_fire_once_per_new_wait_and_skip_background_or_unsubscribed():
    ask = dict(session="s", pane="p1", board="b", level="stopped", done_at="1:00 PM", question="Merge?")
    busy = dict(ask, pane="p2", background=True)
    other = dict(ask, pane="p3", board="x")
    seen, fresh = serve.notices([ask], None, None)
    assert fresh == []
    seen, fresh = serve.notices([ask, busy], seen, None)
    assert fresh == []
    seen, fresh = serve.notices([dict(ask, level="decide")], seen, None)
    assert [d["pane"] for d in fresh] == ["p1"]
    seen, fresh = serve.notices([], seen, None)
    seen, fresh = serve.notices([ask, other], seen, {"s/b"})
    assert [d["pane"] for d in fresh] == ["p1"]
    assert serve.notices([other], {}, {"s/*"})[1] == [other]

def test_choice_options_keep_their_descriptions():
    screen = "\n".join([
        "\u2500" * 40,
        " \u2610 Language",
        "Which language for the greeting?",
        "\u276f 1. Chinese",
        "     Print a Chinese greeting; AC2 checks Chinese",
        "  2. English",
        "     Print Hello, world!; AC2 checks English",
        "  3. Type something.",
        "\u2500" * 40,
        "  4. Chat about this",
        "Enter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel",
    ])
    parsed = decisions.parse_screen(screen, "blocked")
    assert parsed["options"] == ["Chinese", "English", "Type something.", "Chat about this"]
    assert parsed["details"] == ["Print a Chinese greeting; AC2 checks Chinese", "Print Hello, world!; AC2 checks English", "", ""]
    assert parsed["cursor"] == 0
    assert decisions.parse_screen(screen.replace("\u276f 1.", "  1.").replace("  4. Chat", "\u276f 4. Chat"), "blocked")["cursor"] == 3

def test_notified_waits_survive_a_restart():
    with tempfile.TemporaryDirectory(dir=serve.HERE) as root:
        serve.REGISTRY = root
        assert serve.load_notified() is None
        ask = dict(session="s", pane="w1:p1", board="b", level="stopped", done_at="1:00 PM", question="Merge?")
        seen, _ = serve.notices([ask], {}, None)
        serve.save_notified(seen)
        restored = serve.load_notified()
        assert serve.notices([ask], restored, None)[1] == []
        assert serve.notices([dict(ask, question="Deploy?")], restored, None)[1] != []

if __name__ == "__main__":
    test_ac_content_and_evidence_columns()
    test_event_log_is_trimmed()
    test_stream_view_slims_other_boards()
    test_archived_story_stays_hidden_until_new_activity()
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
    test_input_state_tells_empty_suggestion_and_typed_apart()
    test_dim_suggestion_is_returned_and_typed_text_is_not()
    test_report_without_question_is_a_stop()
    test_recap_asking_for_a_decision_counts_without_question_mark()
    test_dash_options_follow_the_question()
    test_numbered_questions_stay_in_the_question_text()
    test_blocked_choice_box_and_permission_prompt()
    test_short_question_takes_options_from_the_list_above()
    test_decision_belongs_to_subscribable_board()
    test_permission_prompt_question_skips_separators_and_tips()
    test_past_decision_in_recap_is_not_a_question()
    test_notices_fire_once_per_new_wait_and_skip_background_or_unsubscribed()
    test_choice_options_keep_their_descriptions()
    test_notified_waits_survive_a_restart()
    print("ok")
