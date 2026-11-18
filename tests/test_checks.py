import io
import json
import subprocess
import sys

import pytest

from slack_mcp import check_cli
from slack_mcp.checks import Result, check, parse_style, refusal

ALL = frozenset({"paragraphs", "plain-markup"})
WALL = " ".join(["This sentence is filler."] * 7)

MUST_REFUSE = {
    "mrkdwn link": "See <https://github.com/o/r/pull/1|PR 1> for detail.",
    "dot bullet": "**Status**\n• one\n• two",
    "indented dot bullet": "**Status**\n  • nested",
    "single-asterisk title": "*Status update*\n- done",
    "single-asterisk inline": "The deploy is *done* now.",
    "bare user mention": "@jane.doe done, access granted.",
    "bare group mention": "Ping @platform about it.",
    "bare @here": "@here deploy is starting",
    "glob outside backticks": "Remove *_backup_* files.",
}

MUST_PASS = {
    "standard markdown": "**Status**\n- one [PR 1](https://github.com/o/r/pull/1)\n- two _soon_",
    "glob in backticks": "Remove `*_backup_*` and `deploy-*.yaml` files.",
    "glob without spaces": "Matches deploy-*.yaml only.",
    "arithmetic stars": "Cost is 3 * 4 * 5 units.",
    "fenced mrkdwn": "**Example**\n```\n• <https://x.y|z> *b* @someone\n```",
    "user id mention": "<@U012AB3CD> done, access granted.",
    "group id mention": "<!subteam^S012AB3CD> please look.",
    "email address": "Mail ops@example.com about it.",
    "npm scope": "Bump @types/node today.",
    "plain url angle": "Link: <https://example.com/x>",
    "bold double star": "**Done**: the deploy is out.",
}

STRUCTURED = (
    "Hi Sam, a nudge on the widget PR.\n\n**Action for your team:** review and approve [o/r#1](https://github.com/o/r/pull/1)\n\n"
    "**Why now:** the next step is blocked until it merges:\n```\nthis PR  →  schema align  →  provisioning\n```\n"
    "_Until then, a deploy can still ship code onto a schema that is missing tables._\n\n"
    "**State:** rebased on today's `main`, no failing checks."
)
NUMBERED = (
    "Hi Sam, a nudge on the four widget PRs.\n\n**Action for you:** review and approve all four, in this order:\n"
    + "\n".join(f"{i}. [o/r#{i}](https://github.com/o/r/pull/{i}): part {i}" for i in range(1, 5))
    + "\n\n**Why now:** the next step is blocked until these merge:\n```\nthese PRs  →  schema align\n```\n"
    "_Until then, a deploy can still ship code onto a schema that is missing tables._\n\n**State:** all four are rebased."
)
LONG = "**Update**\n" + "\n".join(f"- item {i} " + "abcdefghij " * 12 for i in range(24))


@pytest.mark.parametrize("style", [frozenset(), ALL], ids=["default", "all-style"])
@pytest.mark.parametrize("name", MUST_REFUSE)
def test_wrong_dialect_is_refused_whatever_the_style(name, style):
    result = check(MUST_REFUSE[name], style=style)
    assert result.issues and {i.kind for i in result.issues} <= {"dialect", "mention"}, name


@pytest.mark.parametrize("style", [frozenset(), ALL], ids=["default", "all-style"])
@pytest.mark.parametrize("name", MUST_PASS)
def test_standard_markdown_passes(name, style):
    assert check(MUST_PASS[name], style=style) == Result((), "text/markdown", None), name


def test_rewrite_is_mechanical_and_passes():
    text = "*Status*\n• see <https://x.io/a|the PR>\n• ask @platform\n• keep `*_backup_*`"
    fixed = check(text).retry_text
    assert fixed == "**Status**\n- see [the PR](https://x.io/a)\n- ask `@platform`\n- keep `*_backup_*`"
    assert not check(fixed).issues


def test_star_span_holding_code_span_is_rewritten_whole():
    fixed = check("*acme / `Widget`*\n- a").retry_text
    assert fixed == "**acme / `Widget`**\n- a"
    assert not check(fixed).issues


def test_code_with_markup_is_restored_verbatim():
    text = "*T*\n```\n• <https://x.y|z> *b*\n```\nsee `*.md` and `@x`"
    assert check(text).retry_text == "**T**\n```\n• <https://x.y|z> *b*\n```\nsee `*.md` and `@x`"


def test_nul_bytes_are_dropped_before_checking():
    result = check("*a\x00b*")
    assert result.retry_text == "**ab**"


@pytest.mark.parametrize("text", [STRUCTURED, NUMBERED, LONG], ids=["structured", "numbered", "long"])
def test_message_length_is_never_checked(text):
    assert len(LONG) > 3000
    assert not check(text, style=ALL).issues


def test_paragraph_rule_is_opt_in():
    assert not check(WALL).issues
    issues = check(WALL, style=frozenset({"paragraphs"})).issues
    assert [i.kind for i in issues] == ["paragraph"] and "28 words / 7 sentences (limit 80 / 5)" in issues[0].message
    assert check(WALL, style=frozenset({"paragraphs"})).retry_text is None


def test_paragraph_rule_counts_the_rewritten_text():
    result = check("*Update*\n" + WALL, style=frozenset({"paragraphs"}))
    assert [i.kind for i in result.issues] == ["dialect", "paragraph"]
    assert result.retry_text == "**Update**\n" + WALL


def test_plain_text_with_markup_passes_by_default():
    assert not check("**Done** see [PR](https://x.io)", "text/plain").issues
    assert not check("*literal*", "text/plain").issues


