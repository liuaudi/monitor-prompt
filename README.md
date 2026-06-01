# monitor-claude-code

A localhost proxy + web viewer that captures **exactly what Claude Code sends to the model** — the system prompt, every tool definition, and the full message history — and shows it in a live two-pane UI that refreshes as your session runs.

Use it to verify what Claude Code is actually putting on the wire: audit tool descriptions verbatim, watch the prompt change turn by turn, or check a claim an agent makes about its own toolkit against the raw request.

It reads the **request envelope only** (the input to the model), never the streamed response.

## How it works

Claude Code respects the `ANTHROPIC_BASE_URL` environment variable. Point it at this proxy on `127.0.0.1:9999`. The proxy:

1. Saves each `POST /v1/messages` request body to `taps/<seq>-<timestamp>.json`
2. Forwards it unchanged to `https://api.anthropic.com` and streams the response straight back
3. Serves the viewer at `http://127.0.0.1:9999/`

Because the client talks plain HTTP to localhost, there's **no TLS interception and no CA certificate to install**.

## Requirements

- **Python 3.8+** — standard library only, nothing to `pip install`
- **Claude Code CLI** — the session you want to capture
- **macOS** for the extra features (Desktop/agent-mode capture, the auto-rewrap LaunchAgent). The core proxy + viewer run anywhere Python does.

## No API key needed

This tool stores **no credentials**. It's a pass-through: your Claude Code client already carries its own auth header, and the proxy forwards that header untouched. There is nothing to configure and no `.env` to create.

## Quick start

```bash
git clone https://github.com/liuaudi/monitor-claude-code.git
cd monitor-claude-code

# 1. Start the proxy + open the viewer
bash scripts/start.sh

# 2. In a SEPARATE terminal, launch Claude Code pointed at the proxy
ANTHROPIC_BASE_URL=http://127.0.0.1:9999 claude
```

Every API request from that session lands in `taps/` and appears in the viewer within ~2 seconds. The current session can't be captured after the fact — the environment variable is read when `claude` starts, so you must launch a fresh session.

### Verify it's working

After `bash scripts/start.sh`, the proxy is up if this returns HTML:

```bash
curl -s http://127.0.0.1:9999/ | head -1      # -> <!doctype html>
```

Once a captured session has sent at least one request, this lists the taps:

```bash
curl -s http://127.0.0.1:9999/api/taps         # -> [{"name":"0001-...json", ...}]
```

You can also just open `http://127.0.0.1:9999/` in a browser and watch requests appear live.

### Convenience wrapper

`scripts/monitor-claude` starts the proxy if it isn't running, sets the environment variable for you, and launches Claude Code:

```bash
scripts/monitor-claude              # interactive
scripts/monitor-claude -p "hello"   # one-shot; args pass through to claude
```

### Stop the proxy

```bash
pkill -f monitor-claude-code/scripts/proxy.py
```

It's idle when no client is connected, so you can also just leave it running.

## Capturing Claude Desktop (agent / Cowork mode)

Claude Desktop overrides `ANTHROPIC_BASE_URL` for its embedded CLI, so the env-var trick alone won't catch it. A small shim around the embedded binary handles this:

```bash
bash scripts/install-desktop-shim.sh            # wrap the embedded CLI
bash scripts/install-desktop-shim.sh --status   # show what's wrapped
bash scripts/install-desktop-shim.sh --uninstall # restore originals
```

Quit Claude Desktop (⌘Q) and relaunch afterward. See [SKILL.md](SKILL.md) for the full mechanism, the auto-rewrap LaunchAgent, and the known caveats.

## Privacy

`taps/*.json` contains **everything typed in a captured session, including any secrets you may have pasted**. The `taps/` directory is gitignored so captures never leave your machine. To wipe them:

```bash
rm taps/*.json
```

## Using it as a Claude Code skill

This repo is also a Claude Code skill (the `SKILL.md` at its root). To make Claude Code aware of it, symlink the cloned folder into your skills directory:

```bash
ln -s "$(pwd)" ~/.claude/skills/monitor-claude-code
```

Restart Claude Code so it picks up the new skill. From then on you can ask Claude Code things like *"capture what you're sending to the model"* or *"start monitor-claude-code"* and it will run this skill (read `SKILL.md` for the exact triggers). You can always run the scripts directly instead — the skill wrapper and the manual commands above do the same thing.

> Pasting this repo's GitHub URL into an assistant does **not** install anything on its own. You (or the agent, with shell access) must clone the repo and run the steps above.

[SKILL.md](SKILL.md) is the full reference — it also documents an optional exporter (`scripts/export_reference.py`) that turns a capture into a clean markdown/HTML reference of Claude Code's own prompt and tool definitions, useful when authoring new skills. (By default it writes to `~/Code/skills/_reference/`; override with `--out-dir`.)

## License

MIT
