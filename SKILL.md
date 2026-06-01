---
name: monitor-claude-code
description: Starts a local logging proxy plus an HTML viewer that captures every Claude Code API request to api.anthropic.com — system prompt, tool definitions, message history — and shows them in a two-pane web UI that auto-refreshes as the session runs. Use this skill when the user wants to verify what Claude Code is actually sending to the model, audit tool descriptions and parameter schemas verbatim, watch how the prompt envelope changes turn-by-turn, or rule out hallucinations in a Claude assistant's claims about its own toolkit. The captured data is the raw wire payload, not anything Claude reports about itself.
---

# monitor-claude-code

A localhost forwarding proxy + viewer that taps Claude Code's API requests so you can read the actual wire-level prompt yourself, in real time, without trusting any agent's summary of it.

## What it does

1. Runs a single HTTP server on port `9999` that:
   - Forwards `POST /v1/messages` (and any other path) to `https://api.anthropic.com`, streaming the response back unmodified
   - Saves every request body to `taps/<seq>-<timestamp>.json` before forwarding
   - Serves a viewer UI at `http://127.0.0.1:9999/`
2. The viewer auto-refreshes and shows two panes:
   - **Left:** the prompt envelope — system prompt + every tool's full description and parameter schema
   - **Right:** the messages array (user / assistant / tool_use / tool_result blocks)
   - **Top:** a timeline of captured requests; click any to inspect

Because Claude Code respects the `ANTHROPIC_BASE_URL` env var, no TLS interception or CA cert install is needed — the client sends plain HTTP to localhost, and the proxy forwards it over HTTPS. mitmproxy was considered and rejected: its core value is HTTPS interception, which is unnecessary here, and we want a custom UI anyway.

## How to invoke

```bash
bash "$SKILL_DIR/scripts/start.sh"
```

`$SKILL_DIR` resolves to wherever the skill is installed — `~/Code/skills/monitor-claude-code` if running from source, `~/.claude/skills/monitor-claude-code` if running from the installed symlink.

