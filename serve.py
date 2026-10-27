import glob
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SQUAD_BOARD_HOME", os.path.expanduser("~/.squad-board")))
EVENTS_DIR = os.path.join(REGISTRY, ".state")
HOST = os.environ.get("SQUAD_BOARD_HOST", "127.0.0.1")
PORT_RANGE = range(8790, 8800)
STALE_MINUTES = 20
FILE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".json": "application/json; charset=utf-8",
              ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8"}
STATUS_LINE = re.compile(r"STATUS:\s*([A-Za-z-]+)\s*(.*)")
SHOT = re.compile(r"[\w./-]*shots/[\w.\-一-鿿]+\.png")
STATUS_ZH = {"working": "进行中", "idle": "空闲", "done": "完成", "blocked": "等待输入"}

state = {"version": "", "data": {}}
events = {}
activity = {}
layouts = {}
legacy_modules = {}
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


def legacy_module(report_dir):
    if report_dir not in legacy_modules:
        spec = importlib.util.spec_from_file_location(f"legacy_{len(legacy_modules)}", os.path.join(report_dir, "build_report.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        legacy_modules[report_dir] = module
    return legacy_modules[report_dir]


def legacy_evidence(root, round_dir, groups):
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


def ac_rows(board):
    report_dir = board.get("sources", {}).get("legacy_report_dir")
    if not report_dir:
        return []
    report_dir = os.path.join(board["root"], report_dir)
    rows = legacy_module(report_dir).board_rows()
    shots = legacy_evidence(board["root"], os.path.dirname(report_dir), board.get("groups", []))
    for row in rows:
        row["status"] = row.pop("bucket")
        row["shots"] = shots.get(f"{row['card']}|{row['ac']}", [])
    return rows


def issues(board):
    pattern = board.get("sources", {}).get("issues")
    out = []
    for path in sorted(glob.glob(os.path.join(board["root"], pattern))) if pattern else []:
        try:
            data = json.load(open(path, encoding="utf-8"))
        except ValueError:
            continue
        out.append({k: data.get(k) for k in ["id", "title", "acs", "cause", "status", "round", "pr", "ci"]})
    return out


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
    return tasks, impl


def plans(board):
    out = []
    for plan in board.get("plans", []):
        files = globbed(board["root"], plan["files"])
        status = report_status(board["root"], plan["files"])
        parts = plan.get("parts") or [dict(plan=plan["files"], progress=plan.get("progress"))]
        tasks, impls = [], []
        for part in parts:
            got, impl = plan_part(board["root"], part, part.get("label", ""))
            tasks += got
            impls += [impl] if impl else []
        impl = min(impls, key=lambda r: IMPL_RANK.get(r["state"], 1)) if impls else None
        out.append(dict(id=plan["id"], repo=plan.get("repo", ""), scope=plan.get("scope", ""), covers=plan.get("covers", []),
                        state=status["state"] if status else ("missing" if not files else "unknown"),
                        tasks=len(tasks), done=sum(t["done"] for t in tasks), task_list=tasks,
                        impl=dict(state=impl["state"], detail=impl["detail"], mtime=impl["mtime"]) if impl else None,
                        tracked=any(p.get("progress") for p in parts)))
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


def worker_view(agent, tab_label, spec, root, session, task=None):
    name = agent.get("name") or ""
    report = report_status(root, task["report"]) if task and task.get("report") else None
    if report and report["mtime"] < task.get("at", 0):
        report["stale"] = True
    return dict(name=name, role=spec.get("role") or name or "无名 agent", group=spec.get("group"), tab=tab_label,
                pane=agent.get("pane_id", ""), status=agent["agent_status"], seq=agent.get("state_change_seq"),
                doing=activity.get((session, name), ""), report=report,
                task=dict(brief=task.get("brief", ""), report=task.get("report", ""), at=task.get("at")) if task else None)


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
        out.append(dict(level="fail", text=f"{m['role']}（{m['name']}）不在 herdr 里了", worker=m["name"]))
    for p in board["pending"]:
        out.append(dict(level="warn", text=f"{p.get('id', '')} 等你确认：{p.get('ask', '')}"))
    for i in board["issues"]:
        if (i.get("round") or 0) >= 3 and i.get("status") != "verified":
            out.append(dict(level="fail", text=f"{i['id']} 已到第 {i['round']} 轮仍未关闭"))
    return sorted(out, key=lambda n: n["level"] != "fail")


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
                statuses=board.get("statuses", []), phases=board.get("phases", []), workers=workers,
                missing=[dict(name=n, role=s.get("role", n)) for n, s in declared.items() if n not in present],
                rows=ac_rows(board), issues=issues(board), pending=pending(board), plans=plans(board))
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


def auto_boards(session, agents, tabs, prefixes):
    labels = sorted({tabs.get(a["tab_id"], "") for a in agents if a.get("name") and not claims(tabs.get(a["tab_id"], ""), prefixes)})
    if not labels:
        return []
    leads = [a for a in agents if not a.get("name") and not tabs.get(a["tab_id"], "").startswith("W·")]
    name = leads[0].get("terminal_title_stripped") if len(leads) == 1 else ""
    subtitle = "未登记 board.json · 本 session 唯一的 lead" if name else f"未登记 board.json · 本 session 有 {len(leads)} 个 lead，无法区分归属"
    return [dict(id="auto", name=name or f"{session} 未登记的 worker", subtitle=subtitle, root="", auto=True,
                 tabs=sorted({re.sub(r"-\d+$", "", l) for l in labels}), claimed_exact=labels)]


def snapshot():
    sessions = []
    registered = load_boards()
    for session in sorted(set(registered) | set(running_sessions())):
        boards = registered.get(session, [])
        layout = read_layout(session)
        if layout is None:
            sessions.append(dict(name=session, alive=False, boards=[dict(id=b["id"], session=session, name=b.get("name", b["id"]),
                                 subtitle=b.get("subtitle", ""), claimed=b.get("tabs", []), offline=True) for b in boards]))
            continue
        agents, tabs = layout
        layouts[session] = [a["name"] for a in agents if a.get("name")]
        prefixes = [p for b in boards for p in b.get("tabs", [])]
        guessed = auto_boards(session, agents, tabs, prefixes)
        for board in guessed:
            agents_in = [a for a in agents if a.get("name") and tabs.get(a["tab_id"], "") in board["claimed_exact"]]
            board.update(view=board_snapshot(session, board, agents_in, tabs, exact=board["claimed_exact"]))
        views = [board_snapshot(session, b, agents, tabs) for b in boards] + [dict(b["view"], auto=True) for b in guessed]
        if views:
            sessions.append(dict(name=session, alive=True, boards=views))
    return dict(sessions=sessions)


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
            out.append(dict(kind="worker", color=w["status"], text=f"{w['role']}：{STATUS_ZH.get(status, status)} → {STATUS_ZH.get(w['status'], w['status'])}"))
        elif seq is not None and w["seq"] is not None and w["seq"] - seq >= 2:
            out.append(dict(kind="worker", color="warn", text=f"{w['role']}：两次轮询之间状态变过 {w['seq'] - seq} 次，现在{STATUS_ZH.get(w['status'], w['status'])}"))
        new_report = (w["report"] or {}).get("state")
        if report and new_report and report != new_report:
            out.append(dict(kind="report", color="blocked" if new_report == "blocked" else "info", text=f"{w['role']} 报告：{report} → {new_report}"))
    gone = {m["name"] for m in new["missing"]} - {m["name"] for m in old["missing"]}
    out += [dict(kind="worker", color="fail", text=f"{n} 从 herdr 消失") for n in sorted(gone)]
    before = keyed(old["rows"], lambda r: (r["card"], r["ac"]), lambda r: r["final"])
    for r in new["rows"]:
        prev = before.get((r["card"], r["ac"]))
        if prev is not None and prev != r["final"]:
            out.append(dict(kind="ac", color=r["status"], text=f"{r['card']} {r['ac']}：{prev} → {r['final']}"))
    before = keyed(old["issues"], lambda i: i["id"], lambda i: i["status"])
    for i in new["issues"]:
        if before.get(i["id"]) != i["status"]:
            out.append(dict(kind="issue", color=i["status"], text=f"{i['id']} {(i['title'] or '')[:40]}：{before.get(i['id'], '新建')} → {i['status']}"))
    before = keyed(old["plans"], lambda p: p["id"], lambda p: (p["state"], p.get("done", 0)))
    for p in new["plans"]:
        prev = before.get(p["id"])
        if prev and prev[0] != p["state"]:
            out.append(dict(kind="plan", color="info", text=f"子计划 {p['id']}：{prev[0]} → {p['state']}"))
        if prev and p["tracked"] and prev[1] != p["done"]:
            out.append(dict(kind="plan", color="pass" if p["done"] == p["tasks"] else "info", text=f"子计划 {p['id']} 实现：{prev[1]} → {p['done']}/{p['tasks']} Task"))
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
               for b in s["boards"] for w in b.get("workers", []))


