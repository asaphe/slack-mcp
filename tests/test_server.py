import json
import time
from datetime import date, datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import anyio
import httpx
import pytest

from slack_mcp import server
from slack_mcp.slack import (
    Slack,
    SlackError,
    decode_cursor,
    encode_cursor,
    history_window,
    local_midnight,
    month_bounds,
    search_date,
)

TOKEN = "xoxp-test-token-value"
USERS = [
    {"id": "U01ALICE", "name": "alice", "real_name": "Alice Doe", "profile": {"display_name": "ali", "email": "a@x.io"}},
    {"id": "U02BOB", "name": "bob", "real_name": "Bob Roe", "profile": {"display_name": "", "email": "b@x.io"}},
]
CHANNELS = [{"id": "C01GEN", "name": "general", "num_members": 5}, {"id": "C02OPS", "name": "ops", "num_members": 9}]


class FakeSlack:
    """Records every Web API call and answers from a per-method table."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = {
            "users.list": {"members": USERS},
            "conversations.list": {"channels": CHANNELS},
            "auth.test": {"user_id": "U01ALICE"},
            **(responses or {}),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content)
        else:
            body = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((method, body))
        answer = self.responses.get(method, {})
        if callable(answer):
            return answer(body)
        return httpx.Response(200, json={"ok": True, **answer})

    def last(self, method):
        return [body for name, body in self.calls if name == method][-1]


@pytest.fixture
def fake(monkeypatch):
    f = FakeSlack()
    monkeypatch.setattr(server, "_slack", Slack(TOKEN, transport=httpx.MockTransport(f), sleep=lambda _: None))
    return f


def test_markdown_message_uses_native_markdown_and_disables_unfurl(fake):
    fake.responses["chat.postMessage"] = {"channel": "C01GEN", "ts": "1.2"}
    out = server.conversations_add_message("#general", text="**hi**\n- a")
    body = fake.last("chat.postMessage")
    assert body == {"channel": "C01GEN", "unfurl_links": "false", "unfurl_media": "false", "markdown_text": "**hi**\n- a"}
    assert "text" not in body and "blocks" not in body
    assert out == "Posted to C01GEN (ts=1.2)"


def test_plain_text_message_turns_off_mrkdwn(fake):
    fake.responses["chat.postMessage"] = {"channel": "C01GEN", "ts": "1.2"}
    server.conversations_add_message("C01GEN", text="*not bold*", content_type="text/plain", thread_ts="1.1")
    assert fake.last("chat.postMessage")["mrkdwn"] == "false"
    assert fake.last("chat.postMessage")["thread_ts"] == "1.1"


def test_blocks_string_is_sent_as_json_array(fake):
    fake.responses["chat.postMessage"] = {"channel": "C01GEN", "ts": "1.2"}
    server.conversations_add_message("C01GEN", text="fallback", blocks='[{"type":"divider"}]')
    assert fake.last("chat.postMessage")["blocks"] == [{"type": "divider"}]


def test_message_validation():
    with pytest.raises(SlackError, match="either text or blocks"):
        server.conversations_add_message("C01GEN")
    with pytest.raises(SlackError, match="thread_ts"):
        server.conversations_add_message("C01GEN", text="x", thread_ts="123")


def test_at_user_reuses_existing_dm(fake):
    fake.responses["conversations.list"] = lambda body: httpx.Response(
        200, json={"ok": True, "channels": [{"id": "D9BOB", "user": "U02BOB"}] if body.get("types") == "im" else CHANNELS}
    )
    assert server.slack().resolve_channel("@bob") == "D9BOB"
    assert not [c for c in fake.calls if c[0] == "conversations.open"]


def test_at_user_opens_dm_when_none_exists(fake):
    fake.responses["conversations.list"] = lambda body: httpx.Response(200, json={"ok": True, "channels": []})
    fake.responses["conversations.open"] = {"channel": {"id": "D7NEW"}}
    assert server.slack().resolve_channel("@Bob Roe", open_dm=True) == "D7NEW"


def test_unknown_channel_refreshes_once_then_fails(fake):
    with pytest.raises(SlackError, match="#nope not found"):
        server.slack().resolve_channel("#nope")
    assert [c[0] for c in fake.calls].count("conversations.list") == 2


def test_history_resolves_names_and_sets_cursor(fake):
    fake.responses["conversations.history"] = {
        "messages": [
            {
                "ts": "1700000000.000100",
                "user": "U01ALICE",
                "text": "ping <@U02BOB> in <#C02OPS|ops>",
                "reactions": [{"name": "eyes", "count": 2}],
            },
            {"ts": "1700000000.000050", "user": "U02BOB", "subtype": "channel_join", "text": "joined"},
        ],
        "has_more": True,
        "response_metadata": {"next_cursor": "abc"},
    }
    out = server.conversations_history("#general", limit="20")
    assert fake.last("conversations.history") == {"channel": "C01GEN", "limit": "20"}
    lines = out.strip().split("\n")
    assert lines[0].startswith("MsgID,UserID,UserName,RealName,Channel")
    assert len(lines) == 2
    assert (
        "ping @bob in #ops" in lines[1]
        and "eyes:2" in lines[1]
        and decode_cursor(lines[1].rsplit(",", 1)[1]) == {"c": "abc", "n": 20, "o": None, "l": None}
    )


def test_history_rejects_limit_with_cursor(fake):
    with pytest.raises(SlackError, match="limit must be empty"):
        server.conversations_history("C01GEN", limit="1d", cursor="abc")
    server.conversations_history("C01GEN", limit="", cursor=encode_cursor({"c": "abc", "n": 20}))
    assert fake.last("conversations.history") == {"channel": "C01GEN", "limit": "20", "cursor": "abc"}


def test_search_builds_query_and_pages(fake):
    fake.responses["search.messages"] = {
        "messages": {
            "matches": [
                {
                    "ts": "1700000000.000100",
                    "user": "U02BOB",
                    "text": "deploy done",
                    "permalink": "https://x.slack.com/archives/C02OPS/p1?thread_ts=1699999999.000001",
                    "channel": {"id": "C02OPS", "name": "ops"},
                }
            ],
            "paging": {"page": 1, "pages": 3},
        }
    }
    out = server.conversations_search_messages(
        search_query="deploy",
        filter_in_channel="#ops",
        filter_users_from="@bob",
        filter_date_on="2026-10-01",
        filter_threads_only=True,
        limit=500,
    )
    body = fake.last("search.messages")
    assert body["query"] == "deploy is:thread in:#ops from:<@U02BOB> on:2026-10-01"
    assert body["count"] == "100"
    assert "C02OPS (#ops)" in out and "1699999999.000001" in out
    assert decode_cursor(out.strip().split("\n")[1].rsplit(",", 1)[1]) == {
        "q": "deploy is:thread in:#ops from:<@U02BOB> on:2026-10-01",
        "p": 2,
        "n": 100,
    }


def test_search_date_conflicts(fake):
    with pytest.raises(SlackError, match="filter_date_on"):
        server.conversations_search_messages(search_query="x", filter_date_on="today", filter_date_after="today")


def test_search_by_message_url_fetches_that_message(fake):
    fake.responses["conversations.history"] = {"messages": [{"ts": "1700000000.000100", "user": "U01ALICE", "text": "hello"}]}
    out = server.conversations_search_messages(search_query="https://x.slack.com/archives/C01GEN/p1700000000000100")
    assert fake.last("conversations.history")["latest"] == "1700000000.000100"
    assert "hello" in out
    assert not [c for c in fake.calls if c[0] == "search.messages"]


def test_users_search_matches_fields_and_dm(fake):
    fake.responses["conversations.list"] = lambda body: httpx.Response(
        200, json={"ok": True, "channels": [{"id": "D9BOB", "user": "U02BOB"}] if body.get("types") == "im" else CHANNELS}
    )
    out = server.users_search("b@x.io")
    assert "U02BOB" in out and "D9BOB" in out and "U01ALICE" not in out
    assert server.users_search("zzz") == "No users found matching the query."


def test_channels_list_filters_sorts_and_pages(fake):
    out = server.channels_list("public_channel", sort="popularity", limit=1)
    lines = out.strip().split("\n")
    assert lines[1].startswith("C02OPS,#ops")
    assert decode_cursor(lines[1].rsplit(",", 1)[1]) == {"i": 1, "n": 1}
    out = server.channels_list("public_channel", sort="popularity", limit=1, cursor=encode_cursor({"i": 1, "n": 1}))
    assert out.strip().split("\n")[1] == "C01GEN,#general,,,5,"
    assert "general" not in server.channels_list("public_channel", query="OP")


def test_usergroups_me_lists_only_my_groups(fake):
    fake.responses["usergroups.list"] = {
        "usergroups": [
            {"id": "S1", "name": "ops", "handle": "ops", "users": ["U01ALICE"]},
            {"id": "S2", "name": "eng", "handle": "eng", "users": ["U02BOB"]},
        ]
    }
    out = server.usergroups_me()
    assert "S1" in out and "S2" not in out
    assert not [c for c in fake.calls if c[0].startswith("usergroups.users")]


def test_reaction_strips_colons(fake):
    server.reactions_add("C01GEN", "1.2", ":eyes:")
    assert fake.last("reactions.add") == {"channel": "C01GEN", "timestamp": "1.2", "name": "eyes"}


def test_structured_args_go_as_json(fake):
    server.canvas_edit("F1", "replace", markdown_content="# hi")
    assert fake.last("canvases.edit")["changes"] == [
        {"operation": "replace", "document_content": {"type": "markdown", "markdown": "# hi"}}
    ]


def test_api_error_names_missing_scope_without_leaking_token(fake):
    fake.responses["reactions.add"] = lambda body: httpx.Response(
        200, json={"ok": False, "error": "missing_scope", "needed": "reactions:write"}
    )
    with pytest.raises(SlackError) as error:
        server.reactions_add("C01GEN", "1.2", "eyes")
    assert "reactions:write" in str(error.value) and TOKEN not in str(error.value)


def test_rate_limit_is_retried(fake):
    state = {"n": 0}

    def flaky(body):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    fake.responses["reactions.add"] = flaky
    server.reactions_add("C01GEN", "1.2", "eyes")
    assert state["n"] == 2


def test_token_only_sent_to_slack_file_host(fake):
    s = server.slack()
    for url in ("https://evil.example/files/x", "http://files.slack.com/x", "https://files.slack.com.evil.io/x"):
        with pytest.raises(SlackError, match="refusing"):
            s.download(url)
    assert fake.calls == []
    fake.responses["x"] = lambda body: httpx.Response(200, text="<p>canvas</p>")
    assert s.download("https://files.slack.com/files-pri/T1-F1/x") == "<p>canvas</p>"


def test_history_window(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    today = date(2026, 10, 15)
    assert history_window("50") == (50, None, None)
    assert history_window("1d", today, now=0) == (100, f"{int(local_midnight(today))}.000000", "0.000000")
    assert float(history_window("1w", today)[1]) == local_midnight(date(2026, 10, 9))
    assert float(history_window("10m", today)[1]) == local_midnight(date(2025, 12, 15))
    assert float(history_window("1m", date(2026, 10, 31))[1]) == local_midnight(date(2026, 9, 30))
    assert float(history_window("1m", date(2028, 3, 31))[1]) == local_midnight(date(2028, 2, 29))
    with pytest.raises(SlackError):
        history_window("0d")


def test_history_window_midnight_survives_dst(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Jerusalem")
    time.tzset()
    try:
        oldest = float(history_window("2w", date(2026, 11, 1))[1])
        local = datetime.fromtimestamp(oldest, ZoneInfo("Asia/Jerusalem"))
        assert (local.date(), local.hour) == (date(2026, 10, 19), 0)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_search_date():
    today = datetime(2026, 10, 15).astimezone()
    assert search_date("2026-01-31") == "2026-01-31"
    assert search_date("Yesterday", today) == "2026-10-14"
    assert search_date("3 days ago", today) == "2026-10-12"
    assert search_date("July 2026") == "2026-07-01"
    assert search_date("july") == "july"
    for bad in ("2026-02-30", "soon"):
        with pytest.raises((SlackError, ValueError)):
            search_date(bad)


def test_main_refuses_without_token(monkeypatch):
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="SLACK_USER_TOKEN"):
        server.main()


def test_handle_beats_another_users_display_name(fake):
    fake.responses["users.list"] = {
        "members": [
            {"id": "U01ALICE", "name": "alice", "real_name": "Alice", "profile": {"display_name": "bob"}},
            {"id": "U02BOB", "name": "bob", "real_name": "Bob", "profile": {"display_name": ""}},
        ]
    }
    assert server.slack().resolve_user("@bob") == "U02BOB"


def test_ambiguous_display_name_is_refused(fake):
    fake.responses["users.list"] = {
        "members": [
            {"id": "U01A", "name": "a1", "real_name": "Sam Lee", "profile": {}},
            {"id": "U02B", "name": "b2", "real_name": "Sam Lee", "profile": {}},
        ]
    }
    with pytest.raises(SlackError, match="ambiguous"):
        server.slack().resolve_user("Sam Lee")


def test_read_tools_never_create_a_dm(fake):
    fake.responses["conversations.list"] = lambda body: httpx.Response(200, json={"ok": True, "channels": []})
    with pytest.raises(SlackError, match="no DM"):
        server.conversations_history("@bob")
    assert not [c for c in fake.calls if c[0] == "conversations.open"]


def test_tool_annotations():
    tools = {t.name: t.annotations for t in anyio.run(server.mcp.list_tools)}
    assert tools["reactions_remove"].destructive_hint is True
    assert tools["usergroups_me"].read_only_hint is True
    for name in ("conversations_history", "conversations_search_messages", "users_search", "canvas_read"):
        assert tools[name].read_only_hint is True, name


def test_history_cursor_keeps_window_and_page_size(fake):
    fake.responses["conversations.history"] = {
        "messages": [{"ts": "1700000000.000100", "user": "U01ALICE", "text": "hi"}],
        "has_more": True,
        "response_metadata": {"next_cursor": "next"},
    }
    first = server.conversations_history("C01GEN", limit="1w")
    window = fake.last("conversations.history")
    cursor = first.strip().split("\n")[-1].rsplit(",", 1)[1]
    server.conversations_history("C01GEN", limit="", cursor=cursor)
    second = fake.last("conversations.history")
    assert second == {**window, "cursor": "next"}


def test_filtered_page_continues_to_next_page(fake):
    pages = iter(
        [
            {
                "messages": [{"ts": "1.1", "subtype": "channel_join"}],
                "has_more": True,
                "response_metadata": {"next_cursor": "p2"},
            },
            {"messages": [{"ts": "1.0", "user": "U01ALICE", "text": "visible"}], "has_more": False},
        ]
    )
    fake.responses["conversations.history"] = lambda body: httpx.Response(200, json={"ok": True, **next(pages)})
    out = server.conversations_history("C01GEN", limit="10")
    assert "visible" in out
    assert fake.last("conversations.history")["cursor"] == "p2"


def test_search_cursor_keeps_page_size(fake):
    fake.responses["search.messages"] = {
        "messages": {
            "matches": [{"ts": "1700000000.000100", "user": "U02BOB", "text": "x", "channel": {"id": "C1", "name": "a"}}],
            "paging": {"page": 1, "pages": 3},
        }
    }
    out = server.conversations_search_messages(search_query="x", limit=10)
    cursor = out.strip().split("\n")[1].rsplit(",", 1)[1]
    server.conversations_search_messages(search_query="x", cursor=cursor)
    assert (fake.last("search.messages")["count"], fake.last("search.messages")["page"]) == ("10", "2")


def test_during_month_year_covers_whole_month(fake):
    fake.responses["search.messages"] = {"messages": {"matches": [], "paging": {}}}
    server.conversations_search_messages(search_query="x", filter_date_during="July 2026")
    assert fake.last("search.messages")["query"] == "x after:2026-06-30 before:2026-08-01"
    assert month_bounds("Feb 2028") == ("2028-01-31", "2028-03-01")


def test_slow_slack_call_does_not_block_event_loop(monkeypatch):
    def slow(request):
        time.sleep(0.5)
        return httpx.Response(200, json={"ok": True, "user_id": "U01ALICE", "usergroups": []})

    monkeypatch.setattr(server, "_slack", Slack(TOKEN, transport=httpx.MockTransport(slow)))
    ticks = []

    async def heartbeat():
        for _ in range(5):
            ticks.append(time.monotonic())
            await anyio.sleep(0.05)

    async def main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(heartbeat)
            await anyio.sleep(0.01)
            tg.start_soon(server.mcp.call_tool, "usergroups_me", {})

    anyio.run(main)
    assert max(b - a for a, b in zip(ticks, ticks[1:], strict=False)) < 0.3


def test_search_cursor_replays_original_query(fake):
    fake.responses["search.messages"] = {
        "messages": {
            "matches": [{"ts": "1700000000.000100", "user": "U02BOB", "text": "x", "channel": {"id": "C1", "name": "a"}}],
            "paging": {"page": 1, "pages": 2},
        }
    }
    out = server.conversations_search_messages(search_query="x", filter_date_on="2026-10-15")
    cursor = out.strip().split("\n")[1].rsplit(",", 1)[1]
    server.conversations_search_messages(search_query="x", filter_date_on="2026-10-16", cursor=cursor)
    assert fake.last("search.messages")["query"] == "x on:2026-10-15"


def test_concurrent_resolution_never_sees_a_partial_directory(monkeypatch):
    users = [{"id": "U01ALICE", "name": "alice", "real_name": "Alice", "profile": {"display_name": "bob"}}]
    users += [{"id": f"U9{i:05d}", "name": f"user{i}", "profile": {}} for i in range(300)]
    users += [{"id": "U02BOB", "name": "bob", "real_name": "Bob", "profile": {}}]

    def directory(request):
        time.sleep(0.01)
        return httpx.Response(200, json={"ok": True, "members": users})

    original = Slack._index_user

    def slow_index(user, handles, names):
        time.sleep(0.0005)
        original(user, handles, names)

    monkeypatch.setattr(Slack, "_index_user", staticmethod(slow_index))
    s = Slack(TOKEN, transport=httpx.MockTransport(directory))
    results = []

    async def main():
        async with anyio.create_task_group() as tg:
            for _ in range(8):
                tg.start_soon(lambda: anyio.to_thread.run_sync(lambda: results.append(s.resolve_user("@bob"))))

    anyio.run(main)
    assert results == ["U02BOB"] * 8
