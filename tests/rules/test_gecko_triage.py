# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from datetime import datetime, timedelta, timezone

import pytest

from bugbot.hackbot_utils import AgentRun
from bugbot.rules.gecko_triage import (
    AGENT,
    STALE_NEEDINFO_DAYS,
    TEAMS,
    GeckoTriage,
    dedupe_key,
    hours_since,
    is_due,
    load_last_run,
    save_last_run,
)

NOW = datetime(2026, 10, 15, 13, 30, tzinfo=timezone.utc)


def _rule(team="media", **over):
    rule = GeckoTriage(team)
    for key, value in over.items():
        setattr(rule, key, value)
    return rule


LAST_CHANGE = "2026-10-15T10:00:00Z"


def _bug(bug_id=1, creator="reporter@example.org"):
    return {
        "id": bug_id,
        "last_change_time": LAST_CHANGE,
        "summary": "Video freezes",
        "creator": creator,
        "product": "Core",
        "component": "Audio/Video: Playback",
        "groups": [],
    }


# --- the interval ------------------------------------------------------------ #


def test_the_interval_comes_from_the_rule_config():
    assert _rule().every_hours == 24


# --- when the rule acts, and the dedupe key ----------------------------------- #


def test_the_rule_is_due_on_its_first_run_and_after_the_interval():
    assert is_due(None, NOW, 24)
    assert not is_due(NOW - timedelta(hours=23), NOW, 24)
    assert is_due(NOW - timedelta(hours=24), NOW, 24)


def test_hours_since_rounds_up_and_falls_back_to_the_interval():
    assert hours_since(NOW - timedelta(hours=5, minutes=1), NOW, 24) == 6
    assert hours_since(None, NOW, 24) == 24


def test_one_key_per_bug_team_and_change():
    key = dedupe_key("media", 1, "2026-10-15T10:00:00Z")
    assert key == dedupe_key("media", 1, "2026-10-15T10:00:00Z")
    assert key != dedupe_key("media", 2, "2026-10-15T10:00:00Z")
    assert key != dedupe_key("gfx", 1, "2026-10-15T10:00:00Z")
    assert key != dedupe_key("media", 1, "2026-10-15T11:00:00Z")


# --- which bugs are picked ---------------------------------------------------- #


def _values(params):
    return [v for k, v in params.items() if k.startswith("v")]


def test_queries_every_team_component_with_its_product():
    params = _rule().query(6)
    values = _values(params)
    for product, component in TEAMS["media"].components:
        assert product in values and component in values


def test_queries_open_defects_only():
    params = _rule().query(6)
    assert params["resolution"] == "---"
    assert params["bug_type"] == "defect"


def test_queries_bugs_changed_since_the_last_run_or_on_a_stale_reporter_needinfo():
    params = _rule().query(6)
    values = _values(params)
    # BMO misreads an ISO time with `T` and `Z`; a relative value has no time zone.
    assert "-6h" in values
    assert "needinfo?" in values
    assert f"-{STALE_NEEDINFO_DAYS}d" in values
    assert "requestees.login_name" in params.values()
    assert "%reporter%" in values
    # Who set the needinfo does not matter any more.
    assert "setters.login_name" not in params.values()
    # BMO allows AND_G only on one field, so the flag conditions are a plain AND;
    # handle_bug checks the stale needinfo exactly.
    assert "AND_G" not in params.values()


def test_queries_the_reporter():
    assert "creator" in _rule().query(6)["include_fields"]


def test_keeps_a_bot_filed_bug():
    # Crash and fuzzing bugs are filed by bots and still need severity and priority.
    assert _rule().handle_bug(_bug(creator="bot@mozilla.tld"), {}) is not None


def test_an_unknown_team_is_refused():
    with pytest.raises(KeyError):
        GeckoTriage("nope")


# --- starting runs ------------------------------------------------------------ #


@pytest.fixture
def started(monkeypatch):
    calls = []

    def fake_start(agent, inputs, key):
        calls.append((agent, inputs, key))
        return AgentRun(run_id=f"run-{inputs['bug_id']}", created=True)

    monkeypatch.setattr(
        "bugbot.rules.gecko_triage.start_or_retry_agent_run", fake_start
    )
    return calls


def _bugs(*ids):
    return {str(i): _bug(i) for i in ids}


