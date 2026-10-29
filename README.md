# squad-board

A local, zero-dependency live board for multi-agent squads running in [herdr](https://herdr.dev).

One process watches every running herdr session and shows, per lead: worker status, what each worker is doing, whether its report is up to date, and a "needs you" list (blocked workers, stale reports, vanished workers, pending questions). Optional panels show acceptance progress, issues and sub-plans when the lead declares data sources.

- Python 3 standard library only, no install step
- Reads herdr through its CLI and reads files; never prompts workers or writes to the project. Its only actions — **⤢ focus** a pane, **reply** to a lead that is waiting on you, **archive** / **restore** a board under `~/.squad-board` — are accepted from loopback only
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

## Story spaces and conventions (no configuration)

If each task lives in its own directory under a workspace (`<workspace>/<story>/`), list the workspace in `~/.squad-board/config.json`:

```json
{"workspaces": ["~/work"]}
```

A worker whose working directory is inside `<workspace>/<story>/` belongs to that story: it joins the declared board whose `root` is in the same story, or else an auto board rooted at the story. Inside a board's `root` the board finds, without any `sources` entry:

| File | Shows |
|---|---|
| `docs/AC.md` (or `AC.md`) | AC table. Columns are matched by header name; only `AC` and a status column (`最终状态` / `状态` / `结论` / `status` / `result`) are required. `内容` / `描述` is shown as the AC text; screenshot paths in `证据` become evidence. Without a card column the card is the digits in the board name. Columns that are empty or identical on every row are hidden |
| `docs/plans/*.md` with `### Task` headings, progress in `docs/exec/STATUS-<id>.md` | Plan and task progress |
| otherwise `docs/brief-X.md` + `docs/report-X.md` pairs | One work item per pair, status from the report's `STATUS:` line |
| `demos/*.mp4` / `.webm` / `.mov` | MVP demo panel |
| `docs/CLOSEOUT.md` (or `CLOSEOUT.md`) | Closeout panel |

Without `statuses`, built-in ones apply: PASS / 部分通过 / FAIL / 待复测 / 阻塞 / 不适用 / 不可测 / 未执行 / 其他, matched in Chinese and English.

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

Point `sources.demos` at where you drop screen recordings, e.g. `["demos/*.mp4", "demos/*.webm", "demos/*.mov"]`. Videos show up in an **MVP 演示** panel within seconds, newest first, and play in place with seeking (files are streamed with HTTP Range). A file whose name starts with a card code or plan id (`10001-void-all.mp4`, `F1_dispute.mov`) is tagged and gets a **▶ 演示** badge on that card tile and plan. A `<video>.chapters.json` next to a video (`[{"t": seconds, "label": "AC1 …", "result": "PASS"}]`) is listed under it; clicking a chapter seeks there. Live updates never reload a playing video. Archived snapshots keep the list and link the original files.

## Closeout

Point `sources.closeout` at the task's `CLOSEOUT.md`: first line `STATUS: in-progress | done`, `- [x]` / `- [ ]` checklist items, and `## ` sections holding Markdown tables (e.g. deleted / kept / undecided). The board shows a **收尾** panel with checklist progress and the tables, logs status changes in the feed, and a phase `{"label": "收尾", "auto": "closeout"}` turns green once it is done. Archived boards keep the closeout state.

## Waiting on you

Leads (nameless Claude Code agents outside `W·` tabs) that stopped to ask you something are listed first under **需要你**, with their question, the options they listed, the `※ recap` line and the grey suggested reply from their input box. The screen is read with `herdr pane read --source visible --ansi` and parsed by `decisions.py`; it is re-read only when the agent's state changes or every 30 s. A free-text question can be answered in place: **采纳建议** sends the suggested reply, **回复…** sends your own text (typed into the lead's input box and submitted). The reply is refused when the input box already holds text you typed but did not send, and selection or permission prompts must still be answered in the session (**⤢ 定位**). Other reminders (stale reports, idle workers) fold under **其他提醒**.

## Archiving a board

When a squad is done, press **归档** on its board (or `curl -X POST 127.0.0.1:8790/api/archive -d '{"session":"<herdr session>","board":"<board id>"}'`). The board's current state — AC rows, issues, plans and tasks, last worker status, the full event log — is saved under `~/.squad-board/.archive/<session>/<board>-<time>/`, and its declaration, dispatch log and events move there too, so it leaves the overview. Archived boards are listed at the bottom of the overview and open as a read-only snapshot; **恢复** moves everything back. Evidence links still point at the original files under the board's `root`. Auto boards of a story space can be archived too: they stay hidden until files in the story change again, and restore just drops the snapshot. Archive and restore, like focus, accept loopback requests only.

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
