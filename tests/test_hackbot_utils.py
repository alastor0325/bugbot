# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import pytest
import requests
import responses

from bugbot import hackbot_utils

API_URL = "https://hackbot.example"
RUNS_URL = f"{API_URL}/agents/frontend-triage/runs"
RUN_ID = "0f2e9d3c-1b4a-4c5d-8e6f-7a8b9c0d1e2f"


@pytest.fixture(autouse=True)
def hackbot_env(monkeypatch):
    monkeypatch.setenv("HACKBOT_API_URL", API_URL)
    monkeypatch.setattr(
        hackbot_utils.utils, "get_login_info", lambda: {"hackbot_api_key": "key"}
    )


def _run_response(status, run_id=RUN_ID):
    return responses.post(RUNS_URL, status=status, json={"run_id": run_id})


@responses.activate
def test_trigger_agent_run_posts_the_inputs_and_returns_the_run_id():
    _run_response(201)

    assert hackbot_utils.trigger_agent_run("frontend-triage", {"bug_id": 1}) == RUN_ID

    request = responses.calls[0].request
    assert request.url == RUNS_URL
    assert request.headers["X-API-Key"] == "key"
    assert request.body == b'{"bug_id": 1}'


@responses.activate
def test_trigger_agent_run_sends_no_dedupe_key_unless_given():
    # The existing caller passes none and must keep getting a fresh run each time.
    _run_response(201)
    hackbot_utils.trigger_agent_run("frontend-triage", {"bug_id": 1})
    assert "dedupe_key" not in responses.calls[0].request.url


@responses.activate
def test_trigger_agent_run_forwards_the_dedupe_key():
    _run_response(201)
    hackbot_utils.trigger_agent_run("frontend-triage", {"bug_id": 1}, "bug:1:x")
    assert responses.calls[0].request.params == {"dedupe_key": "bug:1:x"}


@responses.activate
def test_trigger_agent_run_returns_the_existing_run_for_a_held_key():
    # 200 is not an error: the key already belongs to a run, and that run is
    # what the caller gets.
    _run_response(200)
    assert (
        hackbot_utils.trigger_agent_run("frontend-triage", {"bug_id": 1}, "bug:1:x")
        == RUN_ID
    )


@responses.activate
def test_start_agent_run_tells_a_new_run_from_an_existing_one():
    _run_response(201)
    _run_response(200)

    first = hackbot_utils.start_agent_run("frontend-triage", {"bug_id": 1}, "k")
    second = hackbot_utils.start_agent_run("frontend-triage", {"bug_id": 1}, "k")

    assert first == hackbot_utils.AgentRun(run_id=RUN_ID, created=True)
    assert second == hackbot_utils.AgentRun(run_id=RUN_ID, created=False)


@responses.activate
def test_start_agent_run_raises_on_an_error_reply():
    responses.post(RUNS_URL, status=422, json={"detail": "bad inputs"})
    with pytest.raises(requests.HTTPError):
        hackbot_utils.start_agent_run("frontend-triage", {"bug_id": "x"})


# --- retrying a run that failed while holding its key ------------------- #


def _status_response(run_id, status):
    return responses.get(
        f"{API_URL}/runs/{run_id}", json={"run_id": run_id, "status": status}
    )


def _posted_keys():
    return [
        call.request.params.get("dedupe_key")
        for call in responses.calls
        if call.request.method == "POST"
    ]


def _status_lookups():
    return [
        call.request.url for call in responses.calls if call.request.method == "GET"
    ]


@responses.activate
def test_get_run_status_reads_the_run():
    _status_response(RUN_ID, "failed")
    assert hackbot_utils.get_run_status(RUN_ID) == "failed"
    assert responses.calls[0].request.headers["X-API-Key"] == "key"


@responses.activate
def test_a_new_run_needs_no_status_lookup():
    _run_response(201)

    run = hackbot_utils.start_or_retry_agent_run("frontend-triage", {"bug_id": 1}, "k")

    assert run == hackbot_utils.AgentRun(run_id=RUN_ID, created=True)
    assert _posted_keys() == ["k"]
    assert _status_lookups() == []


@responses.activate
def test_a_key_held_by_a_run_that_did_not_fail_is_left_alone():
    # Running, succeeded and timed out runs all keep their key: only `failed`
    # is worth another attempt.
    for status in ("pending", "running", "succeeded", "timed_out"):
        responses.reset()
        _run_response(200)
        _status_response(RUN_ID, status)

        run = hackbot_utils.start_or_retry_agent_run(
            "frontend-triage", {"bug_id": 1}, "k"
        )

        assert run == hackbot_utils.AgentRun(run_id=RUN_ID, created=False), status
        assert _posted_keys() == ["k"], status


@responses.activate
def test_a_key_held_by_a_failed_run_is_retried_under_a_suffixed_key():
    _run_response(200, "run-failed")
    _status_response("run-failed", "failed")
    _run_response(201, "run-retry")

    run = hackbot_utils.start_or_retry_agent_run("frontend-triage", {"bug_id": 1}, "k")

    assert run == hackbot_utils.AgentRun(run_id="run-retry", created=True)
    assert _posted_keys() == ["k", "k:r2"]
    # Every attempt carries the same inputs.
    assert {
        call.request.body for call in responses.calls if call.request.method == "POST"
    } == {b'{"bug_id": 1}'}


@responses.activate
def test_a_retry_key_already_held_by_a_live_run_is_returned():
    # A previous cron pass already retried: its run is the answer, not a third one.
    _run_response(200, "run-failed")
    _status_response("run-failed", "failed")
    _run_response(200, "run-retry")
    _status_response("run-retry", "running")

    run = hackbot_utils.start_or_retry_agent_run("frontend-triage", {"bug_id": 1}, "k")

    assert run == hackbot_utils.AgentRun(run_id="run-retry", created=False)
    assert _posted_keys() == ["k", "k:r2"]


@responses.activate
def test_retries_stop_after_two_attempts():
    for suffix in ("", ":r2", ":r3"):
        _run_response(200, f"run{suffix}")
        _status_response(f"run{suffix}", "failed")

    run = hackbot_utils.start_or_retry_agent_run("frontend-triage", {"bug_id": 1}, "k")

    # The last failed run is handed back as is; nothing is posted under `:r4`.
    assert run == hackbot_utils.AgentRun(run_id="run:r3", created=False)
    assert _posted_keys() == ["k", "k:r2", "k:r3"]
