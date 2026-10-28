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

## License

MIT, see [LICENSE](LICENSE).
