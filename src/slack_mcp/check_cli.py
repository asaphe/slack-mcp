"""slack-mcp-check: run the message checks on a text, offline. Needs no token and makes no network call."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .checks import CONTENT_TYPES, STYLE_ENV, STYLE_RULES, check, parse_style, refusal

SCHEMA = 1


def main(argv: list[str] | None = None) -> int:
    """Exit 0 when the text passes, 1 when it has issues, 2 when it could not be read or the arguments are wrong."""
    parser = argparse.ArgumentParser(
        prog="slack-mcp-check",
        description="Check a Slack message text the way the slack-mcp server does before sending it. Offline; no token.",
    )
    parser.add_argument("file", nargs="?", help="UTF-8 text file to check; standard input when omitted")
    parser.add_argument("--content-type", default=CONTENT_TYPES[0], choices=CONTENT_TYPES)
    parser.add_argument("--style", help=f"comma-separated style rules ({', '.join(STYLE_RULES)}); default: ${STYLE_ENV}")
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    args = parser.parse_args(argv)
    try:
        style = parse_style(args.style if args.style is not None else os.environ.get(STYLE_ENV))
        raw = Path(args.file).read_bytes() if args.file else sys.stdin.buffer.read()
        text = raw.decode("utf-8")
    except (ValueError, OSError) as error:
        print(f"slack-mcp-check: {error}", file=sys.stderr)
        return 2
    result = check(text, args.content_type, style)
    if args.json:
        retry = None
        if result.retry_text is not None:
            retry = {"content_type": result.retry_content_type, "text": result.retry_text}
        report = {
            "schema": SCHEMA,
            "ok": not result.issues,
            "content_type": args.content_type,
            "style": sorted(style),
            "issues": [{"kind": issue.kind, "message": issue.message} for issue in result.issues],
            "retry": retry,
        }
        print(json.dumps(report, ensure_ascii=False))
    elif result.issues:
        print(refusal(result))
    return 1 if result.issues else 0


if __name__ == "__main__":
    sys.exit(main())
