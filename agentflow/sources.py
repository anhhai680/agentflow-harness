"""Read the documents the workflow reasons about: plans, the capability map, spec files and spec status.

Plans follow the `writing-plans` shape: `### Task N: Title` headings, `- [ ]` / `- [x]` step
boxes and an optional `**Depends on:**` line (plan decision D12). Lines inside code fences
never count as headings or boxes, so a plan can quote examples of its own format.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MAX_TASK_STEPS = 8
MAX_TASK_FILES = 8

_TASK_HEADING = re.compile(r"^###\s+Task\s+(\d+)\s*:\s*(.*?)\s*$")
_TOP_HEADING = re.compile(r"^#{1,2}\s")
_BOX = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)\[( |x|X)\]")
_DEFERRED = re.compile(r"^\s*>\s*\*\*Deferred\b")
_DEPENDS = re.compile(r"^\*\*Depends on:\*\*\s*(.*?)\s*$")
_DEPENDS_TASK = re.compile(r"\bTask\s+(\d+)\b", re.IGNORECASE)
_FILE_LINE = re.compile(r"^\s*[-*+]\s+(?:Create|Modify|Test|Delete|Rename)\s*:", re.IGNORECASE)
_BACKTICKED = re.compile(r"`([^`]+)`")
_ROW_SPLIT = re.compile(r"(?<!\\)\|")


@dataclass
class PlanTask:
    n: int
    title: str
    text: str
    checked: bool
    depends_on: list[int]
    deferred: bool = False  # its body says `> **Deferred (...)**`: built later, once another module merges

    @property
    def open(self) -> bool:
        """Work still to do here: not every box is ticked and the plan has not deferred the task."""
        return not self.checked and not self.deferred


@dataclass
class ModuleNode:
    id: str
    deps: list[str]


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _fence_flags(lines: list[str]) -> list[bool]:
    """For each line, whether it is inside a fenced code block (the fence lines count as inside)."""
    flags: list[bool] = []
    inside = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flags.append(True)
            inside = not inside
        else:
            flags.append(inside)
    return flags


def _task_spans(lines: list[str]) -> list[tuple[int, str, int, int]]:
    """`(n, title, first_line, end_line)` for every task; `end_line` is exclusive."""
    fenced = _fence_flags(lines)
    starts: list[tuple[int, str, int]] = []
    for i, line in enumerate(lines):
        if fenced[i]:
            continue
        match = _TASK_HEADING.match(line)
        if match:
            starts.append((int(match.group(1)), match.group(2), i))
    spans = []
    for index, (n, title, start) in enumerate(starts):
        limit = starts[index + 1][2] if index + 1 < len(starts) else len(lines)
        end = next((j for j in range(start + 1, limit) if not fenced[j] and _TOP_HEADING.match(lines[j])), limit)
        spans.append((n, title, start, end))
    return spans


def _trim_trailing(lines: list[str]) -> list[str]:
    """Drop blank lines and `---` separators at the end of a task's text."""
    out = list(lines)
    while out and out[-1].strip() in ("", "---"):
        out.pop()
    return out


def _depends_on(n: int, body: list[str]) -> list[int]:
    for line in body:
        match = _DEPENDS.match(line.strip())
        if match:
            value = match.group(1)
            if re.fullmatch(r"none\.?", value.strip(), re.IGNORECASE):
                return []
            return [int(x) for x in _DEPENDS_TASK.findall(value)]
    return [n - 1] if n > 1 else []


def parse_plan(path: Path) -> list[PlanTask]:
    """Every `### Task N: Title` in the plan, in file order."""
    return parse_plan_text(_read(path))


def parse_plan_text(text: str) -> list[PlanTask]:
    """`parse_plan` for a plan's text (one read from a git ref rather than the work tree)."""
    lines = text.splitlines()
    fenced = _fence_flags(lines)
    tasks = []
    for n, title, start, end in _task_spans(lines):
        body = lines[start:end]
        boxes = [
            m.group(2) for k, line in enumerate(body) if not fenced[start + k] and (m := _BOX.match(line))
        ]
        tasks.append(
            PlanTask(
                n=n,
                title=title,
                text="\n".join(_trim_trailing(body)),
                checked=bool(boxes) and all(b in "xX" for b in boxes),
                depends_on=_depends_on(n, body[1:]),
                deferred=any(not fenced[start + k] and _DEFERRED.match(line) for k, line in enumerate(body)),
            )
        )
    return tasks


