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


if __name__ == "__main__":
    test_unregistered_tabs_of_one_lead_form_one_board()
    test_tab_claims_follow_continuation_suffix_only()
    test_latest_dispatch_wins_and_old_report_is_stale()
    print("ok")
