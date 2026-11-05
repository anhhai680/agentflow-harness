#!/usr/bin/env python3
"""Diff-scoped enforcement of the CONSTRAINTS.md floor.

Adapted from .claude/skills/constraint-driven-development/references/floor-guard.md:
same contract, Python and TypeScript patterns, standard library only so it runs before
the uv project exists and needs no Node.

Usage: python3 devtools/floor_guard.py [--base REF]   (default base: origin/main)
Exit:  0 clean, or approved by a human trailer; 1 floor violation; 2 could not run.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

CONSTRAINTS = "CONSTRAINTS.md"
SELF = "devtools/floor_guard.py"
APPROVAL_TRAILER = re.compile(r"^Constraints-Approved-By:\s*\S", re.MULTILINE)

CODE_FILE = re.compile(r"\.(py|pyi|ts|tsx|js|jsx|mjs|cjs)$")
WORKFLOW_FILE = re.compile(r"^\.github/workflows/.+\.ya?ml$")
TEST_FILE = re.compile(
    r"(^|/)(tests|e2e)/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$|\.(test|spec)\.[jt]sx?$"
)

# 1. Silenced checker.
SUPPRESSIONS = re.compile(
    r"#\s*noqa|#\s*type:\s*ignore|#\s*pyright:\s*(ignore|basic|standard|report\w+\s*=\s*false)"
    r"|pragma:\s*no\s*cover|@ts-ignore|@ts-expect-error|@ts-nocheck|biome-ignore|eslint-disable"
    r"|(istanbul|c8|v8) ignore|nosemgrep|gitleaks:allow"
)
# 4. Unfinished work. Multi-line `except: pass` and empty `catch {}` are caught by
# looking at the previous added line in the same file.
STUBS = re.compile(
    r"raise NotImplementedError|throw new Error\(.*[Nn]ot implemented|\bTODO\b|\bFIXME\b"
    r"|catch\s*(\(\w*\))?\s*\{\s*\}|except\b[^:]*:\s*(pass|\.\.\.)\s*$"
)
OPENS_EXCEPT = re.compile(r"^\s*except\b[^:]*:\s*$")
OPENS_CATCH = re.compile(r"catch\s*(\(\w*\))?\s*\{\s*$")
# 2. A test made easier.
SKIPS = re.compile(
    r"\.(skip|todo|only)\b|\bxit\(|\bxdescribe\(|pytest\.mark\.(skip|skipif|xfail)"
    r"|pytest\.(skip|xfail)\(|unittest\.skip"
)
ASSERTION = re.compile(r"\bassert\b|\bexpect\(|pytest\.raises|\.should\b|\btoThrow")
# The runner's own suite pins its behaviour, so a deliberate change rewrites assertions. Outside the
# assertion-removed check only; see the Floor section of CONSTRAINTS.md.
RUNNER_TESTS = "devtools/agentflow/tests/"
# A CI step softened.
SOFTENED_STEP = re.compile(r"continue-on-error:\s*true|\|\|\s*true\b")
STEP_LINE = re.compile(r"^\s*-?\s*(run|uses):")


def bail(message: str) -> None:
    print(f"floor-guard: {message}", file=sys.stderr)
    sys.exit(2)


def git(*args: str, diff_exit: bool = False) -> str | None:
    """Run git; None on failure. `git diff --no-index` exits 1 whenever the sides differ."""
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode == 0 or (diff_exit and result.returncode == 1):
        return result.stdout
    return None


def parse_args() -> str:
    if "--base" in sys.argv:
        i = sys.argv.index("--base")
        if i + 1 >= len(sys.argv):
            bail("--base needs a ref")
        return sys.argv[i + 1]
    return "origin/main"


def collect_diff(merge_base: str) -> str:
    tracked = git("diff", "-M", "--unified=0", merge_base, "--")
    if tracked is None:
        bail(f"could not diff against {merge_base}")
    listing = git("ls-files", "--others", "--exclude-standard")
    if listing is None:
        bail("could not list untracked files")
    parts = [tracked or ""]
    for f in filter(None, (listing or "").split("\n")):
        d = git("diff", "--no-index", "--unified=0", "/dev/null", f, diff_exit=True)
        if d is None:
            bail(f"could not diff untracked file {f}")
        parts.append(d or "")
    return "\n".join(parts)


def _path_of(header: str) -> str:
    return re.sub(r"^[ab]/", "", header)


def walk(diff: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[str]]:
    """Split a unified diff into added lines, removed lines, and deleted files.

    `---`/`+++` are headers only between a file's `diff` line and its first hunk.
    """
    added: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    deleted: list[str] = []
    file = old_file = ""
    in_header = False
    for line in diff.split("\n"):
        if line.startswith("diff "):
            in_header = True
        elif line.startswith("@@"):
            in_header = False
        elif in_header:
            if line.startswith("--- "):
                old_file = _path_of(line[4:])
            elif line.startswith("+++ "):
                new_file = _path_of(line[4:])
                file = old_file if new_file == "/dev/null" else new_file
                if new_file == "/dev/null":
                    deleted.append(file)
        elif line.startswith("+"):
            added.append((file, line[1:]))
        elif line.startswith("-"):
            removed.append((file, line[1:]))
    return added, removed, deleted


# ── CONSTRAINTS.md: rules, thresholds, exceptions ───────────────────────────────

MIN_BEFORE = re.compile(r"(>=|>|≥|at least|minimum|\bmin\b|no less than|not fall|not drop)\s*$")
MAX_BEFORE = re.compile(r"(<=|<|≤|at most|maximum|\bmax\b|no more than|under|below|not grow|not exceed)\s*$")
MIN_AFTER = re.compile(r"^\S*[\s|]*(or more|or higher|must not fall|must not drop)")
MAX_AFTER = re.compile(r"^\S*[\s|]*(or less|or lower|must not grow|must not exceed)")
EXCEPTION_ROW = re.compile(r"^\|\s*W\d+\s*\|")


def cells(text: str) -> list[str]:
    return [c.strip() for c in text.strip().split("|")[1:-1]]


def rule_key(text: str) -> str | None:
    """A rule is a floor bullet (text before its first colon) or a table row (first cell)."""
    s = text.strip()
    if s.startswith("|"):
        row = cells(s)
        if not row or set(row[0]) <= set("-: "):
            return None
        return row[0]
    if re.match(r"^[-*] ", s):
        return s[2:].split(":")[0].strip()
    return None


RULE_SECTIONS = ("## Floor", "## Enforced with numbers", "## Measured", "## Lifecycle")


def rule_lines(text: str) -> set[str]:
    """Floor bullets and table rows in the sections that hold rules; prose elsewhere is not."""
    out: set[str] = set()
    in_rules = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_rules = line.startswith(RULE_SECTIONS)
        elif in_rules and rule_key(line) is not None:
            out.add(line.strip())
    return out


def thresholds(text: str) -> list[tuple[float, str | None]]:
    out: list[tuple[float, str | None]] = []
    for m in re.finditer(r"\d+(?:\.\d+)?", text):
        before = text[max(0, m.start() - 24) : m.start()].lower()
        after = text[m.end() : m.end() + 40].lower()
        if MIN_BEFORE.search(before) or MIN_AFTER.search(after):
            direction: str | None = "min"
        elif MAX_BEFORE.search(before) or MAX_AFTER.search(after):
            direction = "max"
        else:
            direction = None
        out.append((float(m.group()), direction))
    return out


def threshold_verdict(was_text: str, now_text: str) -> str | None:
    before, after = thresholds(was_text), thresholds(now_text)
    for direction in ("min", "max", None):
        was = [n for n, d in before if d == direction]
        now = [n for n, d in after if d == direction]
        for i, b in enumerate(was):
            if i >= len(now):
                return "threshold-removed"
            n = now[i]
            if n == b:
                continue
            if direction == "min" and n < b or direction == "max" and n > b:
                return "threshold-loosened"
            if direction is None:
                return "threshold-changed"
    return None


def active_exceptions(today: date) -> tuple[list[tuple[str, str]], list[str]]:
    """Unexpired Exceptions rows as (rule, path glob), plus the IDs of expired rows."""
    path = Path(CONSTRAINTS)
    if not path.exists():
        return [], []
    active: list[tuple[str, str]] = []
    expired: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not EXCEPTION_ROW.match(line.strip()):
            continue
        row = cells(line)
        if len(row) < 6:
            continue
        exc_id, rule, glob, expires = row[0], row[1].strip("`"), row[2].strip("`"), row[5]
        try:
            if date.fromisoformat(expires) < today:
                expired.append(exc_id)
                continue
        except ValueError:
            expired.append(exc_id)  # no readable expiry is treated as expired
            continue
        active.append((rule, glob))
    return active, expired


# ── Main ────────────────────────────────────────────────────────────────────────


def main() -> None:
    base = parse_args()
    top = git("rev-parse", "--show-toplevel")
    if not top:
        bail("not inside a git work tree")
    os.chdir((top or "").strip())

    merge_base = (git("merge-base", base, "HEAD") or "").strip()
    if not merge_base:
        bail(f"no merge base against {base} (in CI, check out with fetch-depth: 0)")

    added, removed, deleted = walk(collect_diff(merge_base))
    findings: list[tuple[str, str, str]] = []

    def flag(rule: str, file: str, text: str) -> None:
        findings.append((rule, file, text.strip()[:120]))

    previous: dict[str, str] = {}
    for file, text in added:
        if file == SELF:
            continue
        if CODE_FILE.search(file):
            if SUPPRESSIONS.search(text):
                flag("silenced-checker", file, text)
            prev = previous.get(file, "")
            stripped = text.strip()
            if (
                STUBS.search(text)
                or (stripped in ("pass", "...") and OPENS_EXCEPT.match(prev))
                or (stripped == "}" and OPENS_CATCH.search(prev))
            ):
                flag("unfinished-work", file, text)
            if SKIPS.search(text):
                flag("test-made-easier", file, text)
            previous[file] = text
        if WORKFLOW_FILE.match(file) and SOFTENED_STEP.search(text):
            flag("ci-step-softened", file, text)
        if file == CONSTRAINTS and EXCEPTION_ROW.match(text.strip()):
            flag("new-exception", file, text)

    for f in deleted:
        if TEST_FILE.search(f):
            flag("test-deleted", f, "file deleted")
    for file, text in removed:
        if (
            TEST_FILE.search(file)
            and file not in deleted
            and not file.startswith(RUNNER_TESTS)
            and ASSERTION.search(text)
        ):
            flag("assertion-removed", file, text)

    added_steps = {(f, t.strip()) for f, t in added if WORKFLOW_FILE.match(f)}
    for file, text in removed:
        if WORKFLOW_FILE.match(file) and STEP_LINE.match(text) and (file, text.strip()) not in added_steps:
            flag("ci-step-removed", file, text)

    # A rule in CONSTRAINTS.md weakened or removed. Dropping an exception tightens: silent.
    base_rules = rule_lines(git("show", f"{merge_base}:{CONSTRAINTS}") or "")
    added_rules = [t for f, t in added if f == CONSTRAINTS and rule_key(t) is not None]
    for f, was in removed:
        if f != CONSTRAINTS or was.strip() not in base_rules:
            continue
        now = next((t for t in added_rules if rule_key(t) == rule_key(was)), None)
        if now is None:
            if not EXCEPTION_ROW.match(was.strip()):
                flag("rule-removed", f, was)
            continue
        verdict = threshold_verdict(was, now)
        if verdict:
            flag(verdict, f, f"{was.strip()}  ->  {now.strip()}")

    active, expired = active_exceptions(date.today())
    for exc_id in expired:
        flag("exception-expired", CONSTRAINTS, f"{exc_id} has passed its expiry date")
    findings = [
        (rule, file, text)
        for rule, file, text in findings
        if not any(rule == r and fnmatch.fnmatch(file, g) for r, g in active)
    ]

    if not findings:
        print("floor-guard: clean")
        sys.exit(0)

    print(f"floor-guard: {len(findings)} floor violation(s):", file=sys.stderr)
    for rule, file, text in findings:
        print(f"  [{rule}] {file}: {text}", file=sys.stderr)
    if any(r == "rule-removed" for r, _, _ in findings):
        print(
            "\nA rule-removed finding can also mean the rule's label changed: rename a rule in one"
            " commit and change its thresholds in another.",
            file=sys.stderr,
        )
    if any(r == "threshold-removed" for r, _, _ in findings):
        print(
            "\nA threshold-removed finding can also mean a number gained or lost its direction words"
            ' (">= 80%" becoming "80%"): compare the two lines before assuming a threshold was deleted.',
            file=sys.stderr,
        )

    messages = git("log", "--format=%B", f"{merge_base}..HEAD") or ""
    if APPROVAL_TRAILER.search(messages):
        print("\nfloor-guard: approved by a Constraints-Approved-By trailer; not blocking.", file=sys.stderr)
        sys.exit(0)
    print(
        "\nEach is a move that lowers the bar. Fix the code, add a tracked exception to"
        " CONSTRAINTS.md, or have a human approve it (see CONSTRAINTS.md, Overrides).",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