def tick_task(path: Path, n: int) -> None:
    """Flip every unchecked box of Task `n` to checked. Nothing else in the file changes."""
    text = _read(path)
    lines = text.splitlines(keepends=True)
    bare = [line.rstrip("\r\n") for line in lines]
    fenced = _fence_flags(bare)
    for task_n, _title, start, end in _task_spans(bare):
        if task_n != n:
            continue
        for i in range(start, end):
            if fenced[i]:
                continue
            match = _BOX.match(bare[i])
            if match and match.group(2) == " ":
                prefix = match.group(1)
                lines[i] = f"{prefix}[x]" + lines[i][match.end() :]
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("".join(lines))
        return
    raise ValueError(f"no Task {n} in {path}")


def task_size(task: PlanTask) -> tuple[int, int]:
    """`(checkbox steps, files named)` outside code fences."""
    lines = task.text.splitlines()
    fenced = _fence_flags(lines)
    steps = sum(1 for i, line in enumerate(lines) if not fenced[i] and _BOX.match(line))
    files: set[str] = set()
    for i, line in enumerate(lines):
        if not fenced[i] and _FILE_LINE.match(line):
            files.update(s for s in _BACKTICKED.findall(line) if "/" in s or "." in s)
    return steps, len(files)


def task_size_warnings(task: PlanTask) -> list[str]:
    """Reasons one session may be too small for this task (more than 8 steps or 8 files)."""
    steps, files = task_size(task)
    warnings = []
    if steps > MAX_TASK_STEPS:
        warnings.append(f"Task {task.n} has {steps} checkbox steps (more than {MAX_TASK_STEPS})")
    if files > MAX_TASK_FILES:
        warnings.append(f"Task {task.n} names {files} files (more than {MAX_TASK_FILES})")
    return warnings


def plan_batches(tasks: list[PlanTask], max_tasks: int, max_files: int) -> list[list[int]]:
    """Runs of neighbouring small tasks that can share one implement session and one review.

    A task is small when it is open (unticked, not deferred) and names at most `max_files` files (every task has the same
    checkbox steps, so files are the only measure). It joins the open batch when it comes next in plan
    order, the batch holds fewer than `max_tasks`, and each task it depends on is in the batch or before
    it, so running the batch in order never runs a task before its dependencies. Only batches of two or
    more are returned. Pure: the same plan always gives the same batches."""
    batches: list[list[int]] = []
    current: list[int] = []
    for task in tasks:
        small = task.open and task_size(task)[1] <= max_files
        if (small and current and len(current) < max_tasks
                and all(dep in current or dep < current[0] for dep in task.depends_on)):
            current.append(task.n)
            continue
        if len(current) > 1:
            batches.append(current)
        current = [task.n] if small else []
    if len(current) > 1:
        batches.append(current)
    return batches


def find_plan(plans_dir: Path, module: str) -> Path | None:
    """The newest `YYYY-MM-DD-<module>[-suffix].md` in `plans_dir`, or None."""
    plans_dir = Path(plans_dir)
    if not module or not plans_dir.is_dir():
        return None
    matches = sorted(p for p in plans_dir.iterdir() if p.is_file() and is_plan_name(p.name, module))
    return matches[-1] if matches else None


def is_plan_name(name: str, module: str) -> bool:
    """Whether `name` is one of `module`'s plan files, `YYYY-MM-DD-<module>[-suffix].md`."""
    return bool(module) and re.fullmatch(rf"\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(module)}(-.*)?\.md", name) is not None


def _cells(row: str) -> list[str]:
    inner = row.strip()
    inner = inner.removeprefix("|").removesuffix("|")
    return [c.strip() for c in _ROW_SPLIT.split(inner)]


