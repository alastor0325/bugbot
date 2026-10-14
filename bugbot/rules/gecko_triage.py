# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from libmozdata.bugzilla import Bugzilla

from bugbot import logger, utils
from bugbot.bzcleaner import BzCleaner
from bugbot.hackbot_utils import api_url, start_or_retry_agent_run

AGENT = "gecko-triage"

# Every hackbot agent comments as this account and sets its needinfos from it.
HACKBOT_EMAIL = "hackbot@mozilla.tld"

# A needinfo the agent set is stale after this long with no change to the bug. The
# agent uses the same value (bugbug's agents/gecko-triage config.py).
STALE_NEEDINFO_DAYS = 14

# The settings tag on a team's settings bug: `[gecko-triage mode:… every:…]`.
_TAG = re.compile(r"\[gecko-triage((?:\s+[a-z]+:[a-z0-9]+)*)\s*\]", re.IGNORECASE)
_EVERY = re.compile(r"(\d+)([hd])")
MAX_EVERY_HOURS = 7 * 24


@dataclass(frozen=True)
class Team:
    """One team the agent triages for.

    The components must match the team's `teams/<team>/team.toml` in bugbug's
    agents/gecko-triage. Kept here rather than in configs/rules.json on purpose: the
    agent writes to the bug unattended, so widening its reach should take a code
    review. Nothing checks that the two lists agree; they are in separate repos.
    """

    components: tuple[tuple[str, str], ...]
    settings_bug: int | None
    allowlist: frozenset[str]
    default_every_hours: int = 24


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
        settings_bug=None,
        allowlist=frozenset({"alwu@mozilla.com"}),
    ),
}


@dataclass(frozen=True)
class Settings:
    mode: str
    every_hours: int


def parse_settings(
    whiteboard: str, editor: str | None, allowlist: frozenset[str], default_every: int
) -> Settings:
    """The mode and interval from a settings bug's whiteboard.

    Anything malformed, missing or edited by someone off the allowlist falls back to
    `mode:manual` and the default interval. One bad key makes the whole tag invalid.
    The agent reads the same tag the same way (bugbug's gecko_triage/settings.py).
    """
    fallback = Settings("manual", default_every)
    if not editor or editor.lower() not in {a.lower() for a in allowlist}:
        return fallback
    match = _TAG.search(whiteboard or "")
    if not match:
        return fallback
    values: dict[str, str] = {}
    for token in match.group(1).split():
        key, _, value = token.lower().partition(":")
        values.setdefault(key, value)
    mode = values.get("mode", "manual")
    if mode not in ("manual", "auto"):
        return fallback
    every = default_every
    if "every" in values:
        parsed = _EVERY.fullmatch(values["every"])
        if not parsed:
            return fallback
        every = int(parsed.group(1)) * (24 if parsed.group(2) == "d" else 1)
        if not 1 <= every <= MAX_EVERY_HOURS:
            return fallback
    return Settings(mode, every)


def last_whiteboard_editor(history: list[dict], creator: str | None) -> str | None:
    """Who changed the whiteboard last: the newest history entry with a whiteboard
    change, else the bug's creator, who set it at filing."""
    for entry in reversed(history):
        if any(c.get("field_name") == "whiteboard" for c in entry.get("changes", ())):
            return entry.get("who")
    return creator


def is_due(last_run: datetime | None, now: datetime, every_hours: int) -> bool:
    return last_run is None or now - last_run >= timedelta(hours=every_hours)


def hours_since(last_run: datetime | None, now: datetime, every_hours: int) -> int:
    """Whole hours back to the last run, rounded up; the interval on a first run."""
    if last_run is None:
        return every_hours
    return max(1, math.ceil((now - last_run).total_seconds() / 3600))


def dedupe_key(team: str, bug_id: int, last_change_time: str) -> str:
    """One run per state of a bug: a bug that has not changed since its last run is
    not run again, however often the query returns it."""
    return f"{AGENT}:{team}:{bug_id}:{last_change_time}"


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

    Runs from the hourly cron but acts only when the interval from the team's
    settings bug has passed since its last run. It then picks the open defects in the
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
        self.left_for_next_run = 0

    def description(self):
        return f"[Using AI] Bugs sent to the gecko-triage agent for the {self.team_name} team"

    def columns(self):
        return ["id", "summary", "product", "component", "run_id"]

    def has_default_products(self):
        return False

    def has_product_component(self):
        return True

    def settings(self) -> Settings:
        fallback = Settings("manual", self.team.default_every_hours)
        if self.team.settings_bug is None:
            return fallback
        data: dict = {}

        def bughandler(bug, data):
            data["whiteboard"] = bug.get("whiteboard", "")
            data["creator"] = bug.get("creator")

        def historyhandler(bug, data):
            data["history"] = bug.get("history", [])

        try:
            Bugzilla(
                [self.team.settings_bug],
                include_fields=["id", "whiteboard", "creator"],
                bughandler=bughandler,
                bugdata=data,
                historyhandler=historyhandler,
                historydata=data,
            ).get_data().wait()
        except Exception:
            logger.exception("Cannot read settings bug %s", self.team.settings_bug)
            return fallback
        editor = last_whiteboard_editor(data.get("history", []), data.get("creator"))
        return parse_settings(
            data.get("whiteboard", ""),
            editor,
            self.team.allowlist,
            self.team.default_every_hours,
        )

    def query(self, since_hours: int) -> dict:
        params = {
            "include_fields": ["id", "summary", "last_change_time"],
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
        clause("setters.login_name", "equals", HACKBOT_EMAIL)
        clause("delta_ts", "lessthan", f"-{STALE_NEEDINFO_DAYS}d")
        close()
        close()
        return params

    def get_bz_params(self, date):
        return self.query(
            hours_since(self.last_run, self.now, self.current.every_hours)
        )

    def handle_bug(self, bug, data):
        # `bughandler` rebuilds each row from id and summary, so the field the
        # dedupe key needs is carried over by hand.
        data[str(bug["id"])] = {"last_change_time": bug["last_change_time"]}
        return bug

    def get_bugs(self, date="today", bug_ids=[], chunk_size=None):
        return self.trigger_runs(
            super().get_bugs(date=date, bug_ids=bug_ids, chunk_size=chunk_size)
        )

    def run(self):
        self.now = datetime.now(timezone.utc)
        self.current = self.settings()
        self.last_run = load_last_run(self.team_name)
        if not is_due(self.last_run, self.now, self.current.every_hours):
            logger.info("%s (%s) is not due yet", AGENT, self.team_name)
            return
        super().run()
        # Bugs left over the cap must come back on the next cron, so the window only
        # moves on once every bug in it has a run.
        if not self.dryrun and not self.left_for_next_run:
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
            key = dedupe_key(self.team_name, int(bugid), bug["last_change_time"])
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
        return started

    def get_extra_for_template(self):
        return {"left_for_next_run": self.left_for_next_run, "team": self.team_name}


if __name__ == "__main__":
    for name in TEAMS:
        GeckoTriage(name).run()
