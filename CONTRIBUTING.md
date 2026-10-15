# Contributing

## Reporting issues

Open an issue with what you expected, what happened, steps to reproduce, and your environment (OS, Python and uv versions, MCP client). For a security problem, follow [SECURITY.md](SECURITY.md) instead.

## Submitting changes

1. Fork the repository and create a branch: `fix/short-description` or `feat/short-description`.
2. Make the change, with a test for new behavior.
3. Run the checks below; all must pass.
4. Open a pull request. `main` takes signed, squash-merged PRs only.

```bash
uv sync --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

## Guidelines

- Tools live in `src/slack_mcp/server.py`; HTTP, retries and name resolution live in `src/slack_mcp/slack.py`. Call Slack through `Slack.call`, not a new HTTP client.
- A tool's docstring is its MCP description; keep it accurate and short.
- Tests use `httpx.MockTransport` (see `tests/test_server.py`); never a real token or workspace.
- No tokens, workspace IDs, real user or channel names, or personal data anywhere in code, tests or docs.
- Keep tool names and parameters stable; a rename breaks users' permission rules.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
