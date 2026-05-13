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

<img width="1672" height="941" alt="image" src="https://github.com/user-attachments/assets/474e892a-333d-4989-8413-ceec8c83963e" />

State lives in `data/mylittleclaude.db` (SQLite WAL). Per-run outputs are saved to `data/runs/<topic_id>/<UTC-timestamp>.md`.

## Install

The recommended path is the installer — it handles prereqs (apt/dnf), Claude Code, the venv, the wizard, the systemd unit, and the `mylittleclaude-setup` command in one shot.

### Quick install (clone + script)

```bash
git clone https://github.com/yozha/mylittleclaude.git ~/mylittleclaude
cd ~/mylittleclaude
./install.sh
```

### Curl|bash install

If you trust the repo, this is the one-liner equivalent:

```bash
curl -fsSL https://raw.githubusercontent.com/yozha/mylittleclaude/main/bootstrap.sh | bash
```

`bootstrap.sh` clones, checks out the latest release tag, and execs `install.sh`. Set `MYLITTLECLAUDE_TAG=vX.Y.Z` to pin a specific version, or `MYLITTLECLAUDE_DIR=/path` to change the install location (default `~/mylittleclaude`).

### What the installer does

1. Refuses to run as root, checks for `sudo` (or `--no-sudo` to skip systemd).
2. Detects your distro (Debian/Ubuntu or Fedora/RHEL/Rocky/Alma).
3. Installs missing prereqs: `python3.11+`, `python3-venv`, `git`, `curl`, `nodejs >= 20`, `npm`, `rsync`, `openssh-client`.
4. Installs Claude Code via `npm install -g @anthropic-ai/claude-code`.
5. Asks you to authenticate Claude Code (manual step, see prompt). You can defer this.
6. Creates `.venv` and `pip install -e .`.
7. Runs the configuration wizard.
8. Installs `systemd/mylittleclaude.service` (paths rewritten for your install dir) and `enable`s it.
9. Symlinks `~/.local/bin/mylittleclaude-setup` to the venv entry point.

### The wizard

The wizard collects:

- **Bot token** from [@BotFather](https://t.me/BotFather) (instructions shown inline).
- **Your Telegram user ID** from [@userinfobot](https://t.me/userinfobot).
- **At least one instance** — a `(host, workdir)` pair where Claude will run.
- **Group ID** of the supergroup you'll use the bot from. Defer this if you haven't created the group yet — the bot will start in *bootstrap mode* and you can complete setup by re-running `mylittleclaude-setup`.

Type `back`, `skip`, or `quit` at any prompt. Every field you skip becomes a *deferred field* and is surfaced in the final summary, so you know exactly what's still needed.

### Use it

In the General topic of your supergroup:

- `/chatid` — bot replies with the chat ID. Use this if you skipped the group-ID step.
- `/instances` — list configured instances.
- `/new <instance>` — bot creates a forum topic `<instance> #1`.

In the new topic:

- Send a message. The bot launches Claude Code with your prompt and posts the result.
- Send a file. The bot saves it to `<workdir>/_inbox/<timestamp>_<filename>`.
- `/info`, `/get <path>`, `/kill`, `/reset`, `/close`.

## Completing a deferred setup

If you installed in bootstrap mode (no group ID yet):

1. Create the Telegram supergroup, add the bot as admin with **Manage Topics**, enable Topics.
2. In the group's General topic, send `/chatid`. The bot replies with the chat ID.
3. Re-run `mylittleclaude-setup` and paste the chat ID. Existing answers are pre-filled — press Enter to keep them.

The same flow works for any other deferred field (token, user ID, instance).

## Update / rollback

```bash
mylittleclaude-setup update                   # update to latest tag
mylittleclaude-setup update --tag v0.3.0      # pin a tag
mylittleclaude-setup update --branch main     # dev mode (warned)
mylittleclaude-setup rollback                 # pick a previous backup
```

The update flow stops the service, backs up the install dir (excluding `.venv`, `data/`, `.git/`) plus `.env` and `servers.yaml` to `.backup/vX.Y.Z-<timestamp>/`, then checks out, reinstalls, migrates the DB, and restarts. If any step fails, it auto-rolls back.

The last 5 backups are kept; older ones are pruned.

DB migrations are forward-only. Rolling back from a version that added a schema change will work for the rollback itself but the older bot may refuse to start against the newer schema — in that case, restore from a data backup or stay on the newer version.

## Uninstall

```bash
mylittleclaude-setup uninstall
```

Asks four separate questions:

1. Stop + remove the systemd unit and the `mylittleclaude-setup` symlink? (required to proceed)
2. Also delete the data directory (`data/`)?
3. Also delete `.env` and `servers.yaml`?
4. Also delete the install dir (`~/mylittleclaude`)?

Each is independent — say no to keep that part.

## Status / logs

```bash
mylittleclaude-setup status   # service active? configured? deferred fields?
mylittleclaude-setup logs     # journalctl -u mylittleclaude -f
mylittleclaude-setup version  # what version is installed
```

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

## Appendix: manual install (no installer)

If you're on a non-systemd or non-Debian/RHEL system, or you want to install without running `install.sh`, here's the manual path. The installer just automates these steps.

### 1. Prereqs

A VPS with:

- Ubuntu 22.04+ (or equivalent), with a regular user. Examples assume `claude`.
- Python 3.11+.
- Node.js 20+ and npm.
- [Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code) installed for the runtime user (`npm install -g @anthropic-ai/claude-code`) and **logged in** (run `claude` once interactively).
- A Telegram account.

### 2. Bot token + group

1. Talk to [@BotFather](https://t.me/BotFather) on Telegram: `/newbot`. Save the token.
2. Create a new supergroup. In the group settings, enable **Topics**.
3. Add your bot. Promote it to admin with **Manage Topics** permission.
4. *Optional but recommended:* disable the bot's group-privacy mode via BotFather → `/setprivacy` → `Disable`.

### 3. Clone + venv

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

### 4. Fill in `.env`

```
TELEGRAM_BOT_TOKEN=12345:xxxxxxxxxxxxxxxxxxxxxx
ALLOWED_USER_IDS=11111111         # your Telegram user ID
ALLOWED_GROUP_IDS=                 # leave empty for first boot
```

Find your user ID by messaging [@userinfobot](https://t.me/userinfobot).

### 5. Bootstrap the group ID

Start the service in the foreground once:

```bash
.venv/bin/python -m mylittleclaude
```

In the group's General topic, send `/chatid`. The bot replies with the chat ID. Stop the service (Ctrl-C), put the chat ID into `ALLOWED_GROUP_IDS`, restart.

### 6. Configure instances

Edit `servers.yaml`:

```yaml
instances:
  controller:
    description: "Claude Code on this controller VPS"
    host: local
    workdir: /home/claude/projects/controller
```

Create the directory: `mkdir -p /home/claude/projects/controller`.

Restart the service after every edit — there is no hot reload.

### 7. Install as a systemd service (optional)

```bash
sudo cp systemd/mylittleclaude.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mylittleclaude
sudo journalctl -u mylittleclaude -f
```

On non-systemd systems, run `.venv/bin/python -m mylittleclaude` under your supervisor of choice (runit, OpenRC, supervisord, etc.). It logs to stdout/stderr.

### Manual smoke test

After install, verify each of these:

- `mylittleclaude-setup status` shows everything ✓ and `Service active: yes`.
- In the group's General topic, `/instances` lists your configured instances.
- `/new <instance>` creates a new forum topic.
- In the new topic, sending a plain message produces a `⏳ Working...` message that updates with the final result.
- Sending a file in the topic produces `📥 Saved to _inbox/...`.
- Killing a long prompt with the `[Kill]` button or `/kill` produces a `🛑 Killed at ...` message.
- `mylittleclaude-setup update` updates to a newer tag and preserves your `.env` / `servers.yaml` / `data/`.
- `mylittleclaude-setup rollback` lists backups and restores one.
- `mylittleclaude-setup uninstall` removes the unit cleanly.