def capability_map(path: Path) -> list[ModuleNode]:
    """The Modules table: one node per row, in row order. A dependency of `—` means none."""
    nodes: list[ModuleNode] = []
    id_col = deps_col = None
    in_table = False
    for line in _read(path).splitlines():
        if not line.lstrip().startswith("|"):
            if in_table and nodes:
                break
            continue
        cells = _cells(line)
        if id_col is None:
            lowered = [c.lower() for c in cells]
            if "module id" in lowered and "depends on" in lowered:
                id_col, deps_col = lowered.index("module id"), lowered.index("depends on")
                in_table = True
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        if len(cells) <= max(id_col, deps_col):
            continue
        module_id = cells[id_col].strip("` ")
        raw_deps = cells[deps_col].strip()
        deps = [] if raw_deps in ("", "—", "-", "–") else [d.strip("` ") for d in raw_deps.split(",") if d.strip("` ")]
        nodes.append(ModuleNode(module_id, deps))
    return nodes


def _cell(text: str) -> str:
    """`text` as one table cell: one line, with `|` escaped so it cannot start a new cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


def add_module_row(path: Path, template: dict[str, str], values: dict) -> None:
    """Insert one row after the last row of the table `capability_map` reads.

    `template` maps a column header to a cell template using `{module}`, `{responsibility}` and `{deps}`; a column
    with no template stays empty. Values are flattened to one line and have `|` escaped, so no text can break the
    table. Line endings and everything outside the new line stay as they are.
    """
    lines = _read(path).splitlines(keepends=True)
    start = headers = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|"):
            cells = _cells(line.rstrip("\r\n"))
            lowered = [c.lower() for c in cells]
            if "module id" in lowered and "depends on" in lowered:
                start, headers = i, cells
                break
    if start is None or headers is None:
        raise ValueError(f"{path} has no table with `Module id` and `Depends on` columns")
    last = start
    for j in range(start + 1, len(lines)):
        if not lines[j].lstrip().startswith("|"):
            break
        last = j
    id_col = [h.lower() for h in headers].index("module id")
    module = str(values["module"])
    for row in lines[start + 2 : last + 1]:
        cells = _cells(row.rstrip("\r\n"))
        if len(cells) > id_col and cells[id_col].strip("` ") == module:
            raise ValueError(f"module {module} is already in the table in {path.name}")
    deps = ", ".join(str(d) for d in values.get("deps") or []) or "\u2014"
    fill = {"module": module, "responsibility": str(values.get("responsibility") or ""), "deps": deps}
    by_header = {str(k).strip().lower(): str(v) for k, v in template.items()}
    cells = []
    for header in headers:
        cell = by_header.get(header.lower(), "")
        for name, value in fill.items():
            cell = cell.replace("{" + name + "}", _cell(value))
        cells.append(cell)
    eol = lines[start][len(lines[start].rstrip("\r\n")):] or "\n"
    row = "| " + " | ".join(cells) + " |"
    if lines[last].endswith(("\n", "\r")):
        lines.insert(last + 1, row + eol)
    else:  # the table is the last thing in a file with no final newline: keep it that way
        lines[last] += eol
        lines.insert(last + 1, row)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(lines))


def spec_modules(specs_dir: Path, spec_file: str) -> dict[str, Path]:
    """Module id -> spec file for every file in `specs_dir` whose name fits `spec_file` (`{module}` is the
    wildcard, `{{` and `}}` are literal braces, as `str.format` reads them)."""
    specs_dir = Path(specs_dir)
    if not specs_dir.is_dir():
        return {}
    parts = [re.escape(p.replace("{{", "{").replace("}}", "}")) for p in spec_file.split("{module}")]
    pattern = re.compile(parts[0] + "(?P<module>.+)" + "(?P=module)".join(parts[1:]))
    found = {}
    for path in sorted(specs_dir.iterdir()):
        match = pattern.fullmatch(path.name)
        if match and path.is_file():
            found[match.group("module")] = path
    return found


def spec_status(path: Path, approved_re: str, pending_re: str) -> Literal["approved", "pending", "unknown"]:
    """`approved` or `pending` by the last match in the file; within one line the later match wins."""
    approved, pending = re.compile(approved_re), re.compile(pending_re)
    status: Literal["approved", "pending", "unknown"] = "unknown"
    for line in _read(path).splitlines():
        last_approved = max((m.start() for m in approved.finditer(line)), default=-1)
        last_pending = max((m.start() for m in pending.finditer(line)), default=-1)
        if last_approved < 0 and last_pending < 0:
            continue
        status = "approved" if last_approved > last_pending else "pending"
    return status