def test_starts_one_run_per_bug_with_the_team(started):
    _rule(dryrun=False).trigger_runs(_bugs(1, 2))
    assert [(a, i) for a, i, _ in started] == [
        (AGENT, {"bug_id": 1, "team": "media"}),
        (AGENT, {"bug_id": 2, "team": "media"}),
    ]
    assert started[0][2] == dedupe_key("media", 1, LAST_CHANGE)


def test_dry_run_starts_nothing(started):
    bugs = _rule(dryrun=True).trigger_runs(_bugs(1))
    assert started == [] and set(bugs) == {"1"}


def test_caps_the_number_of_new_runs(started):
    rule = _rule(dryrun=False, max_triggers=1)
    bugs = rule.trigger_runs(_bugs(1, 2, 3))
    assert len(started) == 1 and set(bugs) == {"1"}
    assert rule.left_for_next_run == 2


def test_a_bug_whose_run_exists_is_not_left_for_next_run(monkeypatch):
    monkeypatch.setattr(
        "bugbot.rules.gecko_triage.start_or_retry_agent_run",
        lambda agent, inputs, key: AgentRun(run_id="old", created=False),
    )
    rule = _rule(dryrun=False)
    rule.trigger_runs(_bugs(1, 2))
    assert rule.left_for_next_run == 0


def test_an_existing_run_is_not_reported_again(monkeypatch):
    monkeypatch.setattr(
        "bugbot.rules.gecko_triage.start_or_retry_agent_run",
        lambda agent, inputs, key: AgentRun(run_id="old", created=False),
    )
    bugs = _rule(dryrun=False).trigger_runs(_bugs(1))
    assert bugs == {}


def test_a_failing_start_does_not_stop_the_others(monkeypatch):
    def flaky(agent, inputs, key):
        if inputs["bug_id"] == 1:
            raise RuntimeError("boom")
        return AgentRun(run_id="run-2", created=True)

    monkeypatch.setattr("bugbot.rules.gecko_triage.start_or_retry_agent_run", flaky)
    bugs = _rule(dryrun=False).trigger_runs(_bugs(1, 2))
    assert set(bugs) == {"2"}


def test_the_last_run_is_kept_per_team(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "bugbot.rules.gecko_triage.state_path", lambda: tmp_path / "s.json"
    )
    assert load_last_run("media") is None
    save_last_run("media", NOW)
    assert load_last_run("media") == NOW
    assert load_last_run("gfx") is None


def test_the_last_change_time_reaches_trigger_runs():
    # `bughandler` keeps only id/summary/product/component, so the dedupe key's
    # field has to be carried over by hand.
    rule = _rule(dryrun=True)
    rule.cache.set_dry_run(True)
    data: dict = {}
    rule.bughandler(_bug(), data)
    assert data["1"]["last_change_time"] == LAST_CHANGE
    assert set(rule.columns()) <= set(data["1"]) | {"run_id"}


@pytest.mark.parametrize(("left", "saved"), [(0, True), (3, False)])
def test_the_last_run_moves_on_only_when_nothing_is_left(
    tmp_path, monkeypatch, left, saved
):
    # Bugs over the cap must come back on the next cron, so the window stays open.
    monkeypatch.setattr(
        "bugbot.rules.gecko_triage.state_path", lambda: tmp_path / "s.json"
    )

    def fake_run(self):
        self.completed = True
        self.left_for_next_run = left

    monkeypatch.setattr("bugbot.bzcleaner.BzCleaner.run", fake_run)
    rule = _rule(dryrun=False)
    rule.run()
    assert (load_last_run("media") is not None) == saved


def test_a_failed_run_keeps_the_window_open(tmp_path, monkeypatch):
    # BzCleaner.run logs and swallows errors, so a failure must not move the window.
    monkeypatch.setattr(
        "bugbot.rules.gecko_triage.state_path", lambda: tmp_path / "s.json"
    )
    monkeypatch.setattr("bugbot.bzcleaner.BzCleaner.run", lambda self: None)
    _rule(dryrun=False).run()
    assert load_last_run("media") is None


def _with_needinfo(bug, requestee, when, setter="someone@example.org"):
    flag = {
        "name": "needinfo",
        "status": "?",
        "setter": setter,
        "requestee": requestee,
        "creation_date": when,
    }
    return dict(bug, flags=[flag])


