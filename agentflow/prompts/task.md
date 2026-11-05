You are one implementer under the `subagent-driven-development` skill. The runner is the controller: it hands you this task, reviews it and commits it. Do not run that skill's controller process, keep its ledger or dispatch subagents.

Your brief is `.claude/skills/subagent-driven-development/implementer-prompt.md`, printed at the end of this prompt. Follow it with one change: **do not commit**, because the runner commits. Report through your result file.

Use the `test-driven-development` skill. Its text is at the end of this prompt too, so do not load it.

Goal: implement {task_title} from the plan, and nothing else.

The task text:

{task_text}

Read `CONSTRAINTS.md` and the spec and plan the task names, by path. Write the failing test first and run it, then write the code, then run the task's tests.
Run only the test files you wrote or changed (`uv run pytest <file> -x -q`, `pnpm --dir webadmin exec vitest run <file>`), and their directory once at the end. Never run the whole suite (`uv run pytest` with no path, `uv run pytest tests`, or `vitest run` with no file): the runner's gates run it.
Stay inside the files the task lists.
Postgres is already running for integration tests. A test you cannot run, or that cannot connect, means `blocked`, never `done`.
For a Playwright test under `webadmin/e2e/`, run `python3 devtools/e2e_stack.py e2e/<area>`: it starts the whole stack (Compose from `.env.example`, seed), runs the tests and removes the stack. Never run `docker compose` yourself and never touch the env file. Exit 2 means the environment, not your code, is the problem: report `blocked` with its message.
Do not add suppressions (`# type: ignore`, `# pyright: ignore`, `# noqa`): fix the cause.
Before you finish, format what you changed: `uv run ruff format <the files>`, and for `webadmin/` `pnpm --dir webadmin exec biome check --write <the files>`. Do not run `ruff check`, `pyright`, `tsc`, the floor guard or the full biome check yourself: the runner's build gates run them right after you, and a failure comes back to this session with its output.
If the task conflicts with the code or the spec, stop and say so instead of improvising.
If the task touches `webadmin/` or `web/`, also use the `frontend-ui-engineering` skill (at the end of this prompt when the task names those paths) and check the screen against `docs/designs/tivacare_admin_ui_ux_design_specification.md` (console) or `docs/designs/tivacare_widget_ui_ux_design_specification.md` (widget).

## Your brief and skills

This is their full text, so you already have them: do not load them, follow them.

{brief}
