# slack-mcp

A lean [MCP](https://modelcontextprotocol.io) server that acts in Slack as **you**, through a user OAuth token (`xoxp-…`). One small Python process covers messaging, threads, search, users, channels, user groups and reactions, plus the APIs most Slack MCP servers skip: scheduled messages, canvases and Slack Lists.

## Tools

| Tool | What it does | Slack methods |
|---|---|---|
| `conversations_history` | Channel or DM messages as CSV; `limit` is a count (`50`) or a window (`1d`, `2w`, `3m`) | `conversations.history` |
| `conversations_replies` | A thread as CSV | `conversations.replies` |
| `conversations_add_message` | Post as you; markdown is rendered by Slack (`markdown_text`), unfurling off | `chat.postMessage` |
| `conversations_search_messages` | Search with channel, user, date and thread filters, or fetch one message by URL | `search.messages` |
| `users_search` | Find users by ID, handle, name or email, with their DM channel | `users.list`, `users.info` |
| `channels_list` / `channels_me` | All channels, filtered and sorted / channels you are in | `conversations.list`, `users.conversations` |
| `usergroups_list` / `usergroups_me` | User groups / the groups you belong to (read-only) | `usergroups.list` |
| `reactions_add` / `reactions_remove` | Emoji reactions | `reactions.*` |
| `schedule_message`, `list_scheduled_messages`, `delete_scheduled_message` | Scheduled messages | `chat.scheduleMessage`, `chat.scheduledMessages.list`, `chat.deleteScheduledMessage` |
| `canvas_create`, `canvas_edit`, `canvas_find_sections`, `canvas_read` | Canvases from and to markdown | `canvases.*`, `files.info` |
| `list_create`, `list_items`, `list_item_create`, `list_items_update` | Slack Lists (paid plans) | `slackLists.*` |

Wherever a tool takes a channel, it accepts an ID, `#channel-name`, or `@user` for that user's DM. Only `conversations_add_message` and `schedule_message` open a DM that does not exist yet; read tools fail instead. A user is matched by ID, handle, then display or real name, and an ambiguous name is refused. Paging cursors carry the original window and page size. Tool names and parameters match [korotovsky/slack-mcp-server](https://github.com/korotovsky/slack-mcp-server) for the overlapping tools, so existing prompts and permission rules keep working.

## Setup

1. Create a Slack app at <https://api.slack.com/apps> → **From a manifest**:

   ```json
   {
     "display_information": { "name": "Slack MCP" },
     "oauth_config": {
       "scopes": {
         "user": [
           "channels:history", "channels:read", "groups:history", "groups:read",
           "im:history", "im:read", "im:write", "mpim:history", "mpim:read",
           "users:read", "users:read.email", "chat:write", "search:read",
           "usergroups:read", "reactions:write",
           "canvases:read", "canvases:write", "files:read", "lists:read", "lists:write"
         ]
       }
     },
     "settings": { "org_deploy_enabled": false, "socket_mode_enabled": false, "token_rotation_enabled": false }
   }
   ```

   Drop the scopes for tools you will not use; a call that needs a missing scope fails with `missing_scope (token lacks scope: …)`.

2. **Install to Workspace** and copy the **User OAuth Token**.

3. Register a launcher that reads the token from your secret manager at startup, so the token never lands in a config file or on a command line:

   ```sh
   #!/usr/bin/env bash
   # slack-mcp-start.sh
   SLACK_USER_TOKEN="$(op read 'op://Private/Slack MCP/token')" || exit 1   # or any secret manager
   export SLACK_USER_TOKEN
   exec uvx --from git+https://github.com/asaphe/slack-mcp slack-mcp
   ```

   ```sh
   claude mcp add slack --scope user -- /path/to/slack-mcp-start.sh
   ```

   Avoid `claude mcp add … -e SLACK_USER_TOKEN="$(…)"`: the shell expands the secret before Claude Code runs, and it is stored in plain text in `~/.claude.json`.

## Security

- The token is read from `SLACK_USER_TOKEN` only, sent only in the `Authorization` header to `slack.com/api/` and, for `canvas_read`, to `files.slack.com` over HTTPS (any other file URL is refused and redirects are not followed). It is never logged or included in errors.
- Posting tools act as you. Gate them in your MCP client (for Claude Code: `permissions.ask` on `mcp__slack__conversations_add_message`, `schedule_message`, `canvas_create`, `canvas_edit`, `list_create`, `list_item_create`, `list_items_update`, `delete_scheduled_message`, `reactions_*`).
- No telemetry, no disk cache; user and channel directories live in memory for the process lifetime.
- Tools run in worker threads, so a slow Slack call or a rate-limit wait never blocks the MCP connection.

## Development

```sh
uv sync --all-groups
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

## License

MIT
