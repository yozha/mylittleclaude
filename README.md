# mylittleclaude

A single-operator Telegram bot that drives one or more [Claude Code](https://github.com/anthropics/claude-code) instances across local and remote machines. Each Telegram forum topic is bound to one rolling Claude Code conversation; messages in the topic are sent as prompts and results come back into the topic.

> Public, MIT-licensed, single-tenant, self-hostable. There is no hosted service. Anyone who clones the repo deploys their own bot with their own token.

```
[screenshot placeholder — operator chat in a Telegram supergroup with topics]
```

## Features

- Forum-topic-per-session model; sessions resume across bot restarts.
- One-prompt-at-a-time per topic with a *hold-and-confirm* queue (`Run` / `Cancel` / `Edit`).
- Live working-message with 15-minute heartbeat edits and a `Kill` button (`SIGTERM` then `SIGKILL`).
- Local invocation and SSH workers via `servers.yaml`.
- File uploads land in `<workdir>/_inbox/`. `/get <relative_path>` pulls a file back.
- Costs, durations, turn counts, and per-run output files persisted under `data/`.
- Long-poll Telegram client — no public HTTPS endpoint needed.

## Architecture

```
                    +----------------------+
   Telegram  <----> | mylittleclaude (PTB) |   (long polling)
                    +----------+-----------+
                               |
                +--------------+--------------+
                |                             |
       host: local                    host: user@remote
       subprocess(claude)         ssh -T user@remote 'cd … && claude …'
                |                             |
                v                             v
        ~/.claude/projects/…           ~/.claude/projects/…
                                       _inbox/ via scp
```

State lives in `data/mylittleclaude.db` (SQLite WAL). Per-run outputs are saved to `data/runs/<topic_id>/<UTC-timestamp>.md`.

## Deploy your own

### 1. Prerequisites

A VPS with:

- Ubuntu 22.04+ (or equivalent), with a regular user. Examples assume `claude`.
- Python 3.11+.
- [Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code) installed for the runtime user, **logged in** (`claude` once interactively to authenticate).
- A Telegram account.

### 2. Bot token + group

1. Talk to [@BotFather](https://t.me/BotFather) on Telegram: `/newbot`. Save the token.
2. Create a new supergroup. In the group settings, enable **Topics**.
3. Add your bot. Promote it to admin with **Manage Topics** permission. (Other admin permissions optional.)
4. *Optional but recommended:* disable the bot's group-privacy mode via BotFather → `/setprivacy` → `Disable`, so the bot can see all messages in the group.

### 3. Install the service

```bash
sudo useradd -m -s /bin/bash claude   # if not already present
sudo -iu claude
cd ~
git clone https://github.com/<you>/mylittleclaude.git
cd mylittleclaude
python3.11 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
cp servers.example.yaml servers.yaml
chmod 600 .env servers.yaml
```

Fill in `.env`:

```
TELEGRAM_BOT_TOKEN=12345:xxxxxxxxxxxxxxxxxxxxxx
ALLOWED_USER_IDS=11111111         # your Telegram user ID (you can find it below)
ALLOWED_GROUP_IDS=                 # leave empty for first boot
```

### 4. Bootstrap the group ID

Start the service in the foreground once to bootstrap:

```bash
.venv/bin/python -m mylittleclaude
```

In the group's General topic, send `/chatid`. The bot replies with the chat ID. Stop the service (Ctrl-C), put the chat ID into `ALLOWED_GROUP_IDS`, restart.

### 5. Configure instances

Edit `servers.yaml` to declare instances. Each instance is a `(host, workdir)` pair:

```yaml
instances:
  controller:
    description: "Claude Code on this controller VPS"
    host: local
    workdir: /home/claude/projects/controller
```

Create the directory first: `mkdir -p /home/claude/projects/controller`.

Restart the service after every edit — there is no hot reload.

### 6. Install as a systemd service

```bash
sudo cp systemd/mylittleclaude.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mylittleclaude
sudo journalctl -u mylittleclaude -f
```

### 7. Use it

In the General topic:

- `/instances` — list configured instances.
- `/new controller` — bot creates a forum topic `controller #1`.

In the new topic:

- Send a message. The bot launches Claude Code with your prompt and posts the result.
- Send a file. The bot saves it to `<workdir>/_inbox/<timestamp>_<filename>`.
- `/info`, `/get <path>`, `/kill`, `/reset`, `/close`.

## Adding more servers

To add a remote worker:

1. Generate a dedicated SSH key on the controller:
   ```bash
   ssh-keygen -t ed25519 -N "" -f /home/claude/.ssh/mylittleclaude_ed25519
   ```
2. Copy the public key to the worker (`ssh-copy-id ...`). The remote user must have Claude Code installed and logged in.
3. Edit `servers.yaml`:
   ```yaml
   instances:
     gpu-box:
       description: "GPU dev box"
       host: claude@gpu-box.internal
       workdir: /home/claude/projects/work
       ssh_key: /home/claude/.ssh/mylittleclaude_ed25519
   ```
4. If the worker's `workdir` is on the controller's filesystem (it normally isn't), add it to `ReadWritePaths=` in the systemd unit and `daemon-reload`. Remote `workdir` paths do **not** need to be in `ReadWritePaths=`.
5. `sudo systemctl restart mylittleclaude`.

The first connection accepts the worker's host key (`StrictHostKeyChecking=accept-new`); subsequent key changes are rejected.

## Security notes

- This bot runs `claude -p --dangerously-skip-permissions`. That means Claude can run any tool the runtime user can. **Do not run mylittleclaude on a box that holds production data or credentials you don't want a misbehaving agent to touch.** Treat instance `workdir`s as sandboxes.
- `.env` and `servers.yaml` must be `chmod 600`. The service refuses to start if either file is world-accessible.
- The bot accepts only `ALLOWED_USER_IDS` in `ALLOWED_GROUP_IDS`. Unauthorized users are dropped silently.
- `/get` is path-traversal-safe: paths containing `..` or starting with `/` are refused. Resolved paths are asserted to remain inside the instance's `workdir`.
- SSH uses key auth only. `host` values are restricted to `[a-zA-Z0-9._@-]+` and shell-quoted into the `ssh` invocation. Passwords are never accepted.
- Logs never contain the bot token. Prompts and results are truncated at 200 chars in any debug log line.

## Roadmap / non-goals

This project is intentionally narrow. The following are explicit non-goals and will be closed without discussion:

- Multi-tenant mode, per-user credential storage, hosted service features.
- Web UI, admin dashboard, analytics/Prometheus/Sentry.
- Slack/Discord/Matrix bridges.
- Cost budgets, per-user rate limiting, billing.
- Streaming partial output to Telegram as Claude works (final result only).
- LLM-generated summaries of Claude's output.
- Auto-resume of killed runs.
- Telegram channels support.

## Contributing

Issues welcome. PRs reviewed at the operator's pace. Keep changes small and aligned with `SPEC.md`. If you want a feature listed under non-goals, fork instead — that's why it's MIT.

## License

MIT — see [LICENSE](LICENSE).