def board_root(session, board_id):
    for board in load_boards().get(session, []):
        if board["id"] == board_id:
            return os.path.realpath(board["root"])
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
        if path == "/":
            return self.send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send(200, json.dumps(state["data"], ensure_ascii=False).encode(), "application/json; charset=utf-8")
        if path == "/api/stream":
            return self.stream()
        if path.startswith("/files/"):
            return self.file(unquote(path[len("/files/"):]))
        self.send(404, b"not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/api/focus":
            return self.send(404, b"not found", "text/plain")
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return self.send(403, "只能在本机定位 herdr pane".encode(), "text/plain; charset=utf-8")
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)))
            session, pane = body["session"], body["pane"]
        except (ValueError, KeyError, TypeError):
            return self.send(400, b"bad request", "text/plain")
        if not focusable(session, pane):
            return self.send(404, "这个 pane 已经不在了".encode(), "text/plain; charset=utf-8")
        ok = '"focused":true' in run(["herdr", "--session", session, "agent", "focus", pane], timeout=10).replace(" ", "")
        self.send(200 if ok else 502, json.dumps(dict(ok=ok, session=session), ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def stream(self):
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
                self.wfile.write(b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n" if payload else b": waiting\n\n")
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
        self.send(200, open(full, "rb").read(), ctype)


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
