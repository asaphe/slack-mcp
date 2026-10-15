"""MCP tools over a Slack user token (SLACK_USER_TOKEN, xoxp-…)."""

from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .slack import (
    Slack,
    SlackError,
    blocks_text,
    decode_cursor,
    encode_cursor,
    history_window,
    iso,
    month_bounds,
    search_date,
    to_csv,
)

READ = ToolAnnotations(read_only_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

MESSAGE_COLUMNS = [
    "MsgID",
    "UserID",
    "UserName",
    "RealName",
    "Channel",
    "ThreadTs",
    "Text",
    "Time",
    "Permalink",
    "Reactions",
    "BotName",
    "FileCount",
    "AttachmentIDs",
    "Cursor",
]
CHANNEL_COLUMNS = ["ID", "Name", "Topic", "Purpose", "MemberCount", "Cursor"]
CHANNEL_ARG = "Channel ID (C…/G…/D…), #channel-name, or @user for a DM."

MAX_EMPTY_PAGES = 10

mcp = MCPServer("slack")
_slack: Slack | None = None


def tool(annotations: ToolAnnotations):
    """Register a sync tool; MCPServer runs sync tools in a worker thread, so Slack calls never block the event loop."""
    return mcp.tool(annotations=annotations)


def slack() -> Slack:
    global _slack
    if _slack is None:
        token = os.environ.get("SLACK_USER_TOKEN", "")
        if not token:
            raise SlackError("SLACK_USER_TOKEN is not set")
        _slack = Slack(token)
    return _slack


def message_row(s: Slack, msg: dict, channel: str) -> dict:
    text = msg.get("text") or blocks_text(msg.get("blocks", []))
    files = msg.get("files", [])
    if not text and files:
        text = ", ".join(f.get("name", f.get("id", "")) for f in files)
    user_name, real_name = s.user_label(msg.get("user", ""))
    if not msg.get("user") and msg.get("username"):
        user_name = real_name = msg["username"]
    return {
        "MsgID": msg.get("ts", ""),
        "UserID": msg.get("user", ""),
        "UserName": user_name,
        "RealName": real_name,
        "Channel": channel,
        "ThreadTs": msg.get("thread_ts", ""),
        "Text": s.readable(text),
        "Time": iso(msg["ts"]) if msg.get("ts") else "",
        "Permalink": msg.get("permalink", ""),
        "Reactions": "|".join(f"{r['name']}:{r['count']}" for r in msg.get("reactions", [])),
        "BotName": msg.get("bot_profile", {}).get("name", ""),
        "FileCount": len(files) or "",
        "AttachmentIDs": ", ".join(f"{f['id']} ({f['name']})" if f.get("name") else f["id"] for f in files),
    }


def message_rows(s: Slack, messages: list[dict], channel: str, include_activity: bool) -> list[dict]:
    keep = {None, "bot_message", "thread_broadcast", "file_share", "me_message"}
    return [message_row(s, m, channel) for m in messages if include_activity or m.get("subtype") in keep]


def conversation_args(channel_id: str, limit: str, cursor: str | None) -> dict[str, Any]:
    """Slack arguments for history/replies; a cursor carries the first call's window and page size."""
    if cursor and limit:
        raise SlackError("limit must be empty when cursor is provided")
    if cursor:
        state = decode_cursor(cursor)
        count, oldest, latest, slack_cursor = state.get("n"), state.get("o"), state.get("l"), state.get("c")
    else:
        (count, oldest, latest), slack_cursor = history_window(limit), None
    return {
        "channel": slack().resolve_channel(channel_id),
        "limit": count or 100,
        "oldest": oldest,
        "latest": latest,
        "cursor": slack_cursor,
    }


def message_page(method: str, args: dict[str, Any], include_activity: bool, **extra: Any) -> str:
    """Fetch pages until one has a visible row or the history ends, then emit rows plus a continuation cursor."""
    s = slack()
    rows: list[dict] = []
    next_cursor = None
    for _ in range(MAX_EMPTY_PAGES):
        data = s.call(method, **args, **extra)
        rows = message_rows(s, data.get("messages", []), args["channel"], include_activity)
        next_cursor = data.get("response_metadata", {}).get("next_cursor") if data.get("has_more") else None
        if rows or not next_cursor:
            break
        args = {**args, "cursor": next_cursor}
    if next_cursor:
        state = {"c": next_cursor, "n": args["limit"], "o": args["oldest"], "l": args["latest"]}
        if not rows:
            rows = [{"Text": f"(no visible messages in {MAX_EMPTY_PAGES} pages; continue with the cursor)"}]
        rows[-1]["Cursor"] = encode_cursor(state)
    return to_csv(rows, MESSAGE_COLUMNS)


@tool(READ)
def conversations_history(
    channel_id: str, limit: str = "1d", cursor: str | None = None, include_activity_messages: bool = False
) -> str:
    """Messages in a channel or DM as CSV, newest first. limit: a count ('50') or a window ('1d', '2w', '3m',
    counted from local midnight); leave it empty when passing cursor. A non-empty Cursor column on the last row
    means more results: pass it back as cursor, which keeps the original window and page size."""
    return message_page("conversations.history", conversation_args(channel_id, limit, cursor), include_activity_messages)


@tool(READ)
def conversations_replies(
    channel_id: str,
    thread_ts: str,
    limit: str = "1d",
    cursor: str | None = None,
    include_activity_messages: bool = False,
) -> str:
    """A thread (parent plus replies) as CSV. thread_ts: the parent's ts, e.g. 1234567890.123456.
    limit and cursor work as in conversations_history."""
    args = conversation_args(channel_id, limit, cursor)
    return message_page("conversations.replies", args, include_activity_messages, ts=thread_ts)


@tool(DESTRUCTIVE)
def conversations_add_message(
    channel_id: str,
    text: str | None = None,
    thread_ts: str | None = None,
    content_type: str = "text/markdown",
    blocks: list[dict] | str | None = None,
) -> str:
    """Post a message as you. content_type: 'text/markdown' (standard markdown, rendered by Slack) or 'text/plain'.
    blocks: a Block Kit array, which takes precedence; text then becomes the notification fallback.
    Link and media unfurling is off."""
    if thread_ts and "." not in thread_ts:
        raise SlackError("thread_ts must look like 1234567890.123456")
    if not text and not blocks:
        raise SlackError("either text or blocks must be provided")
    s = slack()
    args: dict[str, Any] = {
        "channel": s.resolve_channel(channel_id, open_dm=True),
        "thread_ts": thread_ts,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if blocks:
        args["blocks"] = json.loads(blocks) if isinstance(blocks, str) else blocks
        args["text"] = text
    elif content_type == "text/markdown":
        if len(text or "") > 12000:
            raise SlackError("markdown messages are limited to 12,000 characters")
        args["markdown_text"] = text
    elif content_type == "text/plain":
        args["text"] = text
        args["mrkdwn"] = False
    else:
        raise SlackError("content_type must be 'text/markdown' or 'text/plain'")
    data = s.call("chat.postMessage", **args)
    where = f" in thread {thread_ts}" if thread_ts else ""
    return f"Posted to {data['channel']}{where} (ts={data['ts']})"


def search_terms(
    s: Slack,
    search_query: str | None,
    filter_in_channel: str | None,
    filter_in_im_or_mpim: str | None,
    filter_users_with: str | None,
    filter_users_from: str | None,
    filter_date_before: str | None,
    filter_date_after: str | None,
    filter_date_on: str | None,
    filter_date_during: str | None,
    filter_threads_only: bool,
) -> str:
    terms = (search_query or "").split()
    if filter_threads_only:
        terms.append("is:thread")
    if filter_in_channel:
        raw = filter_in_channel.strip()
        channel_id = s.channel_id_by_name(raw) if raw.startswith("#") else raw
        channel = s.channels().get(channel_id) or s.call("conversations.info", channel=channel_id)["channel"]
        terms.append(f"in:#{channel['name']}")
    elif filter_in_im_or_mpim:
        terms.append(f"in:<@{s.resolve_user(filter_in_im_or_mpim)}>")
    if filter_users_with:
        terms.append(f"with:<@{s.resolve_user(filter_users_with)}>")
    if filter_users_from:
        terms.append(f"from:<@{s.resolve_user(filter_users_from)}>")
    if filter_date_on and (filter_date_during or filter_date_before or filter_date_after):
        raise SlackError("filter_date_on cannot be combined with other date filters")
    if filter_date_during and (filter_date_before or filter_date_after):
        raise SlackError("filter_date_during cannot be combined with before/after")
    if filter_date_during and (bounds := month_bounds(filter_date_during)):
        terms += [f"after:{bounds[0]}", f"before:{bounds[1]}"]
        filter_date_during = None
    for key, value in (
        ("on", filter_date_on),
        ("during", filter_date_during),
        ("before", filter_date_before),
        ("after", filter_date_after),
    ):
        if value:
            terms.append(f"{key}:{search_date(value)}")
    if not terms:
        raise SlackError("provide search_query or at least one filter")
    return " ".join(terms)


@tool(READ)
def conversations_search_messages(
    search_query: str | None = None,
    filter_in_channel: str | None = None,
    filter_in_im_or_mpim: str | None = None,
    filter_users_with: str | None = None,
    filter_users_from: str | None = None,
    filter_date_before: str | None = None,
    filter_date_after: str | None = None,
    filter_date_on: str | None = None,
    filter_date_during: str | None = None,
    filter_threads_only: bool = False,
    cursor: str | None = None,
    limit: int = 20,
) -> str:
    """Search messages as CSV. search_query may carry Slack modifiers (in:, from:, before:, …) or be a message URL,
    which returns that one message. Users: ID, @handle or name. Channels: ID or #name. Dates: YYYY-MM-DD, today,
    yesterday, 'N days ago', 'July 2026' (during: the whole month) or a month name (during only). limit: 1-100;
    a cursor replays the original query and page size; filters passed with it are ignored."""
    s = slack()
    if search_query and (url := re.match(r"https://[^/]+/archives/([A-Z0-9]+)/p(\d{10})(\d{6})", search_query)):
        channel, ts = url.group(1), f"{url.group(2)}.{url.group(3)}"
        data = s.call("conversations.history", channel=channel, latest=ts, oldest=ts, inclusive=True, limit=1)
        if not data.get("messages"):
            data = s.call("conversations.replies", channel=channel, ts=ts, latest=ts, oldest=ts, inclusive=True, limit=1)
        return to_csv(message_rows(s, data.get("messages", [])[:1], channel, True), MESSAGE_COLUMNS)

    if cursor:
        state = decode_cursor(cursor)
        if not isinstance(state.get("q"), str):
            raise SlackError("invalid cursor: missing query")
    else:
        query = search_terms(
            s,
            search_query,
            filter_in_channel,
            filter_in_im_or_mpim,
            filter_users_with,
            filter_users_from,
            filter_date_before,
            filter_date_after,
            filter_date_on,
            filter_date_during,
            filter_threads_only,
        )
        state = {"q": query, "p": 1, "n": max(1, min(limit, 100))}
    data = s.call("search.messages", query=state["q"], count=state["n"], page=state["p"], highlight=False)["messages"]
    rows = []
    for match in data.get("matches", []):
        thread_ts = parse_qs(urlparse(match.get("permalink", "")).query).get("thread_ts", [""])[0]
        channel = match.get("channel", {})
        row = message_row(s, {**match, "thread_ts": thread_ts}, f"{channel.get('id')} (#{channel.get('name')})")
        rows.append(row)
    paging = data.get("paging", {})
    if rows and paging.get("page", 1) < paging.get("pages", 1):
        rows[-1]["Cursor"] = encode_cursor({**state, "p": paging["page"] + 1})
    return to_csv(rows, MESSAGE_COLUMNS)


@tool(READ)
def users_search(query: str, limit: int = 10) -> str:
    """Find users by ID, handle, display name, real name or email (case-insensitive substring) as CSV,
    with the DM channel ID when one exists."""
    s = slack()
    needle = query.strip().lower()
    if not needle:
        raise SlackError("query is required")
    if re.fullmatch(r"[uw][a-z0-9]{6,}", needle):
        found = [u for u in [s.user(query.strip().upper())] if u]
    else:

        def haystack(u: dict) -> str:
            p = u.get("profile", {})
            return " ".join(filter(None, (u.get("name"), u.get("real_name"), p.get("display_name"), p.get("email"))))

        found = [u for u in s.users().values() if not u.get("deleted") and needle in haystack(u).lower()]
    ims = s.ims()
    rows = [
        {
            "UserID": u["id"],
            "UserName": u.get("name", ""),
            "RealName": u.get("real_name", ""),
            "DisplayName": u.get("profile", {}).get("display_name", ""),
            "Email": u.get("profile", {}).get("email", ""),
            "Title": u.get("profile", {}).get("title", ""),
            "DMChannelID": ims.get(u["id"], ""),
        }
        for u in found[: max(1, min(limit, 100))]
    ]
    if not rows:
        return "No users found matching the query."
    return to_csv(rows, ["UserID", "UserName", "RealName", "DisplayName", "Email", "Title", "DMChannelID"])


def channel_row(s: Slack, c: dict) -> dict:
    name = f"@{s.user_label(c['user'])[0]}" if c.get("is_im") else f"#{c.get('name', '')}"
    return {
        "ID": c["id"],
        "Name": name,
        "Topic": c.get("topic", {}).get("value", ""),
        "Purpose": c.get("purpose", {}).get("value", ""),
        "MemberCount": c.get("num_members", ""),
    }


@tool(READ)
def channels_list(
    channel_types: str,
    query: str | None = None,
    query_targets: str = "name",
    sort: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> str:
    """Workspace channels as CSV. channel_types: comma-separated public_channel, private_channel, im, mpim.
    query: case-insensitive substring over query_targets (name, topic, purpose). sort: 'popularity' (member count)."""
    s = slack()
    found = s.paginate("conversations.list", "channels", types=channel_types, exclude_archived=True, limit=999)
    if query:
        targets = {t.strip() for t in query_targets.split(",")}
        fields = {
            "name": lambda c: c.get("name", ""),
            "topic": lambda c: c.get("topic", {}).get("value", ""),
            "purpose": lambda c: c.get("purpose", {}).get("value", ""),
        }
        found = [c for c in found if any(query.lower() in fields[t](c).lower() for t in targets if t in fields)]
    if sort == "popularity":
        found.sort(key=lambda c: c.get("num_members", 0), reverse=True)
    state = decode_cursor(cursor) if cursor else {"i": 0, "n": max(1, min(limit, 999))}
    start, size = state["i"], state["n"]
    rows = [channel_row(s, c) for c in found[start : start + size]]
    if rows and start + size < len(found):
        rows[-1]["Cursor"] = encode_cursor({"i": start + size, "n": size})
    return to_csv(rows, CHANNEL_COLUMNS)


@tool(READ)
def channels_me(channel_types: str = "public_channel,private_channel", limit: int = 100, cursor: str | None = None) -> str:
    """Conversations you are a member of, as CSV. channel_types as in channels_list."""
    s = slack()
    rows: list[dict] = []
    for _ in range(MAX_EMPTY_PAGES):
        data = s.call(
            "users.conversations", types=channel_types, exclude_archived=True, limit=max(1, min(limit, 999)), cursor=cursor
        )
        rows = [channel_row(s, c) for c in data.get("channels", [])]
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if rows or not cursor:
            break
    if cursor:
        if not rows:
            rows = [{"Name": f"(no conversations in {MAX_EMPTY_PAGES} pages; continue with the cursor)"}]
        rows[-1]["Cursor"] = cursor
    return to_csv(rows, CHANNEL_COLUMNS)


@tool(READ)
def usergroups_list(include_users: bool = False, include_disabled: bool = False) -> str:
    """User groups (@mention groups) as CSV: id, name, handle, description, user_count, users (if include_users)."""
    groups = slack().call("usergroups.list", include_users=include_users, include_count=True, include_disabled=include_disabled)[
        "usergroups"
    ]
    rows = [{**g, "users": ",".join(g.get("users", []))} for g in groups]
    columns = ["id", "name", "handle", "description", "user_count"] + (["users"] if include_users else [])
    return to_csv(rows, columns)


@tool(READ)
def usergroups_me() -> str:
    """User groups you belong to, as CSV."""
    s = slack()
    me = s.me()
    groups = s.call("usergroups.list", include_users=True, include_count=True)["usergroups"]
    return to_csv([g for g in groups if me in g.get("users", [])], ["id", "name", "handle", "description", "user_count"])


@tool(WRITE)
def reactions_add(channel_id: str, timestamp: str, emoji: str) -> str:
    """Add an emoji reaction (name without colons) to a message."""
    channel = slack().resolve_channel(channel_id)
    slack().call("reactions.add", channel=channel, timestamp=timestamp, name=emoji.strip(":"))
    return f"Added :{emoji.strip(':')}: to {timestamp} in {channel}"


@tool(DESTRUCTIVE)
def reactions_remove(channel_id: str, timestamp: str, emoji: str) -> str:
    """Remove your emoji reaction from a message."""
    channel = slack().resolve_channel(channel_id)
    slack().call("reactions.remove", channel=channel, timestamp=timestamp, name=emoji.strip(":"))
    return f"Removed :{emoji.strip(':')}: from {timestamp} in {channel}"


def to_epoch(post_at: str) -> int:
    if post_at.isdigit():
        return int(post_at)
    dt = datetime.fromisoformat(post_at)
    if dt.tzinfo is None:
        raise SlackError("post_at needs a UTC offset, e.g. 2026-10-14T09:00:00+03:00")
    return int(dt.timestamp())


def markdown(md: str | None) -> dict | None:
    return {"type": "markdown", "markdown": md} if md is not None else None


@tool(DESTRUCTIVE)
def schedule_message(channel: str, post_at: str, text: str, thread_ts: str | None = None) -> dict:
    """Schedule a message up to 120 days ahead. post_at: Unix seconds or ISO-8601 with offset."""
    s = slack()
    return s.call(
        "chat.scheduleMessage",
        channel=s.resolve_channel(channel, open_dm=True),
        post_at=to_epoch(post_at),
        text=text,
        thread_ts=thread_ts,
    )


@tool(READ)
def list_scheduled_messages(channel: str | None = None, cursor: str | None = None) -> dict:
    """Your pending scheduled messages, optionally for one channel."""
    s = slack()
    return s.call(
        "chat.scheduledMessages.list", channel=s.resolve_channel(channel) if channel else None, cursor=cursor, limit=100
    )


@tool(DESTRUCTIVE)
def delete_scheduled_message(channel: str, scheduled_message_id: str) -> dict:
    """Cancel a scheduled message before it posts."""
    s = slack()
    return s.call("chat.deleteScheduledMessage", channel=s.resolve_channel(channel), scheduled_message_id=scheduled_message_id)


@tool(DESTRUCTIVE)
def canvas_create(title: str | None = None, markdown_content: str | None = None, channel_id: str | None = None) -> dict:
    """Create a canvas from markdown. channel_id tabs it in that channel; omit for a standalone canvas."""
    s = slack()
    return s.call(
        "canvases.create",
        title=title,
        document_content=markdown(markdown_content),
        channel_id=s.resolve_channel(channel_id) if channel_id else None,
    )


@tool(DESTRUCTIVE)
def canvas_edit(canvas_id: str, operation: str, markdown_content: str | None = None, section_id: str | None = None) -> dict:
    """Apply one edit. operation: insert_at_start|insert_at_end|insert_before|insert_after|replace|delete|rename.
    insert_before/insert_after/delete need section_id (canvas_find_sections); replace without section_id replaces
    the whole canvas; rename uses markdown_content as the new title."""
    change: dict[str, Any] = {"operation": operation}
    if operation == "rename":
        change["title_content"] = markdown(markdown_content)
    elif markdown_content is not None:
        change["document_content"] = markdown(markdown_content)
    if section_id:
        change["section_id"] = section_id
    return slack().call("canvases.edit", canvas_id=canvas_id, changes=[change])


@tool(READ)
def canvas_find_sections(canvas_id: str, contains_text: str | None = None, section_types: list[str] | None = None) -> dict:
    """Find section IDs in a canvas by text and/or type (h1, h2, h3, any_header, list, table, ...)."""
    criteria = {k: v for k, v in {"contains_text": contains_text, "section_types": section_types}.items() if v}
    return slack().call("canvases.sections.lookup", canvas_id=canvas_id, criteria=criteria)


@tool(READ)
def canvas_read(canvas_id: str) -> dict:
    """A canvas's title and text content."""
    s = slack()
    info = s.call("files.info", file=canvas_id)["file"]
    url = info.get("url_private_download") or info.get("url_private")
    if not url:
        raise SlackError(f"files.info returned no download URL for {canvas_id}")
    text = re.sub(r"<(br|/p|/h[1-6]|/li|/div|/tr)\b[^>]*>", "\n", s.download(url), flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "permalink": info.get("permalink"),
        "content": re.sub(r"\n{3,}", "\n\n", text).strip(),
    }


@tool(DESTRUCTIVE)
def list_create(name: str, schema: list[dict] | None = None, todo_mode: bool | None = None) -> dict:
    """Create a Slack List. schema: [{"key","name","type":"text|date|select|user|...","is_primary_column"?,"options"?}]."""
    return slack().call("slackLists.create", name=name, schema=schema, todo_mode=todo_mode)


@tool(READ)
def list_items(list_id: str, limit: int = 100, cursor: str | None = None, include_list: bool = True) -> dict:
    """A List's items; include_list also returns its columns (column_id values for writes)."""
    return slack().call("slackLists.items.list", list_id=list_id, limit=limit, cursor=cursor, include_list=include_list)


@tool(DESTRUCTIVE)
def list_item_create(list_id: str, initial_fields: list[dict] | None = None, parent_item_id: str | None = None) -> dict:
    """Add an item. initial_fields: [{"column_id", <type>: value}]; text columns take "rich_text" blocks."""
    return slack().call("slackLists.items.create", list_id=list_id, initial_fields=initial_fields, parent_item_id=parent_item_id)


@tool(DESTRUCTIVE)
def list_items_update(list_id: str, cells: list[dict]) -> dict:
    """Update up to 100 cells: [{"row_id","column_id", <type>: value}]."""
    return slack().call("slackLists.items.update", list_id=list_id, cells=cells)


def main() -> None:
    if not os.environ.get("SLACK_USER_TOKEN"):
        raise SystemExit("slack-mcp: SLACK_USER_TOKEN is not set")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