The script will:
- Start the proxy as a background process (idempotent — won't double-start if already running)
- Open `http://127.0.0.1:9999/` in the default browser
- Print the env var to set on the next Claude Code session

## Capturing a session

The current Claude Code session **cannot be tapped retroactively** — env vars are read at process start. Open a new terminal and start `claude` with the proxy URL:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:9999 claude
```

Or export it for the shell:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:9999
claude
```

Every API request from that session will land in `taps/` and appear in the viewer within ~2 seconds.

A wrapper at `scripts/monitor-claude` (also installed at `~/.local/bin/monitor-claude`) sets the env var for you and starts the proxy first if it isn't running:

```bash
monitor-claude              # interactive
monitor-claude -p "hello"   # one-shot, args pass through to claude
```

## Capturing a Claude Desktop session (agent / Cowork mode)

Claude Desktop **explicitly overrides** `ANTHROPIC_BASE_URL` when spawning its embedded `claude` subprocess (verified by reading `Claude.app/Contents/Resources/app.asar` — function `G4()` builds the subprocess env from internal config, ignoring whatever launchd / shell / Settings provide). So the env var trick alone won't capture Desktop sessions.

The workaround is a one-line shim around the embedded CLI binary:

```bash
bash "$SKILL_DIR/scripts/install-desktop-shim.sh"
```

Per Desktop CLI version found under `~/Library/Application Support/Claude/claude-code/<version>/`:

1. Renames the real `claude` binary to `claude.real`
2. Drops a 10-line shell wrapper in its place that — only when the proxy is listening on `127.0.0.1:9999` — sets `ANTHROPIC_BASE_URL=http://127.0.0.1:9999` and execs `claude.real`. When the proxy is down, it's a transparent pass-through, so Desktop keeps working normally if you forget to start the proxy.

After installing, **fully quit Claude Desktop (⌘Q) and relaunch.** The next agent-mode session will spawn the shimmed CLI and its API calls will appear in the viewer.

```bash
bash scripts/install-desktop-shim.sh --status      # show which versions are shimmed
bash scripts/install-desktop-shim.sh --uninstall   # restore originals
```

Re-run after Desktop auto-updates its embedded CLI (each version lives in its own directory; the new version starts un-shimmed). The script is idempotent and only touches versions that aren't already wrapped.

### Auto-rewrap on Desktop updates (one-time setup)

To avoid having to remember the previous step, install a launchd LaunchAgent that watches the `claude-code/` directory for new version subfolders and runs the shim installer automatically:

```bash
bash "$SKILL_DIR/scripts/install-launchagent.sh"           # install + load
bash "$SKILL_DIR/scripts/install-launchagent.sh" --status  # check load state + log
bash "$SKILL_DIR/scripts/install-launchagent.sh" --uninstall
```

The agent uses `WatchPaths` on `~/Library/Application Support/Claude/claude-code/`, which fires within seconds whenever Desktop drops a new version on disk. The install script is idempotent, so the agent re-runs are no-ops until there's something new to wrap. Verified working by simulating a fake new version dir — auto-shim within ~1 second.

Logs go to `/tmp/monitor-claude-code-shim-watch.log`.

**Caveats:**
- Captures only the *agent mode* subprocess. Desktop's chat panel itself uses its own API client (Electron-side) that doesn't go through the embedded CLI — those requests are not captured.
- macOS code signing on the embedded `claude.app` bundle becomes invalid after the swap, but Desktop spawns the binary directly via `posix_spawn` and there's no Gatekeeper check at that path.

## Stopping the proxy

```bash
pkill -f monitor-claude-code/scripts/proxy.py
```

Or just leave it running — it's idle when no client is connected.

## What's in a tap file

Each `taps/<seq>-<timestamp>.json` has two top-level keys:

- `_meta` — capture metadata: `captured_at`, `path`, `method`
- `request` — the verbatim JSON body of the `POST /v1/messages` request

The fields you care about live **under `request`**:

- `request.system` — the system prompt (string or array of content blocks)
- `request.tools` — array of every tool the model can see this turn, each with `name`, `description`, `input_schema`
- `request.messages` — the conversation history sent for this turn
- `request.model`, `request.max_tokens`, `request.temperature`, etc.

The viewer pretty-prints all of these. For raw inspection: `jq '.request.tools[] | {name, description}' taps/0001-*.json`.

## Export a design reference for skill authors

Use this when you're about to build a new skill (e.g. via `/anthropic-skills:skill-creator`) and want Claude Code's own prompt + tool definitions as a writing reference. The exporter turns a tap into a clean markdown file you can `@`-reference into any conversation.

### Default: auto-archive

```bash
python3 "$SKILL_DIR/scripts/export_reference.py"
```

With no flags, the exporter:

1. Picks the latest tap from `taps/`
2. Reads the tap's `cc_entrypoint` and `cc_version` to identify the surface (`cli` or `desktop`) and the version
3. Writes to `~/Code/skills/_reference/cc-reference-<surface>-<version>-<UTC timestamp>.md` — a permanent, never-overwritten archive entry
4. Updates two convenience symlinks:
   - `cc-reference-<surface>.md` → the file just written (latest for that surface)
   - `cc-reference.md` → `cc-reference-cli.md` (the canonical pointer always tracks the latest CLI capture, regardless of which surface was just captured)
5. Skips the write — but still refreshes the symlinks — when the new content is byte-identical to the most recent capture for the same surface. So idle invocations don't pile up duplicate files, but every meaningful change is preserved.

This means the archive grows as a corpus you can later mine to study how Anthropic evolves tool descriptions over time. Dropped tools (like `RemoteTrigger` in 2.1.138), renamed parameters, restructured prose — all preserved in the historical files.

### Override flags

- `--tap PATH` — export a specific tap instead of the latest
- `-o PATH` — write to an exact file path, bypassing auto-archive (one-off exports)
- `--stdout` — print to stdout, don't touch the archive
- `--out-dir PATH` — auto-archive into a different directory
- `--keep-reminders` — keep `<system-reminder>` blocks in the system prompt (they're stripped by default)
- `--no-schemas` — drop parameter tables (smaller output)

### Output: two formats per capture

Every export produces **both** an `.md` and an `.html` file for the same data — different formats for different audiences:

- **`.md`** (~100–130KB) — plain markdown. Best for @-reference into `/anthropic-skills:skill-creator` and other agent-context use. Sections: metadata, system prompt verbatim, tool index, per-tool description + parameter table.
- **`.html`** (~500KB) — interactive single-file page. Best for humans reading and grabbing JSON schemas. Sidebar nav grouped by category, live filter, per-tool "Copy JSON" button that puts `{name, description, input_schema}` on the clipboard, system prompt blocks in collapsible `<details>`.

The `.html` is the answer to "I want to actually read this thing and copy individual tool schemas." The `.md` remains the canonical agent-facing format. Both get refreshed atomically — if monitor-claude-code detects no change since the last capture for this surface+version, it skips writing both.

Sizes are larger for `.html` because of embedded CSS, JS, and inline JSON blobs (one per tool, for the copy-button to grab). Single file, no external dependencies — works offline once written.

### Eager vs deferred classification (with strong caveat)

Both formats label each tool as **eager**, **loaded**, **deferred-resolved**, or **deferred-pending**, based on authoritative wire signals from the captured API request.

**Background.** Anthropic's API supports tool-level deferral via a [`defer_loading: true`](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool#deferred-tool-loading) flag on each tool in `tools[]`. When a tool has this flag, the API does not include its definition in the system-prompt prefix sent to the model. Claude must call `ToolSearch` to discover and load it. Claude Code's harness also injects a custom `<system-reminder>` listing deferred tool names so the model knows what's available (the literal phrase "*The following deferred tools are now available via ToolSearch*" appears in the CLI binary's source strings).

**Four states per tool:**

| State | Meaning | Detection |
|---|---|---|
| `eager` | Schema always in context for this session. Confirmed eager because the session uses deferral elsewhere yet this tool's defer flag is unset. | `tools[i]` present without `defer_loading: true` AND the session has at least one other deferred signal |
| `loaded` | Schema present in `tools[]` this turn, but we can't tell whether this tool is eager-by-default OR a deferred tool that was resolved earlier in the session. Only appears when no deferral signal of any kind was found in this capture. | `tools[i]` present without `defer_loading: true`, AND no deferral signal anywhere in the capture |
| `deferred-resolved` | Originally deferred but loaded into context this turn (already searched/resolved) | `tools[i].defer_loading: true`, OR named in deferred-tools reminder AND in `tools[]` |
| `deferred-pending` | Named in the deferred-tools reminder but no schema this turn — would be fetched on demand via ToolSearch | Named in reminder, NOT in `tools[]` |

**Important caveat — what an "all loaded, no deferral" capture does NOT mean.**

Some captured turns show every tool as `loaded` with no deferral signal at all. **This does NOT mean Claude Code never defers tools.** Many Claude Code sessions DO use deferral — you can confirm this in your own current session by searching the system prompt for the phrase "Some tools are deferred and not listed above". The phrase is also present in the compiled CLI binary.

What we **don't yet know** is *why* some sessions use deferral and some don't. Hypotheses we haven't validated:

- A tool-count threshold (deferral kicks in only above N tools in registry)
- The entrypoint (Desktop vs CLI vs SDK) chooses different defaults
- A specific Claude Code version turned deferral on/off
- An account flag or org config
- MCP-server config flags (the CLI binary mentions a per-server `force_all` option that disables deferral for that server's tools, but we have not observed this flag being set in any captured tap — it's a configurable option, not an inferred user choice)

The classification report includes a **"How to read this capture"** interpretation banner at the top so any agent or human reading it knows whether the data shows a deferral-using session or a no-deferral-signal session, and warns them not to over-generalize from a single tap.

**In the .md.** Each tool's section gets a `**Classification:** ... — <reason>` line that spells out which signal classified it. The top-of-file breakdown shows the counts plus the interpretation banner. Deferred-pending tools (named but no schema this turn) get a dedicated section at the bottom.

**In the .html.** Each tool card gets a colored badge — green `Eager`, blue `Loaded`, amber `Deferred·loaded`, orange `Deferred·pending`. Sidebar nav shows the same badges. Filter chips at the top: `All` / `Eager` / `Loaded` / `Deferred (loaded)` / `Deferred (pending)`. A yellow interpretation banner under the header restates the caveat for non-deferral captures.

The classification is recomputed on every export — no manual maintenance, no stale state. Captures from sessions that DO use deferral show the proper split. Captures that don't say so plainly with the interpretation banner, and an agent reading the file will not be misled.

## Skill-authoring workflow

1. **Capture once per Claude Code release.** Start the proxy, open a fresh Claude Code session pointed at it (see below), run the exporter. The archive accumulates one file per (surface, version, timestamp).
2. **When invoking `/anthropic-skills:skill-creator`**, tell it: *"Read `~/Code/skills/_reference/cc-reference.md` first — that's how Claude Code itself structures tool descriptions and I want this skill to follow those patterns."* The canonical symlink always resolves to the latest CLI capture.
3. **`list-tools` tells you when to recapture.** Its diff output flags Claude Code version bumps; that's the trigger to rerun the proxy and refresh the archive.

## Which surface gets captured

The proxy captures whatever Claude Code session points `ANTHROPIC_BASE_URL` at it. The skill doesn't fork to a different surface or fake a CLI session from inside Desktop — that route lies. If you want to capture a CLI session, you launch the CLI yourself, with the env var set:

```bash
# In a plain Terminal (not inside Claude Code Desktop):
ANTHROPIC_BASE_URL=http://127.0.0.1:9999 claude
```

And to capture Desktop, point a fresh Desktop session at the proxy. Whichever surface runs the session, that's what lands in the tap.

## Why CLI is the canonical reference (not Desktop)

For *skill design imitation*, the CLI is cleaner. The Desktop capture includes whatever MCP servers the user has connected on claude.ai (Gmail, Drive, Slack, etc.) — those are dozens of extra tool definitions written by third parties, not useful as imitation targets for canonical Anthropic prose. The CLI also exposes a few file-system primitives (`Glob`, `Grep`) that don't appear in the Desktop bundle's main path.

Both archives are valuable — the Desktop one shows how MCP tools are described, the CLI one shows Anthropic's own writing conventions. The `cc-reference.md` symlink picks the CLI as the default skill-design reference because that's what you want 95% of the time.

## Privacy note

Tap files contain everything you've said in the session — including any secrets you may have pasted. The `taps/` directory is gitignored. Wipe it before sharing the skill folder:

```bash
rm "$SKILL_DIR"/taps/*.json
```

## Future work (deliberately out of v1 scope)

- Diff view between any two requests (which tool's description changed?)
- Flagging — mark a tool as "verify against published docs", saved per-tool, surfaced in the viewer
- Change monitor — alert when a tool definition differs from N requests ago
- Export selected envelope as a single shareable JSON

## Limitations

- Only captures Claude Code's CLI surface. Desktop, web, and IDE plugins use different transports.
- Streaming SSE responses are forwarded transparently but the response itself is not stored — only the request envelope. (We assume the user is verifying *input* to the model, not output.)
- If Claude Code retries on a 5xx, every attempt produces a separate tap file. The sequence numbers reflect that, not unique conversation turns.
