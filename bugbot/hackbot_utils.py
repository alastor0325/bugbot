# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import os
from dataclasses import dataclass

import requests

from bugbot import utils

DEFAULT_API_URL = "https://hackbot-api.moz.tools"

TIMEOUT = 60


def api_url() -> str:
    """The hackbot API base URL.

    hackbot runs the LLM agents (see mozilla/bugbug's services/hackbot-api). We
    only ever start a run from here: the agent applies its own findings to
    Bugzilla and reports the result to Slack itself, so nothing comes back to us.

    Defaults to the production deployment so the cron host needs no extra
    environment, the way ``BUGBUG_HTTP_SERVER`` does. Override
    ``HACKBOT_API_URL`` to point at another deployment — that is the name
    hackbot's other clients already use (the pulse listener's setting and the
    console's deploy env), so there is one name to remember across all three.

    Read per call rather than at import so the override is settable by anything
    that configures the environment after this module loads.
    """
    return os.environ.get("HACKBOT_API_URL", DEFAULT_API_URL)


@dataclass(frozen=True)
class AgentRun:
    """A run hackbot answered with, new or already existing.

    ``created`` is False when a run already held the dedupe key: hackbot then
    answers 200 with that run instead of starting another (201).
    """

    run_id: str
    created: bool


def _base_url() -> str:
    base_url = api_url()
    if not base_url:
        raise ValueError("HACKBOT_API_URL is not set")
    return base_url.rstrip("/")


def _headers() -> dict:
    return {"X-API-Key": utils.get_login_info()["hackbot_api_key"]}


def start_agent_run(
    agent: str, inputs: dict, dedupe_key: str | None = None
) -> AgentRun:
    """Start a hackbot agent run, or find the one already holding ``dedupe_key``.

    Args:
        agent: The agent to run, e.g. `frontend-triage`.
        inputs: The agent's per-run inputs, e.g. `{"bug_id": 1234567}`.
        dedupe_key: Forwarded as `?dedupe_key=`. A key belongs to one run of the
            agent for good; a second request with the same key gets that run back,
            whatever its status, and never starts another.

    Returns:
        The run and whether this request created it.
    """
    response = requests.post(
        f"{_base_url()}/agents/{agent}/runs",
        headers=_headers(),
        params={"dedupe_key": dedupe_key} if dedupe_key is not None else None,
        json=inputs,
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    return AgentRun(
        run_id=response.json()["run_id"],
        created=response.status_code == requests.codes.created,
    )


def trigger_agent_run(agent: str, inputs: dict, dedupe_key: str | None = None) -> str:
    """Start a hackbot agent run.

    Args:
        agent: The agent to run, e.g. `frontend-triage`.
        inputs: The agent's per-run inputs, e.g. `{"bug_id": 1234567}`.
        dedupe_key: Forwarded as `?dedupe_key=`, see :func:`start_agent_run`.

    Returns:
        The id of the run, created or existing. Use :func:`start_agent_run` to
        tell the two apart.
    """
    return start_agent_run(agent, inputs, dedupe_key).run_id
