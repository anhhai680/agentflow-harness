# Verify and review, one purpose each: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `verify` proves the branch works and debugs what fails, `review` judges the change and owns the only review-feedback fix loop, and the PR shows verify's evidence.

**Architecture:** A new pure module, `agentflow/verification.py`, parses the verify step's `signals.verification` and renders it for two readers: the `{verification}` prompt placeholder (for `branch_review`) and a `Verification` section in the PR body. The engine prompts and bundled skills for verify and review are rewritten to the upstream split, and two `Retired` entries let `validate --fix` carry the change into pack overrides. The engine's default workflow drops `plan_fixes` / `fix_findings` from `verify`. This repository's pack follows in a last task, which is interactive-only.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, pytest with the fake agent (`agentflow/tests/fake_agent.py`).

**Spec:** `docs/specs/agentflow-verify-review-proposal.md` (approved 2026-11-14). Its "Decided behaviour" section overrides anything else in it.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No third-party import.
- No project strings in any engine file (`test_portability.py`'s `DENYLIST`).
- `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. The `phases:` of the `python` and `node` fixture packs are not edited: they keep `plan_fixes` / `fix_findings` in `verify`, and that shape must keep working. Their `prompts/verify.md` and `prompts/branch_review.md` overrides carry the text Tasks 3 and 4 retire, so those two tasks swap in the new line there (prompts are not in the golden output), or `validate` warns on both fixture packs.
- No new config key and no new `skills.map` key.
- A change that drops or rewords engine prompt text adds a `Retired` entry to `agentflow/pack_fix.py` with `retired_in="2.7"`. Its `new` must appear in the engine prompt, and neither `old` nor `detect` may appear in any engine prompt (`test_register_matches_the_engine_prompts`).
- Model text from a result (criterion, evidence, reason, gate result) reaches a PR or a prompt only as one clean line: `hitl.clean_fact`, cut to 300 characters.
- `.agentflow/**` is a forbidden path inside agentflow runs, so Task 6 is done in an interactive session only.
- `agentflow/pack_fix.py` is on the security overlay's paths: Tasks 3 and 4 are planned and reviewed with `security-and-hardening`.
- A commit message is one imperative sentence ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never create, edit or delete `uv.lock`.
- Test command form: `uv run --no-project --with pyyaml --with pytest pytest <path>[::<test>] --import-mode=prepend -q`.

## Review Focus

1. **Hostile story text** (a newline, a `## ` heading, a bidi override or a backtick inside a criterion or reason) must stay on its one line in the PR and the prompt, and must not open a new PR section. Pinned in Task 1 (`test_hostile_text_stays_on_one_line`).
2. **An oversized story** (thousands of criteria, megabyte strings) must not bloat the PR or the review prompt: each list is capped at 40 items plus one "… and N more" line, and each text at 300 characters. Pinned in Task 1 (`test_lists_and_text_are_capped`).
3. **A malformed signal** (`"verification": "all good"`, `criteria` as a mapping, an item missing fields, `"ran": "yes"`) must never raise from `render_pr_body` or `build_prompt`: a malformed whole becomes "verify reported no evidence", and a malformed item is dropped. Pinned in Task 1 (`test_parse_rejects_malformed_signals`, `test_parse_drops_malformed_items`).
4. **Verify ran twice** (a verify refine round) and **a review fix before or after it**: only a `review/fix_findings` commit *after* verify's last finish marks evidence stale. Pinned in Task 1 (`test_stale_sha_only_counts_review_fixes_after_verify`).
5. **Verify never ran** (a run started `--from review`, a pack without the step): the prompt slot is empty, the PR says "verify did not run", and no missing-file error escapes. Pinned in Task 2 (`test_verification_placeholder_is_empty_when_verify_never_ran`, `test_pr_body_says_verify_did_not_run`).

---

### Task 1: The verification story module

**Files:**
- Create: `agentflow/verification.py`
- Test: `agentflow/tests/test_verification.py`

**Interfaces:**
- Consumes: `agentflow.hitl.clean_fact(text: str) -> str`, `agentflow.hitl.read_result(path: Path, refine: bool = False) -> StepResult` (raises `ResultError` on a malformed file), `agentflow.state.Run.result_path(key: str) -> Path`.
- Produces:
  - `VERIFY_KEY = "verify/verify"`, `REVIEW_FIX_KEY = "review/fix_findings"`, `NO_EVIDENCE = "verify reported no evidence"`, `NOT_RUN = "verify did not run"`, `MAX_ITEMS = 40`, `MAX_TEXT = 300`
  - `@dataclass(frozen=True) class GateLine: gate: str; cmd: str; result: str; passed: bool`
  - `@dataclass(frozen=True) class Criterion: criterion: str; evidence: str; ran: bool; passed: bool`
  - `@dataclass(frozen=True) class Gap: what: str; why: str`
  - `@dataclass(frozen=True) class Story: gates: tuple[GateLine, ...]; criteria: tuple[Criterion, ...]; not_verified: tuple[Gap, ...]; more: int` (`more` counts the items cut by `MAX_ITEMS`, over all three lists)
  - `parse(signals: object) -> Story | None`
  - `stale_sha(events: list[dict]) -> str | None`
  - `render_prompt(found: bool, story: Story | None) -> str`
  - `render_pr(found: bool, story: Story | None, stale: str | None) -> list[str]`
  - `read_story(run: Run) -> tuple[bool, Story | None]` (`found` is False only when `verify/verify` has no result file)

- [ ] **Step 1: Write the failing tests**

```python
from agentflow import verification as v

GOOD = {"verification": {
    "gates": [{"gate": "unit", "cmd": "pytest", "result": "12 passed", "passed": True}],
    "criteria": [{"criterion": "Criterion 1", "evidence": "tests/test_a.py::test_one", "ran": True, "passed": True},
                 {"criterion": "Criterion 2", "evidence": "tests/test_a.py::test_two", "ran": True, "passed": False}],
    "not_verified": [{"what": "Criterion 3", "why": "needs a browser"}]}}

def test_parse_reads_a_good_story():
    story = v.parse(GOOD)
    assert [c.criterion for c in story.criteria] == ["Criterion 1", "Criterion 2"]
    assert story.gates[0] == v.GateLine("unit", "pytest", "12 passed", True)
    assert story.not_verified == (v.Gap("Criterion 3", "needs a browser"),) and story.more == 0

@pytest.mark.parametrize("signals", [{}, {"verification": "all good"}, {"verification": {"criteria": {"a": 1}}},
                                     {"verification": {"gates": [], "criteria": [], "not_verified": []}}, None, []])
def test_parse_rejects_malformed_signals(signals):
    assert v.parse(signals) is None

def test_parse_drops_malformed_items():
    story = v.parse({"verification": {"criteria": [
        {"criterion": "ok", "evidence": "t", "ran": True, "passed": True},
        {"criterion": "no evidence key", "ran": True, "passed": True},
        {"criterion": "bad bool", "evidence": "t", "ran": "yes", "passed": True}, "a string"]}})
    assert [c.criterion for c in story.criteria] == ["ok"]

def test_hostile_text_stays_on_one_line():
    hostile = "line one\n## Injected‮`tick`"
    story = v.parse({"verification": {"criteria": [{"criterion": hostile, "evidence": hostile, "ran": True, "passed": True}],
                                      "not_verified": [{"what": hostile, "why": hostile}]}})
    for line in v.render_pr(True, story, None) + v.render_prompt(True, story).splitlines():
        assert "\n" not in line and "‮" not in line and not line.startswith("## ")
    assert all(line.count("`") % 2 == 0 for line in v.render_pr(True, story, None))  # evidence backticks replaced

def test_lists_and_text_are_capped():
    many = [{"criterion": "c" * 1000, "evidence": "t", "ran": True, "passed": True}] * 100
    story = v.parse({"verification": {"criteria": many}})
    assert len(story.criteria) == 40 and story.more == 60
    assert all(len(c.criterion) <= 300 for c in story.criteria)
    assert v.render_pr(True, story, None)[-1] == "- … and 60 more"

def test_stale_sha_only_counts_review_fixes_after_verify():
    done = lambda seq: {"seq": seq, "type": "step_finished", "key": "verify/verify", "status": "done"}
    fix = lambda seq, sha: {"seq": seq, "type": "step_committed", "key": "review/fix_findings", "sha": sha}
    assert v.stale_sha([done(1)]) is None
    assert v.stale_sha([fix(1, "a" * 40), done(2)]) is None              # a fix before verify's last finish
    assert v.stale_sha([done(1), fix(2, "b" * 40), done(3)]) is None     # verify ran again after it
    assert v.stale_sha([done(1), fix(2, "c" * 40), fix(4, "d" * 40)]) == "d" * 12
    assert v.stale_sha([fix(2, "e" * 40)]) is None                        # verify never finished

def test_render_pr_marks_criteria_and_lists_gaps():
    lines = v.render_pr(True, v.parse(GOOD), "abcdef123456")
    assert lines == ["- Criterion 1: `tests/test_a.py::test_one` ✓ (before review fix abcdef123456)",
                     "- Criterion 2: `tests/test_a.py::test_two` ✗ (before review fix abcdef123456)",
                     "- not verified: Criterion 3: needs a browser"]

def test_render_without_a_story():
    assert v.render_pr(False, None, None) == ["verify did not run"]
    assert v.render_pr(True, None, None) == ["verify reported no evidence"]
    assert v.render_prompt(False, None) == ""
    assert v.render_prompt(True, None) == "verify reported no evidence"

def test_render_prompt_lists_gates_criteria_and_gaps():
    assert v.render_prompt(True, v.parse(GOOD)).splitlines() == [
        "- gate `unit`: 12 passed ✓",
        "- Criterion 1: `tests/test_a.py::test_one` ✓",
        "- Criterion 2: `tests/test_a.py::test_two` ✗",
        "- not verified: Criterion 3: needs a browser"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_verification.py --import-mode=prepend -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agentflow.verification'`

- [ ] **Step 3: Implement `agentflow/verification.py`**

Pure functions except `read_story`. Rules the tests don't fully settle:
- `parse`: the signal must be a mapping whose three keys, when present, are lists. Otherwise return None. Keep an item only when every field is present with the right type (`str` for text, `bool` for `ran`/`passed`). Clean each text with `clean_fact`, replace "`" with "'" in `evidence`, `cmd` and `gate`, and cut to `MAX_TEXT` with a trailing `…`. Return None when all three lists end up empty.
- Marks: `✓` when `ran and passed`, `✗` when `ran and not passed`, `– not run` when not `ran`.
- `render_pr` leaves gates out (the PR's `Gates` section has them). `render_prompt` includes them. Both end with `- … and N more` when `more > 0`.
- `read_story`: no result file (`FileNotFoundError`) → `(False, None)`. `ResultError`, `OSError` or `ValueError` → `(True, None)`. Otherwise `(True, parse(result.signals))`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_verification.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agentflow/verification.py agentflow/tests/test_verification.py
git commit -m "Parse and render the verify step's verification story."
```

---

### Task 2: `{verification}` in prompts and a `Verification` section in the PR

**Files:**
- Modify: `agentflow/config.py` (`PROMPT_PLACEHOLDERS`, after `"gate_evidence"`)
- Modify: `agentflow/lanes.py` (`build_prompt`, next to `values["gate_evidence"] = ""`)
- Modify: `agentflow/feature_lane.py` (`render_pr_body`, right after the `Gates` section)
- Modify: `CHANGELOG.md` (`## [Unreleased]` → `### Added`)
- Test: `agentflow/tests/test_agent_step.py`, `agentflow/tests/test_feature_lane.py`

**Interfaces:**
- Consumes: Task 1's `read_story`, `render_prompt`, `render_pr`, `stale_sha`.
- Produces: the placeholder `{verification}` (Task 4 puts it in `branch_review.md`), and the PR section `## Verification`.

- [ ] **Step 1: Write the failing tests**

In `test_agent_step.py`, using the file's existing `skill_prompt` / `make_step` helpers with a pack prompt that contains `{verification}` and a run whose `verify/verify` result file is written directly (`run.result_path("verify/verify")`):
- `test_verification_placeholder_carries_the_story`: a result with the Task 1 `GOOD` signals renders `- Criterion 1: \`tests/test_a.py::test_one\` ✓` in the prompt.
- `test_verification_placeholder_is_empty_when_verify_never_ran`: no result file means no `{verification}` and no story text, and nothing raises.
- `test_verification_placeholder_says_no_evidence`: a result with no `verification` signal, and one with `"verification": "x"`, both render `verify reported no evidence`.
- `test_verification_text_with_braces_stays_literal`: a criterion `"keeps {module} literal"` appears verbatim in the prompt.
- `test_verification_is_never_read_from_the_worktree`: a `results/verify__verify.json` planted in the worktree changes nothing.

In `test_feature_lane.py`, next to the existing `render_pr_body` tests:
- `test_pr_body_has_a_verification_section`: `## Verification` comes right after `## Gates` and holds the criteria lines.
- `test_pr_body_marks_evidence_older_than_a_review_fix`: after `run.emit("step_committed", key="review/fix_findings", sha="f" * 40, summary="fix")`, which follows verify's `step_finished`, each criterion line ends with `(before review fix ffffffffffff)`.
- `test_pr_body_says_verify_did_not_run`: no verify result means the section body is `verify did not run`.
- `test_pr_body_with_a_malformed_story_does_not_block`: the section says `verify reported no evidence` and `render_pr_body` returns normally.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agent_step.py agentflow/tests/test_feature_lane.py -k "verification or verify_did_not_run or review_fix or malformed_story" --import-mode=prepend -q`
Expected: FAIL (`unknown placeholder '{verification}'`, and no `## Verification` section)

- [ ] **Step 3: Implement**

- `config.py`: add `"verification"` to `PROMPT_PLACEHOLDERS`.
- `lanes.build_prompt`: `values["verification"] = verification.render_prompt(*verification.read_story(ctx.run)) if ctx.run is not None and "{verification}" in body else ""`. The result file is only read when the template asks for it.
- `feature_lane.render_pr_body`: put `_section("Verification", verification.render_pr(found, story, verification.stale_sha(events)))` right after `_section("Gates", ...)`. Add "the verification story" to the docstring.
- `CHANGELOG.md` under `### Added`: `- The pull request body has a \`Verification\` section: each acceptance criterion with what ran to prove it, what verify could not run and why, and evidence older than a review fix marked as such.`

- [ ] **Step 4: Run them to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agent_step.py agentflow/tests/test_feature_lane.py agentflow/tests/test_verification.py --import-mode=prepend -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agentflow/config.py agentflow/lanes.py agentflow/feature_lane.py CHANGELOG.md agentflow/tests/test_agent_step.py agentflow/tests/test_feature_lane.py
git commit -m "Show the verification story in the pull request and offer it to prompts as {verification}."
```

---

### Task 3: Verify proves and debugs (engine prompt and bundled skill)

**Files:**
- Modify: `agentflow/prompts/verify.md` (line 8, plus two new paragraphs)
- Modify: `agentflow/skills/agentflow-verify/SKILL.md`
- Modify: `agentflow/pack_fix.py` (`RETIRED`)
- Modify: `agentflow/tests/fixtures/packs/python/.agentflow/prompts/verify.md`, `agentflow/tests/fixtures/packs/node/.agentflow/prompts/verify.md` (the retired line only)
- Test: `agentflow/tests/test_pack_fix.py`, `agentflow/tests/test_agent_step.py`

**Interfaces:**
- Consumes: the `signals.verification` shape from Task 1.
- Produces: `Retired(id="verify-fix-only-the-cause", file="verify.md", retired_in="2.7", detect="fix only the cause", ...)`.

- [ ] **Step 1: Write the failing tests**

- `test_pack_fix.py::test_old_verify_failure_line_is_fixable`: same pattern as `test_old_result_line_is_fixable`. A pack `prompts/verify.md` holding the old line gives one fixable `verify-fix-only-the-cause` issue, and `--fix` writes the new text.
- `test_agent_step.py::test_engine_verify_prompt_debugs_and_asks_for_the_story`: the rendered engine `verify.md` contains `stop the line`, `"verification"`, `not_verified` and `never as met`, and does not contain `fix only the cause`.
- `test_agent_step.py::test_bundled_verify_skill_is_test_and_debug`: `agentflow/skills/agentflow-verify/SKILL.md` contains `## When a gate fails` and `Reproduce`, and does not contain `Compare the result against the spec`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack_fix.py agentflow/tests/test_agent_step.py -k "verify" --import-mode=prepend -q`
Expected: FAIL

- [ ] **Step 3: Implement**

In `verify.md`, replace line 8 (`If something fails, say which gate and why, and fix only the cause. Never weaken the repository's quality rules, skip a test or add a suppression to pass.`) with this exact text. It is also the `Retired` entry's `new`:

```
If a gate failed, stop the line and debug it: reproduce the failure, find where it comes from, fix the cause and guard it with a test. Say whether the cause is in the branch or in the environment (a clock, a database, a port, a flaky test the branch does not touch). Never weaken the repository's quality rules, skip a test or add a suppression to pass.
Do not judge the code by reading it against the spec: the review phase does that. For each acceptance criterion in the spec and each verification command in the plan, name the test or command that ran and its result. A criterion nothing ran for is not verified: list it with the reason, never as met.
Put that account in the result's `signals` as `{"verification": {"gates": [{"gate": "name", "cmd": "command", "result": "result line", "passed": true}], "criteria": [{"criterion": "Success Criterion 3", "evidence": "the test or command", "ran": true, "passed": true}], "not_verified": [{"what": "Success Criterion 7", "why": "needs a browser"}]}}`.
```

In the `findings` paragraph, add one sentence after `lets it through.`: `An environment failure you cannot fix in the worktree is blocking too, with that reason.` That is an addition, so it needs no `Retired` entry. Every other line stays byte-identical.

Rewrite `agentflow-verify/SKILL.md` (title `# Verify: prove it works`):
- **Steps:** read the gate evidence; report each gate; for each acceptance criterion and plan verification command, name what ran; run what no gate covers; list what could not run as not verified.
- **`## When a gate fails`:** stop the line; reproduce, localize, fix the cause, guard with a test; name the cause as branch or environment; the runner re-runs every gate.
- **Rules:** the current ones, plus "Never judge the code by reading it: review does that."
- Drop step 4 ("Compare the result against the spec's acceptance criteria and the plan's tasks…").

Add the `Retired` entry, with `old` set to the exact line 8 above. In both fixture packs' `prompts/verify.md`, replace that same line with the entry's `new`. Leave the rest of each file alone.

- [ ] **Step 4: Run them to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack_fix.py agentflow/tests/test_agent_step.py agentflow/tests/test_portability.py agentflow/tests/test_config.py agentflow/tests/test_autonomy.py --import-mode=prepend -q`
Expected: PASS (including `test_register_matches_the_engine_prompts`; no drift warning on either fixture pack)

- [ ] **Step 5: Commit**

```bash
git add agentflow/prompts/verify.md agentflow/skills/agentflow-verify/SKILL.md agentflow/pack_fix.py agentflow/tests/fixtures/packs/python/.agentflow/prompts/verify.md agentflow/tests/fixtures/packs/node/.agentflow/prompts/verify.md agentflow/tests/test_pack_fix.py agentflow/tests/test_agent_step.py
git commit -m "Make verify prove the branch works and debug a failing gate, and retire its old failure line."
```

---

### Task 4: Review judges on five axes and checks the verification (engine prompt and bundled skill)

**Files:**
- Modify: `agentflow/prompts/branch_review.md` (line 10, plus a new section)
- Modify: `agentflow/skills/agentflow-review/SKILL.md`
- Modify: `agentflow/pack_fix.py` (`RETIRED`)
- Modify: `agentflow/tests/fixtures/packs/python/.agentflow/prompts/branch_review.md`, `agentflow/tests/fixtures/packs/node/.agentflow/prompts/branch_review.md` (the retired line only)
- Test: `agentflow/tests/test_pack_fix.py`, `agentflow/tests/test_agent_step.py`

**Interfaces:**
- Consumes: `{verification}` from Task 2.
- Produces: `Retired(id="branch-review-checklist", file="branch_review.md", retired_in="2.7", detect="Check that every task landed", ...)`.

- [ ] **Step 1: Write the failing tests**

- `test_pack_fix.py::test_old_branch_review_checklist_is_fixable`: same pattern as Task 3.
- `test_agent_step.py::test_branch_review_prompt_has_five_axes_and_the_story`: the rendered engine `branch_review.md`, for a run whose verify result holds the Task 1 `GOOD` story, contains `readability and simplicity`, `architecture`, `performance`, `"None"`, `## What verify proved` and `- Criterion 1:`. It does not contain `every task landed` or `tests cover the changed lines`.
- `test_agent_step.py::test_bundled_review_skill_has_five_axes`: `agentflow-review/SKILL.md` contains `Readability`, `Architecture`, `Performance` and `## Verify the verification`, and does not contain `Did every task land`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack_fix.py agentflow/tests/test_agent_step.py -k "review" --import-mode=prepend -q`
Expected: FAIL

- [ ] **Step 3: Implement**

In `branch_review.md`, replace line 10 (`Check that every task landed, nothing outside the plan changed, tests cover the changed lines, and the docs the change touches (spec, idea doc, decision record index) are consistent.`) with this exact text, which is the `Retired` entry's `new`:

```
Check that nothing outside the plan changed and that the docs the change touches (spec, idea doc, decision record index) are consistent. Then judge the change on five axes, and write "None" for an axis that does not apply: correctness (does the code do what the spec says, are edge cases and error paths handled, are the tests the right tests), readability and simplicity, architecture (does it fit the existing patterns, is logic leaking into a shared module, is there a near-duplicate helper), security, and performance.
```

Right after it, add (an addition, so no `Retired` entry):

```

## What verify proved

{verification}

That is the verify step's account of what ran. Do not run the gates again and do not repeat it: check it. Flag a criterion whose evidence does not prove it (a test that would pass without the change, a command that does not reach the changed code), and a not-verified item that a test could have covered.
```

Rewrite the "What to check" list in `agentflow-review/SKILL.md` as the five axes plus scope and docs. Add a `## Verify the verification` section that says the same as the paragraph above. Labels, "Declined to judge" and Output Format stay.

Add the `Retired` entry, with `old` set to the exact line 10 above. In both fixture packs' `prompts/branch_review.md`, replace that same line with the entry's `new`. The fixture overrides do not get the `{verification}` section: a pack override keeps its own wording, and the fixture packs pin the old workflow.

- [ ] **Step 4: Run them to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack_fix.py agentflow/tests/test_agent_step.py agentflow/tests/test_portability.py agentflow/tests/test_config.py agentflow/tests/test_workflow.py --import-mode=prepend -q`
Expected: PASS (`validate` accepts `{verification}` in an engine prompt; no drift warning on either fixture pack)

- [ ] **Step 5: Commit**

```bash
git add agentflow/prompts/branch_review.md agentflow/skills/agentflow-review/SKILL.md agentflow/pack_fix.py agentflow/tests/fixtures/packs/python/.agentflow/prompts/branch_review.md agentflow/tests/fixtures/packs/node/.agentflow/prompts/branch_review.md agentflow/tests/test_pack_fix.py agentflow/tests/test_agent_step.py
git commit -m "Make the branch review judge five axes and check verify's evidence, and retire its old checklist."
```

---

### Task 5: The engine's default workflow and its docs

**Files:**
- Modify: `agentflow/defaults/config.yaml` (`phases.verify`, `phases.review`)
- Modify: `docs/agentflow-playbook.md` (§1 bullet "The agent handles its own problems", §8 paragraph "The runner fixes what a review finds")
- Modify: `CHANGELOG.md` (`### Changed`)
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_autonomy.py`

**Interfaces:**
- Consumes: nothing new. The engine already re-runs a `blocks_on_findings` step in a fresh session with `{earlier_findings}` (`flow_control._run_agent`).
- Produces: engine-default `verify` steps `[setup, verify]`, and `review` steps `[setup (when refine_round), plan_fixes, fix_findings, branch_review]`, with the `&plan_fixes` / `&fix_findings` anchors defined in `review`.

- [ ] **Step 1: Write the failing tests**

- `test_config.py`: remove `"verify/plan_fixes"` and `"verify/fix_findings"` from `STEP_SKILLS`, and add `test_engine_default_verify_has_no_refine_round_step`. It loads `agentflow/defaults/config.yaml` with `yaml.safe_load` and asserts `[s["id"] for s in data["phases"]["verify"]["steps"]] == ["setup", "verify"]`, and that `review`'s step ids are `["setup", "plan_fixes", "fix_findings", "branch_review"]`.
- `test_autonomy.py::test_a_verify_without_a_fix_plan_reruns_only_verify`: `auto.configure(PIPE, autopilot=True, edit=drop_verify_fix_steps)`, where the edit removes the steps with ids `plan_fixes` and `fix_findings` from `data["phases"]["verify"]["steps"]`. Script `verify/verify` as `[done("thiếu", findings=[BLOCKING]), done("sạch")]` and run `begin_bug`. Assert:
  - the run reaches `done`;
  - the `phase_rejected` phases are `["verify"]`;
  - `verify/verify` is called twice, and the second call has no `--resume` and carries `BLOCKING["text"]`;
  - `verify/plan_fixes`, `review/plan_fixes` and `review/fix_findings` are never called;
  - `run.state["refine"] is None`.
- `test_autonomy.py::test_a_verify_finding_that_outlasts_its_rounds_is_not_sent_to_a_fix_plan`: same edit, with `verify/verify` scripted blocking on every call. The run does not reach `done`, and `verify/plan_fixes` is never called.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_autonomy.py --import-mode=prepend -q`
Expected: FAIL in `test_config.py` (the defaults still hold the fix steps). The two autonomy tests may already pass, since they pin existing engine behaviour for the new shape. Keep them either way.

- [ ] **Step 3: Implement**

- `defaults/config.yaml`: write `verify` and `review` exactly as in the spec's Design §1 and §2 code blocks. Update the comment above `skills:` if it names verify's fix steps.
- Playbook §1: replace `blocking review or verify findings get an automatic fix plan and fix round` with `a failing verify gate is debugged inside \`verify\`, blocking review findings get an automatic fix plan and fix round`.
- Playbook §8: append to the paragraph that starts "The runner fixes what a review finds": `\`verify\` has no fix plan of its own: it proves the branch works, debugs a failing gate in its own step, and a blocking verify finding re-runs \`verify\` in a fresh session. The PR's \`Verification\` section lists what verify proved and what it could not, and marks evidence older than a review fix.`
- `CHANGELOG.md` under `### Changed`: `- \`verify\` proves the branch works and debugs a failing gate in its own step, with no fix plan of its own; \`branch_review\` judges five axes, checks verify's evidence and no longer re-checks completeness or coverage. A pack override of \`verify.md\` or \`branch_review.md\` with the old text gets a \`validate --fix\` repair.`

- [ ] **Step 4: Run the touched tests, then the full suite**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_autonomy.py agentflow/tests/test_headless.py agentflow/tests/test_workflow.py agentflow/tests/test_plug_and_play.py --import-mode=prepend -q`
Expected: PASS
Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`
Expected: PASS, with `git status agentflow/tests/fixtures/explain` clean

- [ ] **Step 5: Commit**

```bash
git add agentflow/defaults/config.yaml docs/agentflow-playbook.md CHANGELOG.md agentflow/tests/test_config.py agentflow/tests/test_autonomy.py
git commit -m "Keep the review-feedback fix loop in review only in the engine's default workflow."
```

---

### Task 6: This repository's pack (interactive session only)

`.agentflow/**` is forbidden inside agentflow runs, so this task is done by hand or in an interactive session after Tasks 1–5 have merged.

**Files:**
- Modify: `.agentflow/config.yaml` (`phases.verify`, `phases.review`)
- Modify: `.agentflow/prompts/verify.md`, `.agentflow/prompts/branch_review.md` (through `validate --fix`)

- [ ] **Step 1: See the drift**

Run: `bin/agentflow validate`
Expected: two fixable warnings, `verify-fix-only-the-cause` and `branch-review-checklist`

- [ ] **Step 2: Repair the overrides**

Run: `bin/agentflow validate --fix`, then `git diff .agentflow/prompts`
Expected: line 10 of each override is replaced by the engine's new text. The overrides' own first lines (`Use the \`verification-before-completion\` skill.`, the `requesting-code-review` wording, `origin/main`) are kept. Then add the `## What verify proved` / `{verification}` section from Task 4 to `.agentflow/prompts/branch_review.md` by hand (an addition, which `--fix` does not make).

- [ ] **Step 3: Reshape the phases**

In `.agentflow/config.yaml`:
- Move the `&plan_fixes` and `&fix_findings` definitions from `verify` into `review`, replacing `*plan_fixes` / `*fix_findings` there, with their comments.
- Change the `verify` step's `brief` to `[.claude/skills/verification-before-completion/SKILL.md, .claude/skills/systematic-debugging/SKILL.md]`.
- `verify` ends up with `*setup` and `verify` only.

- [ ] **Step 4: Verify**

Run: `bin/agentflow validate`
Expected: `ok: config.yaml is valid (0 warnings)`
Run: `bin/agentflow explain feature --module verify-review`
Expected: `verify:` lists `setup` and `verify` only, and `review:` lists `setup`, `plan_fixes`, `fix_findings` (each `when: refine_round`), `branch_review` and `security_review`

- [ ] **Step 5: Commit**

```bash
git add .agentflow/config.yaml .agentflow/prompts/verify.md .agentflow/prompts/branch_review.md
git commit -m "Give this repository's pack the verify and review split."
```
