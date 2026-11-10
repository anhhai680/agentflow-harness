"""The scorecard: signals of the finished runs against a frozen per-machine baseline, with conformance as the SLO."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agentflow import scorecard
from agentflow.safeio import SymlinkRefused
from agentflow.state import runs_root

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _run(run_id: str, lane: str, ended: str) -> SimpleNamespace:
    events = [{"seq": 1, "ts": ended, "type": "run_finished", "by": "runner"}]
    return SimpleNamespace(id=run_id, state={"pipeline": lane}, events=SimpleNamespace(read=lambda: events))


@pytest.fixture
def fake_runs(monkeypatch):
    """Finished runs and their signals are synthetic: `runs` is `{id: (lane, ended, signals)}`."""
    table: dict[str, tuple[str, str, dict]] = {}

    def finished(_repo, since):
        out = [_run(rid, lane, ended) for rid, (lane, ended, _s) in sorted(table.items())]
        return [r for r in out if since is None or datetime.fromisoformat(r.events.read()[0]["ts"]) > since]

    monkeypatch.setattr(scorecard, "finished_runs", finished)
    monkeypatch.setattr(scorecard, "run_row", lambda run, now: dict(table[run.id][2]))
    return table


def _baseline_runs(table: dict) -> None:
    table["r1"] = ("feature", "2026-11-01T10:00:00+00:00", {"gate_first_pass:lint": 0.5, "cost_usd": 2.0})
    table["r2"] = ("feature", "2026-11-02T10:00:00+00:00", {"gate_first_pass:lint": 0.5, "cost_usd": 4.0})


def test_freeze_then_refuse_then_replace(tmp_git, fake_runs):
    _baseline_runs(fake_runs)
    data = scorecard.freeze(tmp_git, None, None, None, False, NOW)
    path = runs_root(tmp_git) / scorecard.BASELINE
    lane = data["lanes"]["feature"]
    assert path.is_file() and json.loads(path.read_text(encoding="utf-8")) == data
    assert lane["runs"] == ["r1", "r2"] and lane["engine"]
    assert lane["from"] == "2026-11-01T10:00:00+00:00" and lane["to"] == "2026-11-02T10:00:00+00:00"
    assert lane["signals"]["cost_usd"] == {"runs": 2, "mean": 3.0}
    assert scorecard.load_baseline(tmp_git) == data
    with pytest.raises(FileExistsError):
        scorecard.freeze(tmp_git, None, None, None, False, NOW)
    fake_runs["r3"] = ("feature", "2026-11-03T10:00:00+00:00", {"cost_usd": 9.0})
    again = scorecard.freeze(tmp_git, None, None, None, True, NOW)
    assert again["lanes"]["feature"]["runs"] == ["r1", "r2", "r3"]


def test_compare_marks_direction_and_slo(tmp_git, fake_runs):
    _baseline_runs(fake_runs)
    scorecard.freeze(tmp_git, None, None, None, False, NOW)
    fake_runs["r3"] = ("feature", "2026-11-05T10:00:00+00:00",
                       {"gate_first_pass:lint": 1.0, "cost_usd": 5.0, "conformance_missing": 2})
    fake_runs["r4"] = ("bug", "2026-11-06T10:00:00+00:00", {"cost_usd": 1.0})
    view = scorecard.scorecard_view(tmp_git, None, None, None, NOW)
    assert view["baseline"] is True and list(view["lanes"]) == ["feature", "bug"]
    rows = view["lanes"]["feature"]
    by = {r["signal"]: r for r in rows}
    assert rows[0]["signal"] == "conformance_missing" and rows[0]["slo"] is True
    assert not any(r["slo"] for r in rows[1:])
    assert (by["gate_first_pass:lint"]["verdict"], by["gate_first_pass:lint"]["delta"]) == ("better", 0.5)
    assert (by["cost_usd"]["verdict"], by["cost_usd"]["baseline"], by["cost_usd"]["current"]) == ("worse", 3.0, 5.0)
    assert (by["cost_usd"]["baseline_n"], by["cost_usd"]["current_n"]) == (2, 1)
    assert by["conformance_missing"]["baseline"] is None and by["conformance_missing"]["delta"] is None
    # the baseline's own runs are not "current": only runs that ended after its `to` count
    assert by["cost_usd"]["current_n"] == 1
    table = scorecard.scorecard_table(view)
    assert "conformance_missing" in table and "worse" in table and "better" in table
    # a lane with no baseline entry still shows its current runs, with no "before" and the same SLO rule
    fake_runs["r4"] = ("bug", "2026-11-06T10:00:00+00:00", {"cost_usd": 1.0, "conformance_missing": 1})
    bug = scorecard.scorecard_view(tmp_git, None, None, None, NOW)["lanes"]["bug"]
    assert bug[0]["signal"] == "conformance_missing" and bug[0]["slo"] is True and bug[0]["baseline"] is None
    assert (bug[0]["baseline_n"], bug[0]["delta"], bug[0]["verdict"], bug[0]["current"]) == (0, None, "same", 1.0)
    assert bug[1]["signal"] == "cost_usd" and bug[1]["slo"] is False and bug[1]["current_n"] == 1
    assert "bug" in scorecard.scorecard_table(scorecard.scorecard_view(tmp_git, None, None, None, NOW))
    only = scorecard.scorecard_view(tmp_git, "feature", None, None, NOW)
    assert list(only["lanes"]) == ["feature"]
    assert list(scorecard.scorecard_view(tmp_git, "bug", None, None, NOW)["lanes"]) == ["bug"]


def test_a_drop_in_a_higher_is_better_family_reads_worse(tmp_git, fake_runs):
    _baseline_runs(fake_runs)
    scorecard.freeze(tmp_git, None, None, None, False, NOW)
    fake_runs["r3"] = ("feature", "2026-11-05T10:00:00+00:00", {"gate_first_pass:lint": 0.25})
    rows = scorecard.scorecard_view(tmp_git, None, None, None, NOW)["lanes"]["feature"]
    row = next(r for r in rows if r["signal"] == "gate_first_pass:lint")
    assert (row["signal"], row["delta"], row["verdict"], row["slo"]) == ("gate_first_pass:lint", -0.25, "worse", False)


def test_scorecard_without_baseline_says_how_to_freeze(tmp_git, fake_runs):
    view = scorecard.scorecard_view(tmp_git, None, None, None, NOW)
    assert view == {"baseline": False, "lanes": {}}
    assert scorecard.scorecard_table(view) == "No baseline yet: run `agentflow scorecard --freeze` first."
    assert scorecard.load_baseline(tmp_git) is None


def test_freeze_refuses_a_symlinked_baseline(tmp_git, fake_runs):
    _baseline_runs(fake_runs)
    outside = tmp_git.parent / "outside.json"
    outside.write_text("untouched", encoding="utf-8")
    path = runs_root(tmp_git) / scorecard.BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside)
    with pytest.raises(SymlinkRefused):
        scorecard.freeze(tmp_git, None, None, None, True, NOW)
    assert outside.read_text(encoding="utf-8") == "untouched" and path.is_symlink()
    with pytest.raises(SymlinkRefused):
        scorecard.load_baseline(tmp_git)


def test_cli_scorecard_json(flow_repo, capsys):
    from agentflow.flow import main

    assert main(["scorecard", "--json", "--config", str(flow_repo.cfg.path)]) == 0
    assert json.loads(capsys.readouterr().out)["baseline"] is False
    assert main(["scorecard", "--config", str(flow_repo.cfg.path)]) == 0
    assert capsys.readouterr().out.strip() == "No baseline yet: run `agentflow scorecard --freeze` first."


def test_cli_freeze_refuses_a_second_freeze_without_replace(flow_repo, fake_runs, capsys):
    from agentflow.flow import main

    _baseline_runs(fake_runs)
    cfg = ["--config", str(flow_repo.cfg.path)]
    path = runs_root(flow_repo.root) / scorecard.BASELINE
    assert main(["scorecard", "--freeze", *cfg]) == 0 and path.is_file()
    capsys.readouterr()
    before = path.read_bytes()
    fake_runs["r3"] = ("feature", "2026-11-03T10:00:00+00:00", {"cost_usd": 9.0})
    assert main(["scorecard", "--freeze", *cfg]) != 0
    message = capsys.readouterr().out.strip()
    assert message.startswith("error:") and "--replace" in message and "\n" not in message
    assert path.read_bytes() == before
    assert main(["scorecard", "--freeze", "--replace", *cfg]) == 0
    assert "r3" in path.read_text(encoding="utf-8")
