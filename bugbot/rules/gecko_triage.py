# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bugbot import logger, utils
from bugbot.bzcleaner import BzCleaner
from bugbot.hackbot_utils import api_url, start_or_retry_agent_run

AGENT = "gecko-triage"

# Every hackbot agent comments as this account and sets its needinfos from it.
HACKBOT_EMAIL = "hackbot@mozilla.tld"

# A needinfo the agent set is stale after this long with no change to the bug. The
# agent uses the same value (bugbug's agents/gecko-triage config.py).
STALE_NEEDINFO_DAYS = 14


@dataclass(frozen=True)
class Team:
    """One team the agent triages for.

    The components must match the team's `teams/<team>/team.toml` in bugbug's
    agents/gecko-triage. Kept here rather than in configs/rules.json on purpose: the
    agent writes to the bug unattended, so widening its reach should take a code
    review. Nothing checks that the two lists agree; they are in separate repos.
    """

    components: tuple[tuple[str, str], ...]


TEAMS = {
    "media": Team(
        components=(
            ("Core", "Audio/Video"),
            ("Core", "Audio/Video: cubeb"),
            ("Core", "Audio/Video: GMP"),
            ("Core", "Audio/Video: MediaStreamGraph"),
            ("Core", "Audio/Video: Playback"),
            ("Core", "Audio/Video: Recording"),
            ("Core", "Web Audio"),
            ("Core", "Audio/Video: Web Codecs"),
        ),
    ),
}


def is_due(last_run: datetime | None, now: datetime, every_hours: int) -> bool:
    return last_run is None or now - last_run >= timedelta(hours=every_hours)


def hours_since(last_run: datetime | None, now: datetime, every_hours: int) -> int:
    """Whole hours back to the last run, rounded up; the interval on a first run."""
    if last_run is None:
        return every_hours
    return max(1, math.ceil((now - last_run).total_seconds() / 3600))


def dedupe_key(
    team: str, bug_id: int, last_change_time: str, stale: bool = False
) -> str:
    """One run per state of a bug: a bug that has not changed since its last run is
    not run again, however often the query returns it.

    A stale needinfo is a new state with no new change time (the run that set the
    needinfo already used it), so it gets its own key.
    """
    key = f"{AGENT}:{team}:{bug_id}:{last_change_time}"
    return f"{key}:stale" if stale else key


def bot_setters(env=os.environ) -> tuple[str, ...]:
    """The accounts whose needinfos are the agent's: hackbot and the `BOT_ALIASES`.

    `BOT_ALIASES` holds comma-separated logins of a temporary account that posted the
    agent's results, such as a team bot in a pilot. It stays out of the repo, as in the
    agent (bugbug's agents/gecko-triage config.py).
    """
    aliases = (a.strip().lower() for a in env.get("BOT_ALIASES", "").split(","))
    return (HACKBOT_EMAIL, *(a for a in aliases if a))


def has_stale_bot_needinfo(
    bug: dict, now: datetime, setters: tuple[str, ...] = (HACKBOT_EMAIL,)
) -> bool:
    for flag in bug.get("flags") or ():
        if flag.get("name") != "needinfo" or flag.get("status") != "?":
            continue
        if str(flag.get("setter", "")).lower() not in setters:
            continue
        set_at = flag.get("creation_date")
        if set_at and now - datetime.fromisoformat(
            set_at.replace("Z", "+00:00")
        ) >= timedelta(days=STALE_NEEDINFO_DAYS):
            return True
    return False


def state_path() -> str:
    return os.path.join(
        utils.get_config("common", "cache"), "gecko_triage_last_run.json"
    )


