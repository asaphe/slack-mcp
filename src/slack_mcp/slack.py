"""Slack Web API client with lazily loaded user and channel directories."""

from __future__ import annotations

import base64
import calendar
import csv
import io
import json
import re
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

API = "https://slack.com/api/"
FILES_HOST = "files.slack.com"
MAX_RETRIES = 2


class SlackError(RuntimeError):
    pass


class Slack:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, sleep=time.sleep) -> None:
        self.http = httpx.Client(base_url=API, timeout=30, transport=transport, headers={"Authorization": f"Bearer {token}"})
        self.sleep = sleep
        self._users: dict[str, dict] | None = None
        self._handles: dict[str, str] = {}
        self._names: dict[str, set[str]] = {}
        self._channels: dict[str, dict] | None = None
        self._ims: dict[str, str] | None = None
        self._me: str | None = None
        # Tools run in worker threads; directory state is built off-lock and published under it.
        self._lock = threading.RLock()

    def call(self, method: str, **params: Any) -> dict:
        """Call a Web API method. Structured arguments go as JSON, scalar-only calls as a form."""
        params = {k: v for k, v in params.items() if v is not None}
        structured = any(isinstance(v, (dict, list)) for v in params.values())
        form = None if structured else {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items()}
        for attempt in range(MAX_RETRIES + 1):
            resp = self.http.post(method, json=params if structured else None, data=form)
            if resp.status_code == 429 and attempt < MAX_RETRIES:
                self.sleep(int(resp.headers.get("Retry-After", "1")))
                continue
            resp.raise_for_status()
            break
        data = resp.json()
        if not data.get("ok"):
            needed = data.get("needed")
            hint = f" (token lacks scope: {needed})" if needed else ""
            raise SlackError(f"{method}: {data.get('error', 'unknown_error')}{hint}")
        data.pop("ok", None)
        data.pop("warning", None)
        if not data.get("response_metadata", {}).get("next_cursor"):
            data.pop("response_metadata", None)
        return data

    def paginate(self, method: str, key: str, **params: Any) -> list[dict]:
        items: list[dict] = []
        cursor = None
        while True:
            data = self.call(method, cursor=cursor, **params)
            items.extend(data.get(key, []))
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return items

    def download(self, url: str) -> str:
        """Fetch a file body; the token is only ever sent to Slack's file host."""
        if httpx.URL(url).host != FILES_HOST or not url.startswith("https://"):
            raise SlackError(f"refusing to send the token to a non-Slack file URL: {url}")
        resp = self.http.get(url, follow_redirects=False)
        resp.raise_for_status()
        return resp.text

    def me(self) -> str:
        if self._me is None:
            self._me = self.call("auth.test")["user_id"]
        return self._me

    def _load_users(self) -> None:
        with self._lock:
            if self._users is not None:
                return
        fetched = self.paginate("users.list", "members", limit=200)
        users: dict[str, dict] = {}
        handles: dict[str, str] = {}
        names: dict[str, set[str]] = {}
        for user in fetched:
            users[user["id"]] = user
            self._index_user(user, handles, names)
        with self._lock:
            if self._users is None:
                self._users, self._handles, self._names = users, handles, names

    def users(self) -> dict[str, dict]:
        """A snapshot of the user directory, safe to iterate while other tools update it."""
        self._load_users()
        with self._lock:
            return dict(self._users or {})

    @staticmethod
    def _index_user(user: dict, handles: dict[str, str], names: dict[str, set[str]]) -> None:
        if user.get("name"):
            handles[user["name"].lower()] = user["id"]
        for name in (user.get("profile", {}).get("display_name"), user.get("real_name")):
            if name:
                names.setdefault(name.lower(), set()).add(user["id"])

    def user(self, user_id: str) -> dict | None:
        self._load_users()
        with self._lock:
            known = (self._users or {}).get(user_id)
        if known:
            return known
        try:
            fetched = self.call("users.info", user=user_id)["user"]
        except SlackError:
            return None
        with self._lock:
            users = self._users if self._users is not None else {}
            if user_id not in users:
                users[user_id] = fetched
                self._index_user(fetched, self._handles, self._names)
            return users[user_id]

    def user_label(self, user_id: str) -> tuple[str, str]:
        user = self.user(user_id) if user_id else None
        if not user:
            return user_id, user_id
        return user.get("name", user_id), user.get("real_name") or user.get("profile", {}).get("real_name", "")

    def resolve_user(self, raw: str) -> str:
        """Accept U…/W… IDs, <@U…>, @handle, display name or real name; return the user ID."""
        raw = raw.strip()
        if re.fullmatch(r"<@[UW][A-Z0-9]+>", raw):
            return raw[2:-1]
        if re.fullmatch(r"[UW][A-Z0-9]{6,}", raw):
            return raw
        self._load_users()
        key = raw.removeprefix("@").lower()
        with self._lock:
            handle = self._handles.get(key)
            matches = set(self._names.get(key, set()))
        if handle:
            return handle
        if len(matches) > 1:
            raise SlackError(f"user {raw!r} is ambiguous ({', '.join(sorted(matches))}); pass a user ID or handle")
        if not matches:
            raise SlackError(f"user {raw!r} not found")
        return next(iter(matches))

    def channels(self, refresh: bool = False) -> dict[str, dict]:
        if self._channels is None or refresh:
            found = self.paginate(
                "conversations.list", "channels", types="public_channel,private_channel", exclude_archived=True, limit=999
            )
            self._channels = {c["id"]: c for c in found}
        return self._channels

    def channel_id_by_name(self, name: str) -> str:
        name = name.removeprefix("#")
        for refresh in (False, True):
            for channel in self.channels(refresh=refresh).values():
                if channel.get("name") == name:
                    return channel["id"]
        raise SlackError(f"channel #{name} not found")

    def ims(self, refresh: bool = False) -> dict[str, str]:
        """Map of user ID to an existing DM channel ID."""
        if self._ims is None or refresh:
            found = self.paginate("conversations.list", "channels", types="im", limit=999)
            self._ims = {c["user"]: c["id"] for c in found if c.get("user")}
        return self._ims

    def resolve_channel(self, raw: str, open_dm: bool = False) -> str:
        """Accept a channel ID, #name, or @user; return the conversation ID. Only open_dm may create a DM."""
        raw = raw.strip()
        if raw.startswith("#"):
            return self.channel_id_by_name(raw)
        if raw.startswith("@"):
            user_id = self.resolve_user(raw)
            dm = self.ims().get(user_id) or self.ims(refresh=True).get(user_id)
            if dm:
                return dm
            if not open_dm:
                raise SlackError(f"no DM with {raw} exists yet")
            return self.call("conversations.open", users=user_id)["channel"]["id"]
        return raw

    def readable(self, text: str) -> str:
        """Replace <@U…> and <#C…|name> mentions with @handle and #name."""
        text = re.sub(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", lambda m: "@" + self.user_label(m.group(1))[0], text)
        return re.sub(r"<#([CG][A-Z0-9]+)\|([^>]*)>", lambda m: "#" + (m.group(2) or m.group(1)), text)


def blocks_text(blocks: list[dict]) -> str:
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") in ("text", "mrkdwn", "plain_text") and isinstance(node.get("text"), str):
                parts.append(node["text"])
            elif node.get("type") == "link":
                parts.append(node.get("text") or node.get("url", ""))
            for value in node.values():
                if isinstance(value, (list, dict)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(blocks)
    return "".join(parts)


def iso(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), UTC).isoformat().replace("+00:00", "Z")


def to_csv(rows: list[dict], columns: list[str]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def local_midnight(day: date) -> float:
    """Epoch seconds of local midnight on day, using the system zone's DST rules for that date."""
    return time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))


