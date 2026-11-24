"""`agentflow backlog import`: the first `backlog.yml`, written from a repository's current work source.

`build` reads a `capability_map` or `specs` pack and the Board's local form cards and returns the items, in the order
the file keeps them, and the notes the person running it should read. `write` creates the file (never over an
existing one) and moves `runs/done_marks.json` aside, so a leftover hand mark can't say done for an item the file
says is `ready`. Neither commits anything, and neither edits `.agentflow/config.yaml`: the command prints the
`work_source` line to switch to.

Ordering: the source's open items (row order for a capability map, dependency order for `specs`), then the open
items of spec files no source item covers (orphans), then the form cards, then every `done` item alphabetically.
"""

from __future__ import annotations

import fcntl
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from agentflow import backlog, backlog_file, finished, module_queue, safeio, sources, work_source
from agentflow.backlog_file import BacklogItem
from agentflow.state import runs_root

if TYPE_CHECKING:
    from agentflow.config import Config

IMPORTED = finished.MARKS + ".imported"
SWITCH = "work_source: {{kind: backlog, path: {path}}}"
_BUSY = ("note: another `agentflow queue` holds the queue lock, so the assessment was not refreshed: done evidence "
         "is what the last assessment recorded")


def _one_line(text: str, limit: int = backlog_file.TITLE_MAX) -> str:
    return " ".join(text.split())[:limit].rstrip()


def _plain(repo: Path, rel: str) -> Path | None:
    """`repo / rel` when every part below `repo` is a real directory and the last is a regular file, else None."""
    at = Path(repo)
    parts = PurePosixPath(rel).parts
    for i, part in enumerate(parts):
        at = at / part
        try:
            mode = os.lstat(at).st_mode
        except OSError:
            return None
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) if i == len(parts) - 1 else stat.S_ISDIR(mode)):
            return None
    return at


def _heading(repo: Path, rel: str) -> str:
    """The spec's first `# ` heading as one line, or ''."""
    path = _plain(repo, rel)
    if path is None:
        return ""
    try:
        header = sources.read_header(path)
    except OSError:
        return ""
    for line, _ in sources.spec_header(header):
        if line.startswith("# "):
            return _one_line(line[2:])
    return ""


def _specs_rel(cfg: Config) -> str:
    raw = cfg.project.docs.get("specs")
    return PurePosixPath(raw).as_posix() if isinstance(raw, str) and raw.strip() else ""


class _Plans:
    """The plans under `project.docs.plans`: per spec file name (what the plan's `**Spec:**` names), its plans in
    name order (the newest is the last) and whether each is fully ticked. Symlinks are never followed."""

    def __init__(self, cfg: Config, repo: Path) -> None:
        self.by_spec: dict[str, list[tuple[str, bool]]] = {}
        raw = cfg.project.docs.get("plans")
        if not isinstance(raw, str) or not raw.strip() or Path(raw).is_absolute():
            return
        rel = PurePosixPath(raw).as_posix()
        folder = Path(repo)
        for part in PurePosixPath(rel).parts:
            folder = folder / part
            if folder.is_symlink():
                return
        if not folder.is_dir():
            return
        try:
            names = sorted(os.listdir(folder))[: sources.SPECS_MAX]
        except OSError:
            return
        for name in names:
            if not name.endswith(".md"):
                continue
            path = f"{rel}/{name}"
            full = _plain(repo, path)
            if full is None:
                continue
            try:
                text = sources.read_header(full, module_queue.PLAN_READ_MAX)
            except OSError:
                continue
            spec = sources.plan_spec(text)
            if spec is None:
                continue
            tasks = sources.parse_plan_text(text)
            self.by_spec.setdefault(spec, []).append((path, bool(tasks) and not any(t.open for t in tasks)))

    def newest(self, spec_name: str) -> str:
        found = self.by_spec.get(spec_name)
        return found[-1][0] if found else ""

    def all_finished(self, spec_name: str) -> bool:
        found = self.by_spec.get(spec_name)
        return bool(found) and all(done for _, done in found)


