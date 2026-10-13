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
