# squad-board

A local, zero-dependency live board for multi-agent squads running in [herdr](https://herdr.dev).

One process watches every running herdr session and shows, per lead: worker status, what each worker is doing, whether its report is up to date, and a "needs you" list (blocked workers, stale reports, vanished workers, pending questions). Optional panels show acceptance progress, issues and sub-plans when the lead declares data sources.

- Python 3 standard library only, no install step
- Reads herdr through its CLI and reads files; never prompts workers or writes to the project. The only action is **⤢ focus**: switch herdr's focus to a worker pane, accepted from loopback only
- Serves files only from each board's `root`, with an extension allowlist
- Binds `127.0.0.1` by default

## Quick start

```bash
git clone https://github.com/Breaker-Yan/squad-board.git ~/workspaces/squad-board
python3 ~/workspaces/squad-board/serve.py          # http://127.0.0.1:8790/
```

Start at login with launchd (macOS):

```bash
~/workspaces/squad-board/install-launchd.sh                         # loopback only
SQUAD_BOARD_HOST=0.0.0.0 ~/workspaces/squad-board/install-launchd.sh  # LAN-visible, no auth
~/workspaces/squad-board/install-launchd.sh uninstall
```

`0.0.0.0` exposes worker activity and evidence files to your whole network with no authentication. Only use it on a network you trust.

## What you see without any configuration

Every running session that has at least one named worker appears in the **catalog**. Workers no board claims are merged into one auto board per session (named after the lead's terminal title when the session has exactly one lead). Subscribe to a board or a whole session to bring it into the overview and the "needs you" list; subscriptions live in your browser.

Conventions the board relies on:

| Convention | Why |
|---|---|
| Workers are started with a name | A nameless agent is treated as a lead |
| Worker tabs start with `W·`, leads are not in `W·` tabs | Tells leads and workers apart |
| A worker's report starts with `STATUS: in-progress \| done \| blocked` | Drives report state and "needs you" |

## Declaring a board (per lead)

Write `~/.squad-board/<herdr session>/<board id>.json` when the lead opens its first worker tab:

```json
{"name": "PROJ-1 Checkout", "root": "/abs/path/to/project", "tabs": ["W·Checkout", "W·E2E"]}
```

`tabs` matches a label exactly or with a `-N` suffix (`W·E2E` claims `W·E2E-2`). Everything else is optional; see `board.example.json`.

Development progress: give a plan a `progress` file (or `parts`, one plan/progress pair per split) and the board matches `### Task <id>: <title>` headings in the plan against `Task <id> ✔ <commit> <note>` lines in the progress file. `Task 9/10/11 ✔` counts all three; `Task 7 — <reason>` counts as skipped. A phase with `"auto": "impl"` shows the total `done/total Task`, `"auto": "plans"` shows how many plans are done.

## Recording a dispatch (per prompt)

Each time the lead sends a worker a task, append one line:

```bash
printf '{"worker":"%s","brief":"%s","report":"%s","at":%s}\n' \
  proj1-qa-g1 /abs/brief-G1.md /abs/G1/REPORT.md "$(date +%s)" \
  >> ~/.squad-board/"$HERDR_SESSION"/<board id>.dispatch.jsonl
```

The last line per worker wins. A report older than its dispatch shows as "not updated", and becomes a "needs you" item once the worker goes idle.

## Legacy adapter

`sources.legacy_report_dir` points to a directory containing a `build_report.py` that exposes `board_rows()`; the board imports that file to fill the acceptance table. Because it is imported and executed, only point it at code you trust. It exists until a structured `results.json` contract replaces it.

## Files

| Path | Content |
|---|---|
| `~/.squad-board/<session>/<id>.json` | board declaration |
| `~/.squad-board/<session>/<id>.dispatch.jsonl` | dispatch log |
| `~/.squad-board/.state/` | event history written by the board |
| `~/.squad-board/server.log` | launchd log |

## Check

```bash
python3 test_board.py
```