def _stale(bug):
    data: dict = {}
    _rule(now=NOW).handle_bug(bug, data)
    return data[str(bug["id"])]["stale"]


@pytest.mark.parametrize("setter", ["hackbot@mozilla.tld", "dev@mozilla.com"])
def test_a_stale_reporter_needinfo_set_by_anyone_is_stale(setter):
    bug = _with_needinfo(
        _bug(), "Reporter@Example.org", "2026-09-01T10:00:00Z", setter=setter
    )
    assert _stale(bug) is True


def test_a_stale_needinfo_on_someone_else_is_not_stale():
    bug = _with_needinfo(_bug(), "dev@mozilla.com", "2026-09-01T10:00:00Z")
    assert _stale(bug) is False


def test_a_fresh_reporter_needinfo_is_not_stale():
    bug = _with_needinfo(_bug(), "reporter@example.org", "2026-10-14T10:00:00Z")
    assert _stale(bug) is False


def test_an_answered_reporter_needinfo_is_not_stale():
    bug = _with_needinfo(_bug(), "reporter@example.org", "2026-09-01T10:00:00Z")
    bug["flags"][0]["status"] = "+"
    assert _stale(bug) is False


def _needinfo(requestee, when, setter="someone@example.org"):
    return {
        "name": "needinfo",
        "status": "?",
        "setter": setter,
        "requestee": requestee,
        "creation_date": when,
    }


def test_an_open_needinfo_on_someone_else_too_is_not_stale():
    bug = dict(
        _bug(),
        flags=[
            _needinfo("reporter@example.org", "2026-09-01T10:00:00Z"),
            _needinfo("dev@mozilla.com", "2026-10-14T10:00:00Z"),
        ],
    )
    assert _stale(bug) is False


def test_a_self_set_reporter_needinfo_is_not_stale():
    bug = _with_needinfo(
        _bug(),
        "reporter@example.org",
        "2026-09-01T10:00:00Z",
        setter="Reporter@Example.org",
    )
    assert _stale(bug) is False


def test_a_fresh_second_reporter_needinfo_is_not_stale():
    # The agent's clock starts at the newest open needinfo on the reporter.
    bug = dict(
        _bug(),
        flags=[
            _needinfo("reporter@example.org", "2026-09-01T10:00:00Z"),
            _needinfo("reporter@example.org", "2026-10-14T10:00:00Z"),
        ],
    )
    assert _stale(bug) is False


def test_two_stale_reporter_needinfos_are_stale():
    bug = dict(
        _bug(),
        flags=[
            _needinfo("reporter@example.org", "2026-09-01T10:00:00Z"),
            _needinfo("reporter@example.org", "2026-09-20T10:00:00Z"),
        ],
    )
    assert _stale(bug) is True


def test_a_bot_only_change_keeps_the_key(started):
    # BugBot's own field changes move only last_change_time, so they must not
    # start a new model run.
    rule = _rule(dryrun=False, now=NOW)
    keys = []
    for last_change in (LAST_CHANGE, "2026-10-15T12:00:00Z"):
        data: dict = {}
        bug = dict(
            _bug(), last_change_time=last_change, last_change_time_non_bot=LAST_CHANGE
        )
        rule.handle_bug(bug, data)
        rule.trigger_runs({"1": dict(bug, **data["1"])})
        keys.append(started[-1][2])
    assert keys[0] == keys[1] == dedupe_key("media", 1, LAST_CHANGE)


def test_the_key_falls_back_to_last_change_time():
    data: dict = {}
    _rule(now=NOW).handle_bug(_bug(), data)
    assert data["1"]["last_change_time"] == LAST_CHANGE


def test_queries_the_non_bot_change_time():
    assert "last_change_time_non_bot" in _rule().query(6)["include_fields"]


def test_a_stale_needinfo_gets_its_own_key():
    # The run that set the needinfo already used the bug's current change time.
    assert dedupe_key("media", 1, LAST_CHANGE, stale=True).endswith(":stale")
    assert dedupe_key("media", 1, LAST_CHANGE) != dedupe_key(
        "media", 1, LAST_CHANGE, stale=True
    )


def test_a_stale_bug_is_started_under_the_stale_key(started):
    bugs = _bugs(1)
    bugs["1"]["stale"] = True
    _rule(dryrun=False).trigger_runs(bugs)
    assert started[0][2] == dedupe_key("media", 1, LAST_CHANGE, stale=True)
