import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SQUAD_BOARD_HOME", os.path.expanduser("~/.squad-board")))
EVENTS_DIR = os.path.join(REGISTRY, ".state")
ARCHIVE_DIR = os.path.join(REGISTRY, ".archive")
LOCAL_CLIENTS = ("127.0.0.1", "::1")
HOST = os.environ.get("SQUAD_BOARD_HOST", "127.0.0.1")
PORT_RANGE = range(8790, 8800)
STALE_MINUTES = 20
FILE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".json": "application/json; charset=utf-8",
              ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
              ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".vtt": "text/vtt; charset=utf-8"}
VIDEO_TYPES = (".mp4", ".webm", ".mov")
RANGE = re.compile(r"bytes=(\d*)-(\d*)$")
STATUS_LINE = re.compile(r"STATUS:\s*([A-Za-z-]+)\s*(.*)")
SHOT = re.compile(r"[\w./*-]*shots/[\w.*\-一-鿿]+\.png")
AC_COLUMNS = ["card", "ac", "cat", "how", "qa", "qam", "issue", "final"]
STATUS_ZH = {"working": "进行中", "idle": "空闲", "done": "完成", "blocked": "等待输入"}

state = {"version": "", "data": {}}
events = {}
activity = {}
layouts = {}
lock = threading.Lock()
changed = threading.Condition(lock)


def run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def herdr(session, *args):
    try:
        return json.loads(run(["herdr", "--session", session, *args]))["result"]
    except Exception:
        return None


def read_layout(session):
    listed = herdr(session, "agent", "list")
    if listed is None:
        return None
    tabs = {}
    for ws in (herdr(session, "workspace", "list") or {}).get("workspaces", []):
        for tab in (herdr(session, "tab", "list", "--workspace", ws["workspace_id"]) or {}).get("tabs", []):
            tabs[tab["tab_id"]] = tab["label"]
    return listed["agents"], tabs


def claims(label, prefixes):
    return any(label == p or re.fullmatch(re.escape(p) + r"-\d+", label) for p in prefixes)


def as_list(value):
    return value if isinstance(value, list) else [value]


def report_status(root, patterns):
    found = []
    for path in sorted(f for p in as_list(patterns) for f in glob.glob(os.path.join(root, p))):
        with open(path, encoding="utf-8") as fh:
            match = STATUS_LINE.match(fh.readline().strip())
        if match:
            found.append(dict(state=match.group(1).lower(), detail=match.group(2).strip("（）() ")[:140],
                              mtime=os.path.getmtime(path), file=os.path.relpath(path, root) if root else path))
    if not found:
        return None
    return next((s for s in found if s["state"] != "done"), found[0])


def report_evidence(root, round_dir, groups):
    found = {}
    for group in groups:
        path = os.path.join(round_dir, group, "REPORT.md")
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 4 or not re.match(r"^\d{5}$", cells[0]):
                continue
            shots = [os.path.relpath(full, root) for full in
                     (os.path.normpath(os.path.join(round_dir, group, t)) for t in SHOT.findall(line)) if os.path.exists(full)]
            if shots:
                found[f"{cells[0]}|{cells[1]}"] = shots
    return found


def classify(text, statuses, fallback):
    text = text.strip()
    for status in statuses:
        if any(text.startswith(p) for p in status.get("prefix", [])) or any(m in text for m in status.get("match", [])):
            return status["id"]
    return fallback


def cell_shots(root, base, cells):
    found = []
    for token in (t for c in cells for t in SHOT.findall(c)):
        found += [os.path.relpath(f, root) for f in sorted(glob.glob(os.path.join(base, token))) if os.path.isfile(f)]
    return found


AC_FILES = ["docs/AC.md", "AC.md"]
COLUMN_NAMES = [("card", ["卡", "卡号", "card"]), ("ac", ["ac"]), ("evidence", ["证据", "截图", "evidence"]), ("qam", ["qam 核对", "核对", "qam"]),
                ("qa", ["qa 结论", "qa"]), ("issue", ["issue", "缺陷", "bug"]), ("desc", ["内容", "描述", "说明", "标题", "description", "title"]),
                ("cat", ["类别", "分类", "功能", "category"]),
                ("how", ["执行方式", "方式", "how", "story"]), ("final", ["最终状态", "状态", "结论", "结果", "status", "result"])]
DEFAULT_STATUSES = [
    dict(id="PASS", label="通过", color="pass", done=True, **{"pass": True}, prefix=["PASS", "通过", "✅"]),
    dict(id="部分通过", label="部分通过", color="warn", done=True, match=["部分PASS", "部分 PASS", "部分通过"]),
    dict(id="FAIL", label="失败", color="fail", done=True, match=["FAIL", "不通过", "失败", "❌"]),
    dict(id="待复测", label="待复测", color="info", match=["待复测", "复测"]),
    dict(id="阻塞", label="阻塞", color="info", match=["阻塞", "BLOCKED", "blocked"]),
    dict(id="不适用", label="不适用", color="violet", done=True, na=True, match=["不适用", "N/A"]),
    dict(id="不可测", label="不可测", color="violet", done=True, na=True, match=["不可测"]),
    dict(id="未执行", label="未执行", color="idle", match=["未执行", "未开始", "未报告", "待验", "TODO"]),
    dict(id="其他", label="其他", color="muted"),
]


def header_columns(cells):
    names = [c.strip().lower() for c in cells]
    if "ac" not in names:
        return None
    columns = []
    for name in names:
        exact = next((field for field, aliases in COLUMN_NAMES if name in aliases), None)
        columns.append(exact or next((field for field, aliases in COLUMN_NAMES if any(a in name for a in aliases)), ""))
    return columns


def ac_spec(board):
    spec = board.get("sources", {}).get("ac_table")
    if spec:
        return spec
    found = next((f for f in AC_FILES if board.get("root") and os.path.isfile(os.path.join(board["root"], f))), None)
    return dict(file=found, empty="未执行", other="其他") if found else None