def _order(open_nodes: list[sources.ModuleNode], done: set[str], by_levels: bool) -> tuple[list[sources.ModuleNode], bool]:
    """`open_nodes` as they come, or (by_levels) each after the open ones it depends on, alphabetical within a
    level; the flag is True when a dependency cycle left some of them for the end."""
    if not by_levels:
        return list(open_nodes), False
    ids = {n.id for n in open_nodes}
    placed: set[str] = set()
    out: list[sources.ModuleNode] = []
    rest = sorted(open_nodes, key=lambda n: n.id)
    while rest:
        level = [n for n in rest if all(d in placed or d in done or d not in ids or d == n.id for d in n.deps)]
        if not level:
            return out + rest, True
        out.extend(level)
        placed.update(n.id for n in level)
        rest = [n for n in rest if n.id not in placed]
    return out, False


def _usable_dep(dep: str) -> bool:
    return len(dep) <= backlog_file.ID_MAX and sources.MODULE_ID.fullmatch(dep) is not None


def _orphan_id(cfg: Config, name: str, taken: set[str]) -> tuple[str, str]:
    """`(id, note)` for a spec file no item covers: its `{module}` under `project.docs.spec_file` when the name fits,
    else its lowercased slug; `-2`, `-3`... when taken. `id` is '' when no id can be made."""
    fit = next(iter(sources.spec_ids([name], cfg.project.spec_file)), "")
    base = fit if fit and sources.MODULE_ID.fullmatch(fit) else ""
    if not base:
        base = re.sub(r"[^a-z0-9_-]+", "-", PurePosixPath(name).stem.lower()).strip("-_")
    base = base[: backlog_file.ID_MAX].rstrip("-_")
    if not base or sources.MODULE_ID.fullmatch(base) is None:
        return "", f"note: {name}: no item id can be made from the file name; add it with `backlog add --spec` if wanted"
    ident, n = base, 2
    while ident in taken:
        suffix = f"-{n}"
        ident = base[: backlog_file.ID_MAX - len(suffix)].rstrip("-_") + suffix
        n += 1
    note = f"note: {name}: id {ident} (another item or file already used {base})" if ident != base else ""
    return ident, note


def _done_marks_present(repo: Path) -> bool:
    return os.path.lexists(runs_root(repo) / finished.MARKS)


def _done_evidence(cfg: Config, repo: Path, notes: list[str]) -> dict[str, str]:
    """`{module: reason}` for what the queue's last assessment (refreshed here when the queue is not running) and the
    hand marks say is done."""
    try:
        with module_queue.queue_lock(repo):
            module_queue.assess_modules(cfg, repo)
    except module_queue.QueueBusy:
        notes.append(_BUSY)
    return finished.recorded(repo, hand_marks=True)


def _cards(book: backlog.Backlog, taken: set[str], notes: list[str]) -> list[BacklogItem]:
    """The form cards that carry a new module become `backlog` items; every other card is left out and named."""
    found: list[BacklogItem] = []
    for card in book.cards():
        new = card.new_module if isinstance(card.new_module, dict) else None
        module = str(new.get("module") or "") if new else ""
        if new is None:
            notes.append(f"note: card {card.id} is left out: it has no new module, so it is not a backlog item "
                         "(start it from the Board as before)")
        elif card.status in ("starting", "started"):
            notes.append(f"note: card {card.id} ({module}) is left out: it is {card.status}"
                         + (f" as run {card.run_id}" if card.run_id else ""))
        elif not module or not _usable_dep(module):
            notes.append(f"note: card {card.id} is left out: {module!r} is not an item id")
        elif module in taken:
            notes.append(f"note: card {card.id} is left out: {module} is already an item")
        else:
            taken.add(module)
            deps = [d for d in (new.get("deps") or []) if isinstance(d, str) and _usable_dep(d)]
            found.append(BacklogItem(module, deps, title=_one_line(card.label) or module, status="backlog"))
    return found


