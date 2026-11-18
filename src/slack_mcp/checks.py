"""Checks run on a message text before it is sent: wrong-dialect markup always, style rules on request.

Text sent as Slack's `markdown_text` renders as standard markdown, so Slack mrkdwn (`<url|label>`, `*bold*`, `•` bullets)
shows up broken, and a bare `@name` notifies no one. A refusal carries the rewritten text, so the retry is mechanical.
This module is pure: no token, no network, standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STYLE_ENV = "SLACK_MCP_STYLE"
STYLE_RULES = ("paragraphs", "plain-markup")
CONTENT_TYPES = ("text/markdown", "text/plain")
MAX_WORDS = 80
MAX_SENTENCES = 5
MAX_ISSUES = 10

SENTENCE_END = re.compile(r"[.!?][)\"'`]*(?=\s+\S)")
FENCE = r"(?ms)^ {0,3}(```|~~~).*?^ {0,3}\1[^\n]*$"
CODE = re.compile(FENCE + r"|(`+)[^\n]+?\2")
MRKDWN_LINK = re.compile(r"<((?:https?|mailto):[^\s<>|]+)\|([^<>\n]+)>")
DOT_BULLET = re.compile(r"(?m)^([ \t]*)[•◦▪][ \t]*")
SINGLE_STAR = re.compile(r"(?:^|(?<=[\s(\[\"']))\*(?=[^\s*])([^*\n]*?[^\s*\\])\*(?=$|[\s.,:;!?)\]\"'])", re.MULTILINE)
BARE_MENTION = re.compile(r"(?<![\w<@/.`!^|])@([A-Za-z][\w.\-]*\w)(?![\w/@])")
DOUBLE_STAR = re.compile(r"\*\*[^*\n]+\*\*")
MD_LINK = re.compile(r"\[[^\]\n]+\]\([^)\s]+\)")
HEADER = re.compile(r"(?m)^ {0,3}#{1,6} \S")
UNDERSCORE = re.compile(r"(?<!\w)_[^_\s][^_\n]*?_(?!\w)")
PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n|\n(?= {0,3}(?:[-*+•]|[0-9]{1,9}[.)])[ \t])")


@dataclass(frozen=True)
class Issue:
    kind: str  # dialect | mention | plain-markup | paragraph
    message: str


@dataclass(frozen=True)
class Result:
    issues: tuple[Issue, ...]
    retry_content_type: str
    retry_text: str | None  # the mechanically fixed text, or None when there is nothing to fix or nothing changed


def parse_style(raw: str | None) -> frozenset[str]:
    """Style rules named in a comma-separated list; an unknown name is an error, never silently off."""
    names = frozenset(part.strip() for part in (raw or "").split(",") if part.strip())
    if unknown := sorted(names - set(STYLE_RULES)):
        raise ValueError(f"{STYLE_ENV}: unknown style rule(s) {', '.join(unknown)}; known: {', '.join(STYLE_RULES)}")
    return names


def prose(text: str) -> str:
    """Text outside code, each code stretch replaced by a NUL so no pattern spans it."""
    return CODE.sub("\x00", text)


def to_markdown(text: str) -> str:
    """Fix the prose with code masked, so a *span* holding a `code span` is rewritten whole, then restore the code."""
    code = iter([m.group(0) for m in CODE.finditer(text)])
    s = MRKDWN_LINK.sub(lambda m: f"[{m.group(2)}]({m.group(1)})", prose(text))
    s = DOT_BULLET.sub(lambda m: f"{m.group(1)}- ", s)
    s = SINGLE_STAR.sub(lambda m: f"**{m.group(1)}**", s)
    s = BARE_MENTION.sub(lambda m: f"`{m.group(0)}`", s)
    return re.sub("\x00", lambda _: next(code), s)


def markdown_issues(body: str) -> list[Issue]:
    masked = prose(body)
    found = []
    if links := MRKDWN_LINK.findall(masked):
        found.append(Issue("dialect", f"{len(links)} Slack-mrkdwn link(s) like <url|label>; markdown_text needs [label](url)"))
    if bullets := DOT_BULLET.findall(masked):
        found.append(Issue("dialect", f"{len(bullets)} line(s) start with '•'; use '- ' bullets"))
    if stars := [m.group(0) for m in SINGLE_STAR.finditer(masked)]:
        found.append(
            Issue(
                "dialect",
                f"{len(stars)} single-asterisk span(s) like {stars[0]!r}: italic in markdown, not bold; use **bold**, "
                "or backticks for a glob",
            )
        )
    return found + mention_issues(masked)


def mention_issues(masked: str, backticked: bool = True) -> list[Issue]:
    names = sorted({m.group(0) for m in BARE_MENTION.finditer(masked)})
    if not names:
        return []
    special = [n for n in names if n in ("@here", "@channel", "@everyone")]
    hint = "; to notify the channel write <!here>/<!channel>, only if the user asked" if special else ""
    fallback = "the rewrite backticks them, which keeps today's no-notify effect" if backticked else "or drop the @"
    return [
        Issue(
            "mention",
            f"bare mention(s) {', '.join(names[:6])} notify no one: a user needs <@U…> (users_search), a group "
            f"<!subteam^S…> (usergroups_list); {fallback}{hint}",
        )
    ]


def plain_markup(body: str) -> list[Issue]:
    masked = prose(body)
    kinds = [
        name
        for name, rx in (
            ("**bold**", DOUBLE_STAR),
            ("[label](url)", MD_LINK),
            ("<url|label>", MRKDWN_LINK),
            ("# header", HEADER),
            ("*span*", SINGLE_STAR),
            ("_span_", UNDERSCORE),
        )
        if rx.search(masked)
    ]
    if CODE.search(body):
        kinds.append("`code`")
    if not kinds:
        return []
    message = f"text/plain shows markup literally ({', '.join(kinds)}); resend as content_type 'text/markdown'"
    return [Issue("plain-markup", message)]


def walls(text: str) -> list[Issue]:
    found = []
    blocks = PARAGRAPH_BREAK.split(re.sub(FENCE, "", text))
    for number, block in enumerate((b for b in blocks if b and b.strip()), start=1):
        words = len(block.split())
        sentences = len(SENTENCE_END.findall(block)) + 1
        if words > MAX_WORDS or sentences > MAX_SENTENCES:
            found.append(
                Issue(
                    "paragraph",
                    f"paragraph {number} is {words} words / {sentences} sentences (limit {MAX_WORDS} / {MAX_SENTENCES}): "
                    f"{block.strip()[:60]!r}",
                )
            )
    return found


def check(text: str, content_type: str = "text/markdown", style: frozenset[str] = frozenset()) -> Result:
    """Issues in a text about to be sent as content_type, plus the mechanical rewrite when one exists."""
    if content_type not in CONTENT_TYPES:
        raise ValueError("content_type must be 'text/markdown' or 'text/plain'")
    body = text.replace("\x00", "")
    if content_type == "text/plain":
        issues = plain_markup(body) if "plain-markup" in style else []
        retry_type = "text/markdown" if issues else content_type
        rewritten = to_markdown(body) if issues else body
        issues += mention_issues(prose(body), backticked=bool(issues))
    else:
        issues = markdown_issues(body)
        rewritten = to_markdown(body)
        retry_type = content_type
    if "paragraphs" in style:
        issues += walls(rewritten)
    changed = rewritten != text or retry_type != content_type
    return Result(tuple(issues), retry_type, rewritten if issues and changed else None)


def refusal(result: Result) -> str:
    """The refusal text for a result with issues: what is wrong, then the retry text between markers."""
    tail = ""
    if result.retry_text is not None:
        tail = (
            f"\n\nRetry with content_type '{result.retry_content_type}' and this text, verbatim between the markers "
            f"(markup fixed; an over-long paragraph still needs your split):\n<<<\n{result.retry_text}\n>>>"
        )
    listed = "\n- ".join(issue.message for issue in result.issues[:MAX_ISSUES])
    return f"slack-mcp message check, a local check and not an error from Slack, refused this message:\n- {listed}{tail}"