def ac_rows(board):
    spec = ac_spec(board)
    path = os.path.join(board["root"], spec["file"]) if spec else ""
    if not spec or not os.path.isfile(path):
        return []
    base, sections = os.path.dirname(path), spec.get("sections")
    positional = spec.get("columns", AC_COLUMNS)
    key = re.compile(spec.get("key", r"\d{4,}"))
    default_card = (re.search(r"\d{3,}", f"{board.get('name', '')} {board.get('id', '')}") or [board.get("id", "")])[0]
    statuses = board.get("statuses") or DEFAULT_STATUSES
    report_shots = report_evidence(board["root"], base, board.get("groups", []))
    rows, section, group, columns = [], (None if sections else True), "—", None
    for line in open(path, encoding="utf-8"):
        if line.startswith("## "):
            section = next((k for k in sections if line.startswith(k)), None) if sections else True
            group, columns = "—", None
            continue
        if section is None or not line.startswith("|") or re.match(r"^\|\s*:?-", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header_columns(cells):
            columns = header_columns(cells)
            continue
        if re.fullmatch(r"\*\*(.+)\*\*", cells[0]):
            group = cells[0].strip("*")
            continue
        row = dict(zip(columns or positional, cells))
        row.pop("", None)
        has_card = "card" in (columns or positional)
        row.setdefault("card", default_card)
        if not row.get("ac") or (has_card and not key.fullmatch(row["card"])) or (not columns and len(cells) < len(positional)):
            continue
        row.pop("evidence", None)
        for field in AC_COLUMNS + ["desc"]:
            row.setdefault(field, "")
        row["group"] = group
        final = row["final"]
        forced = sections[section] if sections else None
        row["status"] = forced or (spec.get("empty", "") if final in ("", "—") else classify(final, statuses, spec.get("other", "")))
        row["shots"] = list(dict.fromkeys(report_shots.get(f"{row['card']}|{row['ac']}", []) + cell_shots(board["root"], base, cells)))
        rows.append(row)
    return rows


ISSUE_KEYS = ["id", "title", "acs", "cause", "status", "round", "pr", "ci", "severity", "group", "repro", "expected", "actual"]


def root_rel(root, path):
    if not root:
        return ""
    base, full = os.path.realpath(root), os.path.realpath(os.path.join(root, path))
    return os.path.relpath(full, base) if full.startswith(base + os.sep) and os.path.isfile(full) else ""


def issues(board):
    pattern = board.get("sources", {}).get("issues")
    out = []
    for path in sorted(glob.glob(os.path.join(board["root"], pattern))) if pattern else []:
        try:
            data = json.load(open(path, encoding="utf-8"))
        except ValueError:
            continue
        item = {k: data.get(k) for k in ISSUE_KEYS}
        fix = data.get("fix") if isinstance(data.get("fix"), dict) else {}
        item["pr"] = item["pr"] or fix.get("pr")
        evidence = [e for e in as_list(data.get("evidence") or []) if isinstance(e, str)]
        item["evidence"] = [rel for rel in (root_rel(board["root"], os.path.join(os.path.dirname(path), e)) for e in evidence) if rel]
        item["notes"] = {k: v for k, v in data.items() if k not in ISSUE_KEYS + ["evidence", "fix", "cards"] and isinstance(v, str) and v.strip()}
        item["file"] = root_rel(board["root"], path)
        out.append(item)
    return out


def demos(board):
    pattern = board.get("sources", {}).get("demos") or (["demos/*.mp4", "demos/*.webm", "demos/*.mov"]
                                                         if board.get("root") and os.path.isdir(os.path.join(board["root"], "demos")) else None)
    if not pattern:
        return None
    root, tags = board["root"], set(board.get("cards", {})) | {p["id"] for p in board.get("plans", [])}
    found = []
    for path in globbed(root, pattern):
        if os.path.splitext(path)[1].lower() not in VIDEO_TYPES or not root_rel(root, path):
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        sidecar = os.path.splitext(path)[0] + ".chapters.json"
        try:
            chapters = [dict(t=float(c["t"]), label=str(c.get("label", "")), result=str(c.get("result", ""))) for c in json.load(open(sidecar, encoding="utf-8"))] if os.path.isfile(sidecar) else []
        except (ValueError, KeyError, TypeError):
            chapters = []
        found.append(dict(file=root_rel(root, path), title=stem, size=os.path.getsize(path), mtime=int(os.path.getmtime(path)),
                          tags=[t for t in re.split(r"[-_ .]+", stem) if t in tags], chapters=chapters,
                          subtitles=root_rel(root, os.path.splitext(path)[0] + ".vtt")))
    return sorted(found, key=lambda d: -d["mtime"])


CHECK_ITEM = re.compile(r"^- \[([ xX])\] (.+)$", re.M)


def closeout(board):
    rel = board.get("sources", {}).get("closeout") or next((f for f in ("docs/CLOSEOUT.md", "CLOSEOUT.md")
                                                            if board.get("root") and os.path.isfile(os.path.join(board["root"], f))), None)
    if not rel:
        return None
    path = os.path.join(board["root"], rel)
    if not os.path.isfile(path):
        return dict(file=rel, exists=False)
    text = open(path, encoding="utf-8").read()
    tables, section = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            tables[section] = []
        elif section and line.startswith("|") and not re.match(r"^\|\s*:?-", line):
            tables[section].append([c.strip() for c in line.strip().strip("|").split("|")])
    status = report_status(board["root"], rel)
    return dict(file=root_rel(board["root"], rel) or rel, exists=True, state=status["state"] if status else "unknown",
                detail=status["detail"] if status else "", mtime=int(os.path.getmtime(path)),
                items=[dict(done=mark != " ", text=item) for mark, item in CHECK_ITEM.findall(text)],
                tables={k: v[1:] for k, v in tables.items() if len(v) > 1})


def pending(board):
    path = board.get("sources", {}).get("pending")
    try:
        return json.load(open(os.path.join(board["root"], path), encoding="utf-8")) if path else []
    except (OSError, ValueError):
        return []


PLAN_TASK = re.compile(r"^### Task\s+([^:：\s]+)\s*[:：]?\s*(.*)$", re.M)
DONE_TASK = re.compile(r"^Task\s+(\S+)\s*✔\s*(\S*)\s*(.*)$", re.M)
SKIP_TASK = re.compile(r"^Task\s+(\S+)\s+[—–]\s+(.*)$", re.M)


def task_ids(token):
    first, *rest = token.split("/")
    prefix = re.sub(r"\d+$", "", first)
    return [first] + [r if not r.isdigit() else prefix + r for r in rest]
IMPL_RANK = {"blocked": 0, "in-progress": 1, "done": 2}


def globbed(root, patterns):
    return sorted(f for p in as_list(patterns) if p for f in glob.glob(os.path.join(root, p)))


def plan_part(root, part, label):
    tasks = []
    for path in globbed(root, part["plan"]):
        tasks += [dict(id=i, title=t.strip("` ")[:120], part=label) for i, t in PLAN_TASK.findall(open(path, encoding="utf-8").read())]
    done, impl = {}, None
    files = dict(label=label, plan=[root_rel(root, f) for f in globbed(root, part["plan"])], progress=[root_rel(root, f) for f in globbed(root, part.get("progress"))])
    for path in globbed(root, part.get("progress")):
        text = open(path, encoding="utf-8").read()
        for token, note in SKIP_TASK.findall(text):
            done.update({i: ("", "跳过：" + note[:150]) for i in task_ids(token)})
        for token, sha, note in DONE_TASK.findall(text):
            done.update({i: (sha, note[:160]) for i in task_ids(token)})
        impl = report_status(root, os.path.relpath(path, root))
    for task in tasks:
        sha, note = done.get(task["id"], ("", ""))
        task.update(done=task["id"] in done, sha=sha, note=note)
    return tasks, impl, files


def first_heading(path):
    for line in open(path, encoding="utf-8"):
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def conventional_plans(root):
    if not root:
        return [], []
    planned = {}
    for path in sorted(glob.glob(os.path.join(root, "docs", "plans", "*.md"))):
        if PLAN_TASK.search(open(path, encoding="utf-8").read()):
            planned.setdefault(os.path.basename(path).split("-")[0].split(".")[0], []).append(os.path.relpath(path, root))
    if planned:
        return [dict(id=pid, files=files, progress=f"docs/exec/STATUS-{pid}.md") for pid, files in planned.items()], []
    briefs = {os.path.basename(p)[len("brief-"):-3]: p for p in sorted(glob.glob(os.path.join(root, "docs", "brief-*.md")))}
    reported = {k for k in briefs if os.path.isfile(os.path.join(root, "docs", f"report-{k}.md"))}
    items = []
    for key, path in briefs.items():
        if key not in reported and any(key.startswith(k + "-") for k in reported):
            continue
        title = re.sub(r"^任务书[:：]\s*", "", first_heading(path)) or key
        report_rel = f"docs/report-{key}.md"
        found = report_status(root, report_rel) if key in reported else None
        done = bool(found and found["state"] == "done")
        items.append(dict(id=key, kind="workitem", repo="", scope=title, covers=[], state="done", tasks=1, done=int(done),
                          task_list=[dict(id=key, title=title, part="", done=done, sha="", note=found["detail"] if found else "还没有报告")],
                          impl=dict(state=found["state"], detail=found["detail"], mtime=found["mtime"]) if found else None, tracked=True,
                          files=[dict(label="", plan=[os.path.relpath(path, root)], progress=[found["file"]] if found else [])]))
    return [], items


def plans(board):
    out = []
    configured, items = (board.get("plans"), []) if board.get("plans") else conventional_plans(board.get("root", ""))
    out += items
    for plan in configured:
        files = globbed(board["root"], plan["files"])
        status = report_status(board["root"], plan["files"])
        parts = plan.get("parts") or [dict(plan=plan["files"], progress=plan.get("progress"))]
        tasks, impls, part_files = [], [], []
        for part in parts:
            got, impl, files = plan_part(board["root"], part, part.get("label", ""))
            tasks += got
            impls += [impl] if impl else []
            part_files.append(files)
        impl = min(impls, key=lambda r: IMPL_RANK.get(r["state"], 1)) if impls else None
        out.append(dict(id=plan["id"], repo=plan.get("repo", ""), scope=plan.get("scope", ""), covers=plan.get("covers", []),
                        state=status["state"] if status else ("missing" if not files else "unknown"),
                        tasks=len(tasks), done=sum(t["done"] for t in tasks), task_list=tasks,
                        impl=dict(state=impl["state"], detail=impl["detail"], mtime=impl["mtime"]) if impl else None,
                        tracked=any(p.get("progress") for p in parts), files=part_files))
    return out


def dispatch_path(session, board_id):
    return os.path.join(REGISTRY, session, f"{board_id}.dispatch.jsonl")


def load_dispatch(session, board_id):
    latest = {}
    path = dispatch_path(session, board_id)
    for line in open(path, encoding="utf-8") if os.path.exists(path) else []:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("worker"):
            latest[record["worker"]] = record
    return latest


def latest_report(root, report):
    if not report:
        return report
    folder = os.path.dirname(os.path.join(root, report["file"]))
    newer = sorted((f for f in glob.glob(os.path.join(folder, "*.md")) if os.path.getmtime(f) > report["mtime"]), key=os.path.getmtime, reverse=True)
    for path in newer:
        found = report_status(root, os.path.relpath(path, root))
        if found:
            return dict(found, followed=True)
    return report


def worker_view(agent, tab_label, spec, root, session, task=None):
    name = agent.get("name") or ""
    report = latest_report(root, report_status(root, task["report"])) if task and task.get("report") else None
    if report and report["mtime"] < task.get("at", 0):
        report["stale"] = True
    return dict(name=name, role=spec.get("role") or name or "无名 agent", group=spec.get("group"), tab=tab_label,
                pane=agent.get("pane_id", ""), status=agent["agent_status"], seq=agent.get("state_change_seq"),
                doing=activity.get((session, name), ""), report=report,
                task=dict(brief=task.get("brief", ""), report=task.get("report", ""), at=task.get("at"),
                          brief_href=root_rel(root, task.get("brief", "")), report_href=root_rel(root, task.get("report", ""))) if task else None)


def needs(board):
    out, now = [], time.time()
    for w in board["workers"]:
        rep, at = w["report"], dict(worker=w["name"], tab=w["tab"], pane=w.get("pane", ""))
        if not w["name"]:
            out.append(dict(level="warn", text="无名 agent，可能是 worker 重启后丢了 name", **at))
        elif w["status"] == "blocked":
            out.append(dict(level="fail", text=f"{w['role']} 在等输入", **at))
        if rep and rep.get("stale"):
            if w["status"] in ("idle", "done"):
                out.append(dict(level="warn", text=f"{w['role']} 已{STATUS_ZH.get(w['status'])}，但这次任务的报告还没写（{rep['file']} 仍是派活前的版本）", **at))
        elif rep and rep["state"] == "blocked" and w["status"] != "blocked":
            out.append(dict(level="fail", text=f"{w['role']} 显示{STATUS_ZH.get(w['status'], w['status'])}，但报告写 blocked：{rep['detail']}", **at))
        elif rep and rep["state"] == "in-progress" and w["status"] in ("idle", "done") and now - rep["mtime"] > STALE_MINUTES * 60:
            minutes = int((now - rep["mtime"]) / 60)
            out.append(dict(level="warn", text=f"{w['role']} 已{STATUS_ZH.get(w['status'])}，但报告仍是 in-progress，{minutes} 分钟没更新", **at))
    for m in board["missing"]:
        out.append(dict(level="fail", text=f"{m['role']}（{m['name']}）已不在 herdr，但任务报告还是 {m['report']}", worker=m["name"]))
    for p in board["pending"]:
        out.append(dict(level="warn", text=f"{p.get('id', '')} 等你确认：{p.get('ask', '')}"))
    for i in board["issues"]:
        if (i.get("round") or 0) >= 3 and i.get("status") not in ("verified", "rejected"):
            out.append(dict(level="fail", text=f"{i['id']} 已到第 {i['round']} 轮仍未关闭"))
    return sorted(out, key=lambda n: n["level"] != "fail")


def interrupted(root, declared, tasks, present):
    out = []
    for name in sorted((set(declared) | set(tasks)) - present):
        task = tasks.get(name) or {}
        report = latest_report(root, report_status(root, task["report"])) if task.get("report") and root else None
        if report and report["state"] != "done":
            out.append(dict(name=name, role=declared.get(name, {}).get("role", name), report=report["state"], file=report["file"]))
    return out


def board_snapshot(session, board, agents, tabs, exact=None):
    root = board["root"]
    declared = board.get("workers", {})
    mine = agents if exact else [a for a in agents if claims(tabs.get(a["tab_id"], ""), board.get("tabs", []))]
    tasks = load_dispatch(session, board["id"])
    workers = [worker_view(a, tabs.get(a["tab_id"], ""), declared.get(a.get("name"), {}), root, session, tasks.get(a.get("name")))
               for a in mine]
    present = {w["name"] for w in workers}
    data = dict(id=board["id"], session=session, name=board.get("name", board["id"]), subtitle=board.get("subtitle", ""),
                claimed=board.get("tabs", []), tabs=sorted({w["tab"] for w in workers}), cards=board.get("cards", {}),
                statuses=board.get("statuses") or DEFAULT_STATUSES, phases=board.get("phases", []), workers=workers,
                missing=interrupted(root, declared, tasks, present),
                rows=ac_rows(board), issues=issues(board), pending=pending(board), plans=plans(board), demos=demos(board), closeout=closeout(board),
                demo_dir=os.path.dirname(as_list(board.get("sources", {}).get("demos") or [""])[0]))
    if not data["cards"] and data["rows"]:
        spec = ac_spec(board)
        title = first_heading(os.path.join(root, spec["file"])) if spec and spec.get("file") else ""
        codes = list(dict.fromkeys(r["card"] for r in data["rows"]))
        name = re.sub(r"\bAC\b|验收标准|[A-Z]+-\d+", "", title).strip(" ·:：-—") if len(codes) == 1 else ""
        data["cards"] = {card: name or card for card in codes}
    data["needs"] = needs(data)
    return data


def load_boards():
    found = {}
    for session_dir in sorted(glob.glob(os.path.join(REGISTRY, "*"))):
        if not os.path.isdir(session_dir):
            continue
        session = os.path.basename(session_dir)
        for path in sorted(glob.glob(os.path.join(session_dir, "*.json"))):
            try:
                board = json.load(open(path, encoding="utf-8"))
            except ValueError as error:
                print(f"skip {path}: {error}", file=sys.stderr)
                continue
            board.setdefault("id", os.path.splitext(os.path.basename(path))[0])
            found.setdefault(session, []).append(board)
    return found


def running_sessions():
    try:
        listed = json.loads(run(["herdr", "session", "list", "--json"]))["sessions"]
    except Exception:
        return []
    return [s["name"] for s in listed if s.get("running")]


def load_config():
    path = os.path.join(REGISTRY, "config.json")
    try:
        return json.load(open(path, encoding="utf-8")) if os.path.isfile(path) else {}
    except ValueError:
        return {}


def story_of(path, workspaces):
    real = os.path.realpath(path) if path else ""
    for workspace in workspaces:
        base = os.path.realpath(os.path.expanduser(workspace))
        if real.startswith(base + os.sep):
            return os.path.join(base, os.path.relpath(real, base).split(os.sep)[0])
    return None


def agent_dir(agent):
    return agent.get("foreground_cwd") or agent.get("cwd") or ""


def session_board(session, loose, agents, tabs):
    labels = sorted({tabs.get(a["tab_id"], "") for a in loose})
    leads = [a for a in agents if not a.get("name") and not tabs.get(a["tab_id"], "").startswith("W·")]
    name = leads[0].get("terminal_title_stripped") if len(leads) == 1 else ""
    subtitle = "未登记 board.json · 本 session 唯一的 lead" if name else f"未登记 board.json · 本 session 有 {len(leads)} 个 lead，无法区分归属"
    return dict(id="auto", name=name or f"{session} 未登记的 worker", subtitle=subtitle, root="", auto=True,
                tabs=sorted({re.sub(r"-\d+$", "", l) for l in labels}))


def story_board(story, members, tabs):
    labels = {tabs.get(a["tab_id"], "") for a in members}
    return dict(id=f"story-{os.path.basename(story)}", name=os.path.basename(story), subtitle="未登记 board.json · 按 story 目录自动识别",
                root=story, auto=True, tabs=sorted({re.sub(r"-\d+$", "", l) for l in labels}))


def assign_workers(boards, agents, tabs, workspaces):
    stories = {b["id"]: story_of(b.get("root", ""), workspaces) for b in boards}
    owned, by_story, loose = {b["id"]: [] for b in boards}, {}, []
    for agent in (a for a in agents if a.get("name")):
        label = tabs.get(agent["tab_id"], "")
        story = story_of(agent_dir(agent), workspaces)
        owner = next((b["id"] for b in boards if claims(label, b.get("tabs", []))), None) \
            or next((b["id"] for b in boards if story and stories[b["id"]] == story), None)
        if owner:
            owned[owner].append(agent)
        elif story:
            by_story.setdefault(story, []).append(agent)
        else:
            loose.append(agent)
    return owned, by_story, loose


RECENT_SECONDS = 24 * 3600


def story_activity(story):
    marks = [os.path.join(story, f) for f in ("docs/AC.md", "AC.md", "docs/CLOSEOUT.md", "CLOSEOUT.md")]
    marks += glob.glob(os.path.join(story, "demos", "*"))
    times = [os.path.getmtime(f) for f in marks if os.path.isfile(f)]
    return max(times) if times else 0


def recent_stories(workspaces, shown):
    found = []
    for workspace in workspaces:
        base = os.path.realpath(os.path.expanduser(workspace))
        for story in sorted(glob.glob(os.path.join(base, "*"))):
            if os.path.isdir(story) and story not in shown and time.time() - story_activity(story) < RECENT_SECONDS and not archived_story(story):
                found.append(story)
    return found


DECISION_TTL = 30
screen_cache = {}


def read_screen(session, pane):
    return run(["herdr", "--session", session, "pane", "read", pane, "--source", "visible", "--ansi"], timeout=10)


def lead_decisions(session, agents, tabs, workspaces):
    try:
        from decisions import parse_screen
    except ImportError:
        return []
    out = []
    for agent in agents:
        if agent.get("name") or tabs.get(agent["tab_id"], "").startswith("W·") or agent["agent_status"] == "working":
            continue
        key, seq = (session, agent["pane_id"]), agent.get("state_change_seq")
        cached = screen_cache.get(key)
        if not cached or cached["seq"] != seq or time.time() - cached["at"] > DECISION_TTL:
            parsed = parse_screen(read_screen(session, agent["pane_id"]), agent["agent_status"])
            cached = screen_cache[key] = dict(seq=seq, at=time.time(), parsed=parsed)
        if cached["parsed"]:
            story = story_of(agent_dir(agent), workspaces)
            out.append(dict(cached["parsed"], session=session, pane=agent["pane_id"], status=agent["agent_status"],
                            title=agent.get("terminal_title_stripped") or "lead", story=os.path.basename(story) if story else ""))
    return out


def snapshot():
    sessions = []
    decisions = []
    registered = load_boards()
    workspaces = load_config().get("workspaces", [])
    shown = set()
    for session in sorted(set(registered) | set(running_sessions())):
        boards = registered.get(session, [])
        layout = read_layout(session)
        if layout is None:
            sessions.append(dict(name=session, alive=False, boards=[dict(id=b["id"], session=session, name=b.get("name", b["id"]),
                                 subtitle=b.get("subtitle", ""), claimed=b.get("tabs", []), offline=True) for b in boards]))
            continue
        agents, tabs = layout
        layouts[session] = [a["name"] for a in agents if a.get("name")]
        owned, by_story, loose = assign_workers(boards, agents, tabs, workspaces)
        decisions += lead_decisions(session, agents, tabs, workspaces)
        leads = [a for a in agents if not a.get("name") and not tabs.get(a["tab_id"], "").startswith("W·")]
        if loose and len(by_story) == 1 and len(leads) == 1:
            next(iter(by_story.values())).extend(loose)
            loose = []
        board_stories = {story_of(b.get("root", ""), workspaces) for b in boards}
        for lead in leads:
            story = story_of(agent_dir(lead), workspaces)
            if story and story not in board_stories:
                by_story.setdefault(story, [])
        for story in [k for k in by_story if archived_story(k)]:
            shown.add(story)
            del by_story[story]
        shown |= board_stories | set(by_story)
        views = [board_snapshot(session, b, owned[b["id"]], tabs, exact=True) for b in boards]
        views += [dict(board_snapshot(session, story_board(story, members, tabs), members, tabs, exact=True), auto=True)
                  for story, members in sorted(by_story.items())]
        if loose:
            views.append(dict(board_snapshot(session, session_board(session, loose, agents, tabs), loose, tabs, exact=True), auto=True))
        if views:
            sessions.append(dict(name=session, alive=True, boards=views))
    idle = recent_stories(workspaces, shown)
    if idle:
        sessions.append(dict(name="工作区（无 herdr 会话）", alive=True,
                             boards=[dict(board_snapshot("workspace", story_board(story, [], {}), [], {}, exact=True), auto=True) for story in idle]))
    return dict(sessions=sessions, decisions=decisions, archives=[{k: v for k, v in a.items() if k != "root"} for a in list_archives()])


def keyed(items, key, value):
    return {key(i): value(i) for i in items}


def diff_board(old, new):
    out = []
    if not old or old.get("offline") or new.get("offline"):
        return out
    before = keyed(old["workers"], lambda w: w["name"], lambda w: (w["status"], w["seq"], (w["report"] or {}).get("state")))
    for w in new["workers"]:
        prev = before.get(w["name"])
        if not prev or not w["name"]:
            continue
        status, seq, report = prev
        if status != w["status"]:
            out.append(dict(kind="worker", color=w["status"], worker=w["name"], text=f"{w['role']}：{STATUS_ZH.get(status, status)} → {STATUS_ZH.get(w['status'], w['status'])}"))
        elif seq is not None and w["seq"] is not None and w["seq"] - seq >= 2:
            out.append(dict(kind="worker", color="warn", worker=w["name"], text=f"{w['role']}：两次轮询之间状态变过 {w['seq'] - seq} 次，现在{STATUS_ZH.get(w['status'], w['status'])}"))
        new_report = (w["report"] or {}).get("state")
        if report and new_report and report != new_report:
            out.append(dict(kind="report", color="blocked" if new_report == "blocked" else "info", worker=w["name"], text=f"{w['role']} 报告：{report} → {new_report}"))
    gone = {m["name"] for m in new["missing"]} - {m["name"] for m in old["missing"]}
    out += [dict(kind="worker", color="fail", worker=n, text=f"{n} 从 herdr 消失") for n in sorted(gone)]
    before = keyed(old["rows"], lambda r: (r["card"], r["ac"]), lambda r: r["final"])
    for r in new["rows"]:
        prev = before.get((r["card"], r["ac"]))
        if prev is not None and prev != r["final"]:
            out.append(dict(kind="ac", color=r["status"], card=r["card"], ac=r["ac"], text=f"{r['card']} {r['ac']}：{prev} → {r['final']}"))
    before = keyed(old["issues"], lambda i: i["id"], lambda i: i["status"])
    for i in new["issues"]:
        if before.get(i["id"]) != i["status"]:
            out.append(dict(kind="issue", color=i["status"], issue=i["id"], text=f"{i['id']} {(i['title'] or '')[:40]}：{before.get(i['id'], '新建')} → {i['status']}"))
    before = keyed(old["plans"], lambda p: p["id"], lambda p: (p["state"], p.get("done", 0)))
    for p in new["plans"]:
        prev = before.get(p["id"])
        if prev and prev[0] != p["state"]:
            out.append(dict(kind="plan", color="info", plan=p["id"], text=f"子计划 {p['id']}：{prev[0]} → {p['state']}"))
        if prev and p["tracked"] and prev[1] != p["done"]:
            out.append(dict(kind="plan", color="pass" if p["done"] == p["tasks"] else "info", plan=p["id"], text=f"子计划 {p['id']} 实现：{prev[1]} → {p['done']}/{p['tasks']} Task"))
    before, after = (old.get("closeout") or {}).get("state"), (new.get("closeout") or {}).get("state")
    if after and before != after:
        out.append(dict(kind="closeout", color="pass" if after == "done" else "info", closeout=True, text=f"收尾：{before or '未开始'} → {after}"))
    return out


def events_path(session, board_id):
    return os.path.join(EVENTS_DIR, session, f"{board_id}.events.jsonl")


def board_events(session, board_id):
    key = (session, board_id)
    if key not in events:
        events[key] = []
        path = events_path(session, board_id)
        if os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                try:
                    events[key].append(json.loads(line))
                except ValueError:
                    pass
        del events[key][:-200]
    return events[key]


EVENT_LOG_KEEP = 2000


def trim_log(path, keep=EVENT_LOG_KEEP):
    if os.path.getsize(path) < 512 * 1024:
        return
    lines = open(path, encoding="utf-8").readlines()
    if len(lines) > keep:
        open(path, "w", encoding="utf-8").writelines(lines[-keep:])


def record(old, new):
    previous = {(b["session"], b["id"]): b for s in old.get("sessions", []) for b in s["boards"]}
    stamp = time.strftime("%H:%M:%S")
    for session in new["sessions"]:
        for board in session["boards"]:
            log = board_events(session["name"], board["id"])
            fresh = diff_board(previous.get((session["name"], board["id"])), board)
            if fresh:
                os.makedirs(os.path.dirname(events_path(session["name"], board["id"])), exist_ok=True)
                with open(events_path(session["name"], board["id"]), "a", encoding="utf-8") as fh:
                    for e in fresh:
                        e["at"], e["ts"] = stamp, int(time.time())
                        log.append(e)
                        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
                trim_log(events_path(session["name"], board["id"]))
                del log[:-200]
            board["events"] = list(reversed(log[-60:]))


def poll():
    while True:
        try:
            data = snapshot()
        except Exception as error:
            print("snapshot failed:", error, file=sys.stderr)
            time.sleep(5)
            continue
        with changed:
            record(state["data"], data)
            data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            digest = hashlib.md5(json.dumps({k: v for k, v in data.items() if k != "updated"}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if digest != state["version"]:
                state.update(version=digest, data=data)
                changed.notify_all()
            else:
                state["data"]["updated"] = data["updated"]
        time.sleep(4)


def refresh_activity():
    while True:
        for session, names in list(layouts.items()):
            for name in names:
                screen = run(["herdr", "--session", session, "agent", "read", name, "--source", "recent-unwrapped"], timeout=10)
                lines = [l.strip() for l in screen.splitlines() if l.strip().startswith("⏺") and "Monitor event" not in l]
                if lines:
                    activity[(session, name)] = lines[-1].lstrip("⏺ ").strip()[:160]
        time.sleep(15)


def focusable(session, pane):
    return any(w.get("pane") == pane for s in state["data"].get("sessions", []) if s["name"] == session
               for b in s["boards"] for w in b.get("workers", [])) or awaiting(session, pane) is not None


def awaiting(session, pane):
    return next((d for d in state["data"].get("decisions", []) if d["session"] == session and d["pane"] == pane), None)


def send_reply(session, pane, text):
    from decisions import input_state
    decision = awaiting(session, pane)
    if not decision:
        return 404, "这个会话现在没有在等你决定"
    if decision.get("kind") != "question":
        return 409, "这是选择框或权限确认，请定位到会话里操作"
    text = " ".join(str(text).split())
    if not text or len(text) > 2000:
        return 400, "回复不能为空，也不能超过 2000 字"
    box = input_state(read_screen(session, pane))
    if box not in ("empty", "suggestion"):
        return 409, "会话输入框里有你还没发送的内容，没有覆盖；请到会话里处理" if box == "typed" else "读不到会话的输入框，没有发送"
    run(["herdr", "--session", session, "pane", "send-text", pane, text], timeout=10)
    run(["herdr", "--session", session, "pane", "send-keys", pane, "Enter"], timeout=10)
    screen_cache.pop((session, pane), None)
    return 200, "已发送"


def slim(board):
    out = dict(board, slim=True)
    out["rows"] = [dict(card=r["card"], status=r["status"]) for r in board.get("rows", [])]
    out["issues"] = [dict(id=i["id"], status=i["status"], round=i.get("round")) for i in board.get("issues", [])]
    out["plans"] = [{k: p[k] for k in ("id", "kind", "tasks", "done", "tracked", "state", "covers") if k in p} for p in board.get("plans", [])]
    return out


ISSUE_BODY = ("repro", "expected", "actual", "notes", "evidence")


def focused(board):
    return dict(board, issues=[{k: v for k, v in i.items() if k not in ISSUE_BODY} for i in board.get("issues", [])])


def view_for(data, focus):
    if not data:
        return data
    return dict(data, sessions=[dict(s, boards=[focused(b) if f"{b.get('session')}/{b['id']}" == focus else slim(b) for b in s["boards"]])
                                for s in data["sessions"]])


def find_issue(focus, issue_id):
    board = next((b for s in state["data"].get("sessions", []) for b in s["boards"] if f"{b.get('session')}/{b['id']}" == focus), None)
    return next((i for i in (board or {}).get("issues", []) if i.get("id") == issue_id), None)


def board_root(session, board_id):
    for board in load_boards().get(session, []):
        if board["id"] == board_id:
            return os.path.realpath(board["root"])
    archived = next((a for a in list_archives() if a["session"] == session and a["id"] == board_id and a.get("root")), None)
    return os.path.realpath(archived["root"]) if archived else None


def read_jsonl(path):
    out = []
    for line in open(path, encoding="utf-8") if os.path.exists(path) else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def archive_summary(view, archive, root):
    statuses = {s["id"]: s for s in view.get("statuses", [])}
    flag = lambda r, k: statuses.get(r["status"], {}).get(k)
    counted = [r for r in view.get("rows", []) if not flag(r, "exclude")]
    graded = [r for r in counted if not flag(r, "na")]
    plans = view.get("plans", [])
    return dict(archive=archive, session=view["session"], id=view["id"], name=view["name"], subtitle=view.get("subtitle", ""),
                root=root, archived_at=int(time.time()), workers=len(view.get("workers", [])), issues=len(view.get("issues", [])),
                rows=len(counted), graded=len(graded) if any(s.get("pass") for s in statuses.values()) else 0,
                passed=sum(1 for r in graded if flag(r, "pass")),
                tasks=sum(p["tasks"] for p in plans if p.get("tracked")), tasks_done=sum(p["done"] for p in plans if p.get("tracked")),
                demos=len(view.get("demos") or []), closed_out=(view.get("closeout") or {}).get("state") == "done")


def story_root(board_id):
    if not board_id.startswith("story-"):
        return None
    name = board_id[len("story-"):]
    for workspace in load_config().get("workspaces", []):
        candidate = os.path.join(os.path.realpath(os.path.expanduser(workspace)), name)
        if os.path.isdir(candidate):
            return candidate
    return None


def archived_story(story):
    return any(a.get("story") and a.get("root") == story and a["archived_at"] >= story_activity(story) for a in list_archives())


def archive_board(session, board_id):
    registration = os.path.join(REGISTRY, session, f"{board_id}.json")
    view = next((b for s in state["data"].get("sessions", []) if s["name"] == session for b in s["boards"] if b["id"] == board_id), None)
    story = story_root(board_id) if view and view.get("auto") else None
    if not view or view.get("offline") or (view.get("auto") and not story) or (not story and not os.path.isfile(registration)):
        return None
    archive = f"{session}/{board_id}-{time.strftime('%Y%m%d-%H%M%S')}"
    dest = os.path.join(ARCHIVE_DIR, archive)
    os.makedirs(dest)
    history = read_jsonl(events_path(session, board_id))
    root = story or json.load(open(registration, encoding="utf-8")).get("root", "")
    summary = dict(archive_summary(view, archive, root), story=bool(story))
    json.dump(dict(view, events=list(reversed(history))[:1000], archive=archive, archived=True, archived_at=summary["archived_at"]),
              open(os.path.join(dest, "snapshot.json"), "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(summary, open(os.path.join(dest, "summary.json"), "w", encoding="utf-8"), ensure_ascii=False)
    if not story:
        shutil.move(registration, os.path.join(dest, "board.json"))
    for src, name in ((os.path.join(REGISTRY, session, f"{board_id}.dispatch.jsonl"), "dispatch.jsonl"), (events_path(session, board_id), "events.jsonl")):
        if os.path.exists(src):
            shutil.move(src, os.path.join(dest, name))
    events.pop((session, board_id), None)
    return archive


def archive_dir(archive):
    base = os.path.realpath(ARCHIVE_DIR)
    full = os.path.realpath(os.path.join(base, archive))
    return full if full.startswith(base + os.sep) and os.path.isfile(os.path.join(full, "summary.json")) else None


def list_archives():
    found = []
    for path in glob.glob(os.path.join(ARCHIVE_DIR, "*", "*", "summary.json")):
        try:
            found.append(json.load(open(path, encoding="utf-8")))
        except ValueError:
            continue
    return sorted(found, key=lambda a: -a["archived_at"])


def restore_board(archive):
    src = archive_dir(archive)
    if not src:
        return "没有这份归档"
    summary = json.load(open(os.path.join(src, "summary.json"), encoding="utf-8"))
    session, board_id = summary["session"], summary["id"]
    registration = os.path.join(REGISTRY, session, f"{board_id}.json")
    if not summary.get("story"):
        if os.path.exists(registration):
            return "同名看板已经重新登记，先处理它再恢复"
        os.makedirs(os.path.dirname(registration), exist_ok=True)
        shutil.move(os.path.join(src, "board.json"), registration)
    for name, dest in (("dispatch.jsonl", os.path.join(REGISTRY, session, f"{board_id}.dispatch.jsonl")), ("events.jsonl", events_path(session, board_id))):
        if os.path.exists(os.path.join(src, name)) and not os.path.exists(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(os.path.join(src, name), dest)
    shutil.rmtree(src)
    if not os.listdir(os.path.dirname(src)):
        os.rmdir(os.path.dirname(src))
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        focus = parse_qs(urlparse(self.path).query).get("board", [""])[0]
        if path == "/":
            return self.send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send(200, json.dumps(view_for(state["data"], focus), ensure_ascii=False).encode(), "application/json; charset=utf-8")
        if path == "/api/stream":
            return self.stream(focus)
        if path.startswith("/files/"):
            return self.file(unquote(path[len("/files/"):]))
        if path == "/api/issue":
            issue = find_issue(focus, parse_qs(urlparse(self.path).query).get("id", [""])[0])
            if not issue:
                return self.send(404, "没有这条缺陷".encode(), "text/plain; charset=utf-8")
            return self.send(200, json.dumps(issue, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        if path == "/api/archive":
            src = archive_dir(parse_qs(urlparse(self.path).query).get("id", [""])[0])
            if not src:
                return self.send(404, "没有这份归档".encode(), "text/plain; charset=utf-8")
            return self.send(200, open(os.path.join(src, "snapshot.json"), "rb").read(), "application/json; charset=utf-8")
        self.send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/focus", "/api/archive", "/api/restore", "/api/reply"):
            return self.send(404, b"not found", "text/plain")
        if self.client_address[0] not in LOCAL_CLIENTS:
            return self.send(403, "只能在本机操作".encode(), "text/plain; charset=utf-8")
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 16384)))
            if not isinstance(body, dict):
                raise TypeError
        except (ValueError, TypeError):
            return self.send(400, b"bad request", "text/plain")
        reply = lambda code, data: self.send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        if path == "/api/reply":
            code, message = send_reply(body.get("session", ""), body.get("pane", ""), body.get("text", ""))
            return reply(code, dict(ok=code == 200, message=message))
        if path == "/api/focus":
            session, pane = body.get("session", ""), body.get("pane", "")
            if not focusable(session, pane):
                return self.send(404, "这个 pane 已经不在了".encode(), "text/plain; charset=utf-8")
            ok = '"focused":true' in run(["herdr", "--session", session, "agent", "focus", pane], timeout=10).replace(" ", "")
            return reply(200 if ok else 502, dict(ok=ok, session=session))
        with lock:
            if path == "/api/archive":
                archive = archive_board(body.get("session", ""), body.get("board", ""))
                if not archive:
                    return self.send(404, "这块看板不能归档（未登记或已不在）".encode(), "text/plain; charset=utf-8")
                return reply(200, dict(archive=archive))
            error = restore_board(body.get("archive", ""))
            if error:
                return self.send(409, error.encode(), "text/plain; charset=utf-8")
            return reply(200, dict(ok=True))

    def stream(self, focus=""):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seen = None
        try:
            while True:
                with changed:
                    if state["version"] == seen:
                        changed.wait(timeout=15)
                    payload, seen = state["data"], state["version"]
                self.wfile.write(b"data: " + json.dumps(view_for(payload, focus), ensure_ascii=False).encode() + b"\n\n" if payload else b": waiting\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def file(self, rest):
        parts = rest.split("/", 2)
        root = board_root(parts[0], parts[1]) if len(parts) == 3 else None
        full = os.path.realpath(os.path.join(root, parts[2])) if root else ""
        ctype = FILE_TYPES.get(os.path.splitext(full)[1].lower())
        if not root or not full.startswith(root + os.sep) or not ctype or not os.path.isfile(full):
            return self.send(404, b"not found", "text/plain")
        size = os.path.getsize(full)
        start, end, code = 0, size - 1, 200
        match = RANGE.match(self.headers.get("Range", ""))
        if match and size:
            first, last = match.groups()
            start, end = (int(first), min(int(last), size - 1) if last else size - 1) if first else (max(size - int(last or 0), 0), size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1 if size else 0))
        self.send_header("Cache-Control", "no-store")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(full, "rb") as fh:
                fh.seek(start)
                left = end - start + 1 if size else 0
                while left > 0:
                    chunk = fh.read(min(left, 1 << 16))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

def bind():
    for port in PORT_RANGE:
        try:
            return ThreadingHTTPServer((HOST, port), Handler)
        except OSError:
            continue
    sys.exit(f"no free port in {PORT_RANGE.start}-{PORT_RANGE.stop - 1}")


if __name__ == "__main__":
    server = bind()
    threading.Thread(target=refresh_activity, daemon=True).start()
    threading.Thread(target=poll, daemon=True).start()
    print(f"squad board: http://{HOST}:{server.server_address[1]}/  registry={REGISTRY}", flush=True)
    server.serve_forever()
