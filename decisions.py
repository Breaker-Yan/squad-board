import re

ESCAPE = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.")
RULE = re.compile(r"\s*─{3,}")
TABLE_ROW = re.compile(r"\s*[┌├└│┬┴┼┐┤┘]")
LIST_ITEM = re.compile(r"^(\s*)([-*•]|\d+[.、)])\s+(.*)")
SELECT_OPTION = re.compile(r"^\s*(?:❯\s*)?(\d+)[.)]\s+(.+)")
PROMPT_CHROME = re.compile(r"^\s*(?:╌{3,}|Tip:)")
RECAP_LINE = re.compile(r"\s*※\s*recap:?\s*(.*)")
TURN_END_LINE = re.compile(r"\bfor \d+\s*[smh]\b.*·\s*done")
DONE_AT = re.compile(r"·\s*done\s+(\d{1,2}:\d{2}(?:\s?[AP]M)?)")
NEW_TASK_HINT = re.compile(r"new task\?.*/clear")
TITLE = re.compile(r"Things only you can decide|(?:需要|要|待|等)你(?:来)?(?:决定|拍板)", re.I)
ASK = re.compile(
    r"[？]|\?(?:\s|$)|要不要|是否要|要我[^。！\n]{0,30}吗"
    r"|(?:请|需要|要|等|待|由)你(?:来|先|一并|再)?(?:决定|确认|判断|拍板|定|选|回复|回答)"
    r"|你来(?:决定|判断|拍板)|should i\b|do you want|would you like",
    re.I,
)
APPROVAL = re.compile(
    r"don't ask again|Do you want to (?:proceed|make|create|allow|run|edit|delete|overwrite)|Allow this|允许|批准|授权",
    re.I,
)
QUESTION_LIMIT = 400
SHORT_QUESTION_LIMIT = 60


def _cells(line):
    out, dim, reverse, pos = [], False, False, 0
    for match in ESCAPE.finditer(line):
        out += [(char, dim, reverse) for char in line[pos:match.start()]]
        pos = match.end()
        sequence = match.group(0)
        if sequence.startswith("\x1b[") and sequence.endswith("m"):
            dim, reverse = _after_sgr(sequence[2:-1], dim, reverse)
    out += [(char, dim, reverse) for char in line[pos:]]
    return out


def _after_sgr(params, dim, reverse):
    codes = [int(part) if part.isdigit() else 0 for part in params.replace(":", ";").split(";")]
    index = 0
    while index < len(codes):
        code = codes[index]
        if code == 0:
            dim = reverse = False
        elif code == 22:
            dim = False
        elif code == 2:
            dim = True
        elif code == 7:
            reverse = True
        elif code == 27:
            reverse = False
        elif code in (38, 48):
            index += 2 if index + 1 < len(codes) and codes[index + 1] == 5 else 4
        index += 1
    return dim, reverse


def _plain(line):
    return "".join(char for char, _, _ in _cells(line)).replace("\xa0", " ").rstrip()


def _plain_lines(ansi):
    return [_plain(line) for line in ansi.replace("\r", "").split("\n")]


def _find_input_box(plain):
    for index in range(len(plain) - 1, 0, -1):
        line = plain[index]
        if line.startswith("❯") and not SELECT_OPTION.match(line) and RULE.match(plain[index - 1]):
            for bottom in range(index + 1, len(plain)):
                if RULE.match(plain[bottom]):
                    return index - 1, index, bottom
            return index - 1, index, len(plain)
    return None


def _read_input(ansi):
    raw = ansi.replace("\r", "").split("\n")
    box = _find_input_box([_plain(line) for line in raw])
    if not box:
        return "unknown", ""
    _, first, bottom = box
    cells = []
    for number, line in enumerate(raw[first:bottom]):
        line_cells = [(c.replace("\xa0", " "), d, r) for c, d, r in _cells(line)]
        if number == 0:
            line_cells = line_cells[line_cells.index(next(cell for cell in line_cells if cell[0] == "❯")) + 1:]
        cells += line_cells + [("\n", False, False)]
    visible = [cell for cell in cells if not cell[0].isspace()]
    if not visible:
        return "empty", ""
    if any(not dim and not reverse for _, dim, reverse in visible):
        return "typed", ""
    return "suggestion", "".join(char for char, _, _ in cells).strip()


def input_state(ansi):
    return _read_input(ansi)[0]


def _join_wrapped(parts):
    text = ""
    for part in parts:
        if text and not (_is_cjk(text[-1]) or _is_cjk(part[0])):
            text += " "
        text += part
    return text


def _is_cjk(char):
    return ord(char) > 0x2E80


