# squad-board

A local, zero-dependency live board for multi-agent squads running in [herdr](https://herdr.dev).

One process watches every running herdr session and shows, per lead: worker status, what each worker is doing, whether its report is up to date, and a "needs you" list (blocked workers, stale reports, vanished workers, pending questions). Optional panels show acceptance progress, issues and sub-plans when the lead declares data sources.

- Python 3 standard library only, no install step
- Reads herdr through its CLI and reads files; never prompts workers or writes to the project. Its only actions — **⤢ focus** a worker pane, **archive** / **restore** a board under `~/.squad-board` — are accepted from loopback only
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

AC `statuses` take optional flags: `done` (verdict reached), `pass` (counts toward the pass rate), `na` (concluded but untestable/not applicable, left out of the pass-rate denominator), `exclude` (out of scope, hidden from counts). The ring shows pass rate = `pass` / (non-`exclude`, non-`na`) rows; with no `pass` status it falls back to the `done` share.

Development progress: give a plan a `progress` file (or `parts`, one plan/progress pair per split) and the board matches `### Task <id>: <title>` headings in the plan against `Task <id> ✔ <commit> <note>` lines in the progress file. `Task 9/10/11 ✔` counts all three; `Task 7 — <reason>` counts as skipped. A phase with `"auto": "impl"` shows the total `done/total Task`, `"auto": "plans"` shows how many plans are done.

## Recording a dispatch (per prompt)

Each time the lead sends a worker a task, append one line:

```bash
printf '{"worker":"%s","brief":"%s","report":"%s","at":%s}\n' \
  proj1-qa-g1 /abs/brief-G1.md /abs/G1/REPORT.md "$(date +%s)" \
  >> ~/.squad-board/"$HERDR_SESSION"/<board id>.dispatch.jsonl
```

The last line per worker wins. A report older than its dispatch shows as "not updated", and becomes a "needs you" item once the worker goes idle.

## AC table

`sources.ac_table` reads acceptance rows from a Markdown table:

```json
"ac_table": {"file": "acceptance/BOARD.md",
             "sections": {"## Rows (in scope)": null, "## Rows (not built)": "NOT_BUILT"},
             "empty": "PENDING", "other": "OTHER"}
```

Only rows under the listed `## ` headings count; a section mapped to a status id forces that status. A row is `| card | ac | cat | how | qa | qam | issue | final |` (override with `columns`, key regex with `key`); a bold first cell such as `| **G1** |` starts a group. The `final` text picks the first status, in `statuses` order, whose `prefix` it starts with or whose `match` it contains; empty text maps to `empty`, anything unmatched to `other`. Screenshot paths (`…/shots/….png`, globs allowed) in any cell are shown as evidence.

## MVP demos

Point `sources.demos` at where you drop screen recordings, e.g. `["demos/*.mp4", "demos/*.webm", "demos/*.mov"]`. Videos show up in an **MVP 演示** panel within seconds, newest first, and play in place with seeking (files are streamed with HTTP Range). A file whose name starts with a card code or plan id (`29217-void-all.mp4`, `F1_dispute.mov`) is tagged and gets a **▶ 演示** badge on that card tile and plan. A `<video>.chapters.json` next to a video (`[{"t": seconds, "label": "AC1 …", "result": "PASS"}]`) is listed under it; clicking a chapter seeks there. Live updates never reload a playing video. Archived snapshots keep the list and link the original files.

## Closeout

Point `sources.closeout` at the task's `CLOSEOUT.md`: first line `STATUS: in-progress | done`, `- [x]` / `- [ ]` checklist items, and `## ` sections holding Markdown tables (e.g. deleted / kept / undecided). The board shows a **收尾** panel with checklist progress and the tables, logs status changes in the feed, and a phase `{"label": "收尾", "auto": "closeout"}` turns green once it is done. Archived boards keep the closeout state.

## Archiving a board

When a squad is done, press **归档** on its board (or `curl -X POST 127.0.0.1:8790/api/archive -d '{"session":"<herdr session>","board":"<board id>"}'`). The board's current state — AC rows, issues, plans and tasks, last worker status, the full event log — is saved under `~/.squad-board/.archive/<session>/<board>-<time>/`, and its declaration, dispatch log and events move there too, so it leaves the overview. Archived boards are listed at the bottom of the overview and open as a read-only snapshot; **恢复** moves everything back. Evidence links still point at the original files under the board's `root`. Only declared boards can be archived; archive and restore, like focus, accept loopback requests only.

## Files

| Path | Content |
|---|---|
| `~/.squad-board/<session>/<id>.json` | board declaration |
| `~/.squad-board/<session>/<id>.dispatch.jsonl` | dispatch log |
| `~/.squad-board/.state/` | event history written by the board |
| `~/.squad-board/.archive/` | archived board snapshots |
| `~/.squad-board/server.log` | launchd log |

## Check

```bash
python3 test_board.py
```

## License

MIT, see [LICENSE](LICENSE).