def _load_state() -> dict:
    try:
        with open(state_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_last_run(team: str) -> datetime | None:
    value = _load_state().get(team)
    return datetime.fromisoformat(value) if value else None


def save_last_run(team: str, when: datetime) -> None:
    state = _load_state()
    state[team] = when.isoformat()
    os.makedirs(os.path.dirname(state_path()) or ".", exist_ok=True)
    with open(state_path(), "w") as f:
        json.dump(state, f)


class GeckoTriage(BzCleaner):
    """Ask hackbot's gecko-triage agent to triage one team's bugs.

    Runs from the hourly cron but acts only when `every_hours` (configs/rules.json)
    have passed since its last run. It then picks the open defects in the
    team's components that changed since that run, or that wait on a needinfo the
    agent set and has gone stale. The dedupe key keeps one run per state of a bug.

    The agent's own comment also changes the bug, so the next interval picks the bug
    again; the agent's preflight then ends that run at no model cost.

    This rule only starts runs: the agent writes to the bug itself.
    """

    def __init__(self, team: str = "media") -> None:
        self.team_name = team
        self.team = TEAMS[team]
        super().__init__()
        self.max_triggers = self.get_config("max_triggers", 20)
        self.every_hours = self.get_config("every_hours", 24)
        self.now = datetime.now(timezone.utc)
        self.left_for_next_run = 0
        self.setters = bot_setters()

    def description(self):
        return f"[Using AI] Bugs sent to the gecko-triage agent for the {self.team_name} team"

    def columns(self):
        return ["id", "summary", "product", "component", "run_id"]

    def has_default_products(self):
        return False

    def has_product_component(self):
        return True

    def query(self, since_hours: int) -> dict:
        params = {
            "include_fields": ["id", "summary", "last_change_time", "flags"],
            "bug_type": "defect",
            "resolution": "---",
        }
        utils.add_prod_comp_to_query(
            params, [f"{p}::{c}" for p, c in self.team.components]
        )

        def clause(field, op, value):
            n = utils.get_last_field_num(params)
            params.update({f"f{n}": field, f"o{n}": op, f"v{n}": value})

        def group(join):
            n = utils.get_last_field_num(params)
            params.update({f"j{n}": join, f"f{n}": "OP"})

        def close():
            params[f"f{utils.get_last_field_num(params)}"] = "CP"

        # Changed since the last run, or waiting on a stale needinfo the agent set.
        # BMO misreads an ISO time with `T` and `Z`, so the time goes in relative.
        # The two flag conditions may match two flags (BMO allows AND_G only on one
        # field); the agent's preflight checks the stale needinfo exactly.
        group("OR")
        clause("delta_ts", "greaterthan", f"-{since_hours}h")
        group("AND")
        clause("flagtypes.name", "equals", "needinfo?")
        clause("setters.login_name", "anyexact", ",".join(self.setters))
        clause("delta_ts", "lessthan", f"-{STALE_NEEDINFO_DAYS}d")
        close()
        close()
        return params

    def get_bz_params(self, date):
        return self.query(hours_since(self.last_run, self.now, self.every_hours))

    def handle_bug(self, bug, data):
        # `bughandler` rebuilds each row from id and summary, so the field the
        # dedupe key needs is carried over by hand.
        data[str(bug["id"])] = {
            "last_change_time": bug["last_change_time"],
            "stale": has_stale_bot_needinfo(bug, self.now, self.setters),
        }
        return bug

    def get_bugs(self, date="today", bug_ids=[], chunk_size=None):
        return self.trigger_runs(
            super().get_bugs(date=date, bug_ids=bug_ids, chunk_size=chunk_size)
        )

    def run(self):
        self.now = datetime.now(timezone.utc)
        self.last_run = load_last_run(self.team_name)
        if not is_due(self.last_run, self.now, self.every_hours):
            logger.info("%s (%s) is not due yet", AGENT, self.team_name)
            return
        self.completed = False
        super().run()
        # BzCleaner.run logs and swallows errors, and bugs left over the cap must come
        # back on the next cron, so the window only moves on after a complete pass.
        if not self.dryrun and self.completed and not self.left_for_next_run:
            save_last_run(self.team_name, self.now)

    def trigger_runs(self, bugs: dict) -> dict:
        """Start a run per bug, and return the ones this call started.

        A bug whose run already exists for this interval, one over the cap, or one
        whose start failed is dropped from the report.
        """
        if self.dryrun:
            logger.info(
                "Dry run: would start %s (%s) for bugs %s",
                AGENT,
                self.team_name,
                ", ".join(bugs) or "(none)",
            )
            for bug in bugs.values():
                bug["run_id"] = "(dry run)"
            return bugs

        if not api_url():
            raise ValueError(f"HACKBOT_API_URL is not set; cannot start {AGENT} runs")

        started: dict = {}
        failed = 0
        items = list(bugs.items())
        for index, (bugid, bug) in enumerate(items):
            if len(started) >= self.max_triggers:
                logger.warning(
                    "Reached the cap of %d %s runs; the rest wait for the next run",
                    self.max_triggers,
                    AGENT,
                )
                self.left_for_next_run = failed + len(items) - index
                return started
            key = dedupe_key(
                self.team_name,
                int(bugid),
                bug["last_change_time"],
                stale=bug.get("stale", False),
            )
            try:
                run = start_or_retry_agent_run(
                    AGENT, {"bug_id": int(bugid), "team": self.team_name}, key
                )
            except Exception:
                logger.exception("Failed to start %s for bug %s", AGENT, bugid)
                failed += 1
                continue
            if run.created:
                bug["run_id"] = run.run_id
                started[bugid] = bug

        self.left_for_next_run = failed
        self.completed = True
        return started

    def get_extra_for_template(self):
        return {"left_for_next_run": self.left_for_next_run, "team": self.team_name}


if __name__ == "__main__":
    for name in TEAMS:
        GeckoTriage(name).run()