def build(cfg: Config, repo: Path) -> tuple[list[BacklogItem], list[str]]:
    """The items of the first backlog file and the notes for the user. `ValueError` when the pack's work source is not
    `capability_map` or `specs`. It refreshes the queue's assessment, which is the only thing it writes."""
    repo = Path(repo)
    kind = work_source.kind(cfg)
    if kind == "backlog":
        raise ValueError("work_source is already backlog: there is nothing to import")
    if kind not in ("capability_map", "specs"):
        raise ValueError("backlog import reads a capability_map or specs work source, and this pack has "
                         + ("none" if kind is None else f"kind {kind!r}"))
    notes: list[str] = []
    nodes: list[sources.ModuleNode] = []
    seen: set[str] = set()
    for node in work_source.items(cfg, repo):
        if node.id in seen:
            notes.append(f"note: {node.id} is listed twice in the {work_source.noun(cfg)}: the first row is kept")
        elif not _usable_dep(node.id):
            notes.append(f"note: {node.id} is left out: not an item id (a lowercase slug of at most "
                         f"{backlog_file.ID_MAX} characters)")
        else:
            seen.add(node.id)
            nodes.append(node)
    recorded = _done_evidence(cfg, repo, notes)
    plans = _Plans(cfg, repo)
    specs_rel = _specs_rel(cfg)
    responsibilities: dict[str, str] = {}
    path = work_source.file(cfg, repo)
    if kind == "capability_map" and path is not None and path.is_file():
        responsibilities = sources.capability_responsibilities(path)

    def spec_of(module: str) -> tuple[str, str]:
        name = cfg.project.spec_file.format(module=module)
        rel = f"{specs_rel}/{name}" if specs_rel else name
        return (rel, name) if _plain(repo, rel) is not None else ("", name)

    covered: set[str] = {PurePosixPath(cfg.work_source.path).as_posix()} if kind == "capability_map" else set()
    modules: list[BacklogItem] = []
    for node in nodes:
        spec, name = spec_of(node.id)
        covered.add(spec)
        title = _one_line(responsibilities.get(node.id, "").split("\n")[0])
        modules.append(BacklogItem(
            node.id, [d for d in node.deps if _usable_dep(d)],
            title=title or (_heading(repo, spec) if spec else "") or node.id,
            status="done" if node.id in recorded else "ready", spec=spec, plan=plans.newest(name)))
        for dep in node.deps:
            if not _usable_dep(dep):
                notes.append(f"note: {node.id}: dependency {dep[:60]!r} is not an item id and is left out")
    known = {i.id for i in modules}
    for item in modules:
        for dep in item.deps:
            if dep not in known:
                notes.append(f"note: {item.id} depends on {dep}, which is not an item: add it, or edit the file")
    orphans: list[BacklogItem] = []
    taken = set(known)
    for rel in work_source.unlisted_specs(cfg, repo, covered):
        name = PurePosixPath(rel).name
        if PurePosixPath(name).stem.lower() in sources.NOT_MODULES:
            notes.append(f"note: {rel} is left out: it is not a spec")
            continue
        ident, note = _orphan_id(cfg, name, taken)
        if note:
            notes.append(note)
        if not ident:
            continue
        taken.add(ident)
        path = _plain(repo, rel)
        try:
            says = path is not None and bool(sources.says_done(sources.read_header(path), cfg.project.done_status))
        except OSError:
            says = False
        finished_ = says or plans.all_finished(name)
        orphans.append(BacklogItem(ident, [], title=_heading(repo, rel) or PurePosixPath(rel).stem, status="done" if finished_ else "ready",
                                   spec=rel, plan=plans.newest(name)))
    if orphans:
        notes.append(f"note: {len(orphans)} spec file(s) that no module of the {work_source.noun(cfg)} covers became items "
                     "(status ready, or done when the spec or its plans say so); drop the ones that are not work")
    cards = _cards(backlog.Backlog.load(repo), taken, notes)
    open_nodes = [m for m in modules if m.status != "done"]
    ordered, cycle = _order(open_nodes, {m.id for m in modules if m.status == "done"}, kind == "specs")
    if cycle:
        notes.append("note: some items depend on each other in a cycle: they are listed last, in id order")
    by_id = {m.id: m for m in modules}
    out: list[BacklogItem] = [by_id[n.id] for n in ordered]
    out += [o for o in orphans if o.status != "done"]
    out += cards
    out += sorted((i for i in [*modules, *orphans] if i.status == "done"), key=lambda i: i.id)
    if _done_marks_present(repo):
        notes.append(f"note: runs/{finished.MARKS} is read in: its marks count as done evidence above, and a real "
                     f"import moves it to runs/{IMPORTED}")
    problem = _marks_problem(repo)
    if problem:
        notes.append(f"note: a real import would be refused: {problem}")
    _, problems = backlog_file.parse(backlog_file.render(out))
    notes += [f"note: the file would carry a {p.severity}: {p.where}: {p.message}" for p in problems]
    return out, notes