def _clip_tail(text):
    return text if len(text) <= QUESTION_LIMIT else "…" + text[-QUESTION_LIMIT:]


def _clip_head(text):
    return text if len(text) <= QUESTION_LIMIT else text[:QUESTION_LIMIT] + "…"


def _read_recap(body):
    for index in range(len(body) - 1, -1, -1):
        match = RECAP_LINE.match(body[index])
        if match:
            parts, end = [match.group(1).strip()], index + 1
            while end < len(body) and body[end].strip() and not RULE.match(body[end]) and not NEW_TASK_HINT.search(body[end]):
                parts.append(body[end].strip())
                end += 1
            return _join_wrapped([part for part in parts if part]), set(range(index, end))
    return "", set()


def _blocks(lines):
    blocks, current = [], []
    for line in lines:
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _indent(line):
    return len(line) - len(line.lstrip())


def _split_block(lines):
    lead, items, tail, marker_indent = [], [], [], 0
    for line in lines:
        match = LIST_ITEM.match(line)
        if match:
            marker_indent = len(match.group(1))
            items.append([match.group(2), match.group(3).strip()])
        elif items and not tail and _indent(line) > marker_indent:
            items[-1][1] = _join_wrapped([items[-1][1], line.strip()])
        elif items:
            tail.append(line.strip())
        else:
            lead.append(line.strip())
    return lead, items, tail


def _read_blocked(plain):
    last = next((i for i in range(len(plain) - 1, -1, -1) if SELECT_OPTION.match(plain[i])), None)
    if last is None:
        return None
    options, index = [], last
    while index >= 0:
        match = SELECT_OPTION.match(plain[index])
        if match:
            options.insert(0, match.group(2).strip())
            if match.group(1) == "1":
                break
        index -= 1
    if index < 0:
        return None
    above = index - 1
    while above >= 0 and not plain[above].strip():
        above -= 1
    question = []
    while above >= 0 and plain[above].strip() and not RULE.match(plain[above]):
        if not PROMPT_CHROME.match(plain[above]):
            question.insert(0, plain[above].strip())
        above -= 1
    question_text = _clip_tail(_join_wrapped(question))
    kind = "approval" if APPROVAL.search(question_text + " " + " ".join(options)) else "choice"
    return kind, question_text, options


def _read_decision(body, recap):
    blocks = _blocks(body)
    if blocks:
        lead, items, tail = _split_block(blocks[-1])
        if not lead and items and len(blocks) > 1:
            lead = [line.strip() for line in blocks[-2]]
        head = _join_wrapped(lead)
        trailing = _join_wrapped(tail)
        numbered = bool(items) and items[0][0][0].isdigit()
        question_marks = sum(bool(re.search(r"[？?]", text)) for _, text in items)
        if len(items) >= 2 and (question_marks >= 2 or (numbered and TITLE.search(head))):
            lines = [head] + [f"{marker} {text}" for marker, text in items]
            return _clip_head("\n".join(line for line in lines if line)), []
        if head and (ASK.search(head + " " + trailing) or TITLE.search(head)):
            question = _join_wrapped([head, trailing] if trailing else [head])
            options = [text for _, text in items]
            if not items and len(question) <= SHORT_QUESTION_LIMIT and len(blocks) > 1:
                previous_lead, previous_items, _ = _split_block(blocks[-2])
                if not previous_lead:
                    options = [text for _, text in previous_items]
            return _clip_tail(question), options
    if recap and ASK.search(recap):
        return _clip_tail(recap), []
    return None


def parse_screen(ansi, status):
    if status == "working":
        return None
    plain = _plain_lines(ansi)
    box = _find_input_box(plain)
    body_lines = plain[:box[0]] if box else plain
    recap, recap_rows = _read_recap(body_lines)
    done_at = ""
    cleaned = []
    for number, line in enumerate(body_lines):
        if number in recap_rows or RULE.match(line) or NEW_TASK_HINT.search(line):
            continue
        if TURN_END_LINE.search(line):
            found = DONE_AT.search(line)
            done_at = found.group(1) if found else done_at
            continue
        cleaned.append("" if TABLE_ROW.match(line) else re.sub(r"^(\s*)⏺ ", r"\1  ", line))
    _, suggestion = _read_input(ansi)
    decision = None
    if status == "blocked":
        decision = _read_blocked(plain)
    if decision:
        kind, question, options = decision
    else:
        found = _read_decision(cleaned, recap)
        if not found:
            return None
        kind, (question, options) = "question", found
    return dict(kind=kind, question=question, options=options, suggestion=suggestion, recap=recap, done_at=done_at)