def test_plain_markup_rule_refuses_markup_and_retries_as_markdown():
    result = check("**Done** see [PR](https://x.io)", "text/plain", frozenset({"plain-markup"}))
    assert [i.kind for i in result.issues] == ["plain-markup"]
    assert (result.retry_content_type, result.retry_text) == ("text/markdown", "**Done** see [PR](https://x.io)")


@pytest.mark.parametrize("text", ["Deploy finished, all green.", "Set max_retry_count to 3 in __init__."])
def test_plain_text_without_markup_passes_under_every_rule(text):
    assert not check(text, "text/plain", ALL).issues


def test_plain_bare_mention_is_refused_without_a_backtick_rewrite():
    result = check("Ping @platform about it.", "text/plain")
    assert [i.kind for i in result.issues] == ["mention"] and "or drop the @" in result.issues[0].message
    assert result.retry_text is None


def test_unknown_content_type_is_an_error():
    with pytest.raises(ValueError, match="content_type must be"):
        check("x", "text/html")


def test_parse_style():
    assert parse_style(None) == parse_style("") == parse_style(" , ") == frozenset()
    assert parse_style(" paragraphs, plain-markup ,") == ALL
    with pytest.raises(ValueError, match="unknown style rule\\(s\\) on, walls; known: paragraphs, plain-markup"):
        parse_style("walls,paragraphs,on")


def test_refusal_names_the_check_and_carries_the_retry():
    text = refusal(check("*Hi* <https://x.io|there>"))
    assert text.startswith("slack-mcp message check, a local check and not an error from Slack, refused this message:\n- ")
    assert text.endswith("\n<<<\n**Hi** [there](https://x.io)\n>>>")
    assert "content_type 'text/markdown'" in text


def test_refusal_lists_at_most_ten_issues():
    many = "\n\n".join([WALL] * 12)
    result = check(many, style=frozenset({"paragraphs"}))
    assert len(result.issues) == 12 and refusal(result).count("\n- ") == 10


def run_cli(monkeypatch, capsys, text, *argv, env=None):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(text if isinstance(text, bytes) else text.encode())))
    monkeypatch.delenv("SLACK_MCP_STYLE", raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    code = check_cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_passes_clean_text_silently(monkeypatch, capsys):
    assert run_cli(monkeypatch, capsys, "**Done**: the deploy is out.\n") == (0, "", "")


def test_cli_refuses_with_exit_1_and_the_refusal_text(monkeypatch, capsys):
    code, out, err = run_cli(monkeypatch, capsys, "*Hi*")
    assert code == 1 and err == "" and out.startswith("slack-mcp message check") and "<<<\n**Hi**\n>>>" in out


def test_cli_json_contract(monkeypatch, capsys):
    code, out, _ = run_cli(monkeypatch, capsys, "*Hi*\n" + WALL, "--json", "--style", "paragraphs")
    report = json.loads(out)
    assert code == 1 and list(report) == ["schema", "ok", "content_type", "style", "issues", "retry"]
    assert (report["schema"], report["ok"], report["content_type"], report["style"]) == (
        1,
        False,
        "text/markdown",
        ["paragraphs"],
    )
    assert [i["kind"] for i in report["issues"]] == ["dialect", "paragraph"]
    assert report["retry"] == {"content_type": "text/markdown", "text": "**Hi**\n" + WALL}
    code, out, _ = run_cli(monkeypatch, capsys, "fine", "--json")
    assert code == 0 and json.loads(out) == {
        "schema": 1,
        "ok": True,
        "content_type": "text/markdown",
        "style": [],
        "issues": [],
        "retry": None,
    }


def test_cli_style_comes_from_the_environment_unless_given(monkeypatch, capsys):
    assert run_cli(monkeypatch, capsys, WALL)[0] == 0
    assert run_cli(monkeypatch, capsys, WALL, env={"SLACK_MCP_STYLE": "paragraphs"})[0] == 1
    assert run_cli(monkeypatch, capsys, WALL, "--style", "", env={"SLACK_MCP_STYLE": "paragraphs"})[0] == 0


def test_cli_reads_a_file_and_a_content_type(monkeypatch, capsys, tmp_path):
    path = tmp_path / "message.txt"
    path.write_text("**Done**", encoding="utf-8")
    assert run_cli(monkeypatch, capsys, "", str(path), "--content-type", "text/plain", "--style", "plain-markup")[0] == 1
    assert run_cli(monkeypatch, capsys, "", str(path))[0] == 0


@pytest.mark.parametrize(
    ("text", "argv", "env", "needle"),
    [
        (b"\xff\xfe", [], {}, "codec can't decode"),
        ("x", ["--style", "walls"], {}, "unknown style rule(s) walls"),
        ("x", [], {"SLACK_MCP_STYLE": "on"}, "unknown style rule(s) on"),
        ("x", ["/nonexistent/message.txt"], {}, "No such file"),
    ],
    ids=["not-utf8", "bad-style-flag", "bad-style-env", "missing-file"],
)
def test_cli_exits_2_when_it_cannot_check(monkeypatch, capsys, text, argv, env, needle):
    code, out, err = run_cli(monkeypatch, capsys, text, *argv, env=env)
    assert code == 2 and out == "" and err.startswith("slack-mcp-check: ") and needle in err


def test_cli_rejects_an_unknown_content_type(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run_cli(monkeypatch, capsys, "x", "--content-type", "text/html")
    assert exit_info.value.code == 2


def test_cli_needs_no_token_and_loads_no_network_library():
    probe = (
        "import sys; from slack_mcp import check_cli; sys.argv = ['slack-mcp-check', '--json']; code = check_cli.main(); "
        "print(code, sorted(m for m in ('httpx', 'mcp', 'slack_mcp.server', 'slack_mcp.slack') if m in sys.modules))"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe], input="*Hi*", capture_output=True, text=True, timeout=30, env={}, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1] == "1 []"