def _move_marks(repo: Path) -> bool:
    """Move `runs/done_marks.json` to `runs/done_marks.json.imported` under the marks' own lock; whether there was one.
    `ValueError` (nothing moved) when the target exists, or the marks file is not a regular file."""
    root = runs_root(repo)
    source, target = root / finished.MARKS, root / IMPORTED
    if not os.path.lexists(source):
        return False
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / finished.LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.path.lexists(target):
            raise ValueError(f"{target} already exists: move it away first, so the hand marks are not lost")
        if not stat.S_ISREG(os.lstat(source).st_mode):
            raise ValueError(f"{source} is not a regular file: move it away by hand")
        os.rename(source, target)
    finally:
        os.close(fd)
    return True


def _marks_problem(repo: Path) -> str | None:
    """Why `_move_marks` would fail, from a look at the files; None when there are no marks to move or it can go."""
    root = runs_root(repo)
    source, target, lock = root / finished.MARKS, root / IMPORTED, root / finished.LOCK
    try:
        if not os.path.lexists(source):
            return None
        if not stat.S_ISREG(os.lstat(source).st_mode):
            return f"{source} is not a regular file: move it away by hand"
        if os.path.lexists(target):
            return f"{target} already exists: move it away first, so the hand marks are not lost"
        if os.path.lexists(lock) and stat.S_ISLNK(os.lstat(lock).st_mode):
            return f"{lock} is a symlink: remove it first"
    except OSError as exc:
        return f"cannot look at {source}: {exc.strerror or type(exc).__name__}"
    return None


def preflight(repo: Path) -> None:
    """Refuse (`ValueError`), before anything is written, what moving the marks aside would refuse after the file is
    written: a marks file that is not a regular file, an existing `.imported`, a symlinked lock file."""
    problem = _marks_problem(Path(repo))
    if problem:
        raise ValueError(problem)


class MarksNotMoved(Exception):
    """`backlog.yml` was written, but `runs/done_marks.json` could not be moved aside."""

    def __init__(self, path: Path, why: str) -> None:
        super().__init__(f"wrote {path}, but could not move runs/{finished.MARKS}: {why}")
        self.path = path
        self.why = why


def _create(repo: Path, items: list[BacklogItem]) -> tuple[Path, bool]:
    """`(the file written, whether the marks were moved)`. `MarksNotMoved` once the file is written but the marks stay."""
    preflight(repo)
    path = safeio.write_new(repo / backlog_file.FILE, backlog_file.render(items))
    try:
        return path, _move_marks(repo)
    except (OSError, ValueError) as exc:
        raise MarksNotMoved(path, str(exc)) from exc


def write(cfg: Config, repo: Path, items: list[BacklogItem]) -> Path:
    """Create `<repo>/backlog.yml` from `items` (`FileExistsError` when anything is there) and move the hand marks
    aside (`MarksNotMoved`, after the file is written, when that fails). Nothing is committed, and the pack's config
    is not changed."""
    return _create(Path(repo), items)[0]


def run(cfg: Config, repo: Path, dry_run: bool) -> tuple[list[str], list[str], int]:
    """`(stdout lines, stderr lines, exit code)` of `agentflow backlog import`: a dry run prints the file's text and the
    notes (apart, so the text can be redirected into the file) and writes nothing; a real run writes the file and
    prints where it went, the notes and the `work_source` line to switch to. When the file is written but the hand
    marks could not be moved aside, it prints all of that and says why, and the code is 1."""
    repo = Path(repo)
    items, notes = build(cfg, repo)
    if dry_run:
        return backlog_file.render(items).splitlines(), notes, 0
    switch = SWITCH.format(path=backlog_file.FILE)
    try:
        path, moved = _create(repo, items)
    except FileExistsError:
        raise ValueError(f"{repo / backlog_file.FILE} already exists: nothing was written (move it away to import again)") from None
    except MarksNotMoved as exc:
        return [f"wrote {exc.path} ({len(items)} items; not committed)", *notes,
                f"error: could not move runs/{finished.MARKS} to runs/{IMPORTED}: {exc.why}",
                f"the file is written and a backlog pack ignores the marks: once you move runs/{finished.MARKS} away by "
                "hand, nothing is left to do", switch], [], 1
    done = [f"moved runs/{finished.MARKS} to runs/{IMPORTED}"] if moved else []
    return [f"wrote {path} ({len(items)} items; not committed)", *notes, *done, switch], [], 0