def history_window(limit: str, today: date | None = None, now: float | None = None) -> tuple[int | None, str | None, str | None]:
    """Parse '50' (message count) or '1d'/'2w'/'3m' (calendar window from local midnight) into limit/oldest/latest."""
    limit = limit.strip()
    if not limit:
        return None, None, None
    if limit.isdigit():
        return int(limit), None, None
    match = re.fullmatch(r"([1-9][0-9]*)([dwm])", limit)
    if not match:
        raise SlackError(f"invalid limit {limit!r}: use a message count like '50' or a window like '1d', '2w', '3m'")
    n, unit = int(match.group(1)), match.group(2)
    today = today or date.today()
    if unit == "d":
        first = today - timedelta(days=n - 1)
    elif unit == "w":
        first = today - timedelta(days=n * 7 - 1)
    else:
        month = today.month - n
        year = today.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        first = date(year, month, min(today.day, calendar.monthrange(year, month)[1]))
    return 100, f"{int(local_midnight(first))}.000000", f"{int(now if now is not None else time.time())}.000000"


MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}
MONTHS.update({name[:3]: num for name, num in list(MONTHS.items())})


def search_date(raw: str, today: datetime | None = None) -> str:
    """Normalize a search date: YYYY-MM-DD, today/yesterday/tomorrow, 'N days ago', 'Month YYYY', or a bare month."""
    value = raw.strip().lower()
    today = today or datetime.now()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        datetime.strptime(value, "%Y-%m-%d")
        return value
    relative = {"today": 0, "yesterday": -1, "tomorrow": 1}
    if value in relative:
        return (today + timedelta(days=relative[value])).strftime("%Y-%m-%d")
    if match := re.fullmatch(r"(\d+) days? ago", value):
        return (today - timedelta(days=int(match.group(1)))).strftime("%Y-%m-%d")
    if value in MONTHS:
        return value
    if (match := re.fullmatch(r"([a-z]+) (\d{4})", value)) and match.group(1) in MONTHS:
        return f"{match.group(2)}-{MONTHS[match.group(1)]:02d}-01"
    raise SlackError(f"unable to parse date {raw!r}")


def month_bounds(raw: str) -> tuple[str, str] | None:
    """'July 2026' -> ('2026-06-30', '2026-08-01'): exclusive after/before bounds covering the month."""
    match = re.fullmatch(r"([a-z]+) (\d{4})", raw.strip().lower())
    if not match or match.group(1) not in MONTHS:
        return None
    year, month = int(match.group(2)), MONTHS[match.group(1)]
    first = date(year, month, 1)
    after = first - timedelta(days=1)
    before = first + timedelta(days=calendar.monthrange(year, month)[1])
    return after.isoformat(), before.isoformat()


def encode_cursor(state: dict) -> str:
    return base64.b64encode(json.dumps(state, separators=(",", ":")).encode()).decode()


def decode_cursor(cursor: str) -> dict:
    try:
        state = json.loads(base64.b64decode(cursor, validate=True))
        if not isinstance(state, dict):
            raise ValueError
        return state
    except ValueError as error:
        raise SlackError(f"invalid cursor {cursor!r}") from error
