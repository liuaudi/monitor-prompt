#!/usr/bin/env python3
"""Turn a captured Claude Code API tap into a clean markdown design reference.

The goal: produce a single markdown file that another skill-author (human
or agent) can read to learn *how Anthropic writes Claude Code's prompt
and tool definitions* — verbatim descriptions, parameter schemas, the
shape of the system prompt — without any session-specific noise.

This is the file you @-reference into /skill-creator so the generated
skill follows the same idioms Claude Code itself uses.

Usage:
    python3 export_reference.py [--tap PATH] [--strip-reminders] [--no-schemas] > out.md

Defaults:
    --tap         = latest tap in ../taps/
    --strip-reminders = drop <system-reminder> blocks from system prompt
    --no-schemas  = off (schemas included by default; they're the most
                    instructive part for skill authors)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
TAPS_DIR = SKILL_DIR / "taps"

# Where archived references live by default. Each capture writes a
# version-and-timestamp-tagged file here so the corpus grows over time;
# convenience symlinks (cc-reference.md, cc-reference-<surface>.md) point
# at the most recent entry for each role.
DEFAULT_OUT_DIR = Path.home() / "Code" / "skills" / "_reference"

# The "primary" reference that skill-authoring workflows should @-reference.
# By convention this is the CLI capture — the Desktop capture is polluted
# with whatever MCP servers the user has connected on claude.ai, which
# aren't useful as imitation targets for canonical Anthropic tool prose.
PRIMARY_SURFACE = "cli"

# Tool categorization for the index. The grouping is editorial — these are
# the buckets a skill author actually thinks in. Order within a group is
# preserved as listed (most fundamental first). Anything unmatched falls
# into "Other".
GROUPS: list[tuple[str, list[str]]] = [
    ("File operations", [
        "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep",
    ]),
    ("Shell execution", ["Bash", "PowerShell"]),
    ("Process / output monitoring", ["Monitor"]),
    ("Web access", ["WebFetch", "WebSearch"]),
    ("Subagent spawning", ["Agent", "Task"]),
    ("Background task management", [
        "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput",
        "TaskStop", "TaskDelete",
    ]),
    ("Scheduling & notification", [
        "ScheduleWakeup", "CronCreate", "CronDelete", "CronList",
        "PushNotification", "RemoteTrigger",
    ]),
    ("Plan mode", ["EnterPlanMode", "ExitPlanMode"]),
    ("Worktree control", ["EnterWorktree", "ExitWorktree"]),
    ("Skills, todos, tool discovery", [
        "Skill", "ToolSearch", "TodoWrite", "AskUserQuestion",
    ]),
    ("MCP resources", ["ListMcpResourcesTool", "ReadMcpResourceTool"]),
    ("Onboarding / misc", ["ShareOnboardingGuide"]),
]

# System-prompt blocks we always drop because they're transport metadata,
# not part of the design we want to study.
TRANSPORT_BLOCK_RE = re.compile(r"^x-anthropic-billing-header:")


def latest_tap() -> Path | None:
    taps = sorted(TAPS_DIR.glob("*.json"))
    return taps[-1] if taps else None


def system_blocks(system_field) -> list[str]:
    if isinstance(system_field, str):
        return [system_field]
    out = []
    for block in system_field or []:
        if isinstance(block, dict) and "text" in block:
            out.append(block["text"])
    return out


def strip_session(text: str, *, strip_reminders: bool) -> str:
    """Remove session-specific bits so the reference is reusable.

    - Optionally strip every <system-reminder>...</system-reminder> block
    - Strip the trailing claudeMd/userEmail/currentDate appendix if present
      (its presence makes the reference look polluted; the model uses it
      at runtime but it's not part of the design)
    """
    if strip_reminders:
        text = re.sub(
            r"<system-reminder>.*?</system-reminder>\s*",
            "",
            text,
            flags=re.DOTALL,
        )
    # Drop the claudeMd/userEmail/currentDate appendix; it always starts
    # with a header line like "# claudeMd" or "# currentDate"
    text = re.sub(
        r"\n+As you answer the user's questions, you can use the following context:.*$",
        "",
        text,
        flags=re.DOTALL,
    )
    return text.rstrip() + "\n"


def fmt_schema(schema: dict) -> str:
    """Render a tool's input_schema as compact markdown. The schema is the
    most useful single piece for an author copying patterns — show param
    names, types, requireds, and one-line descriptions.
    """
    if not isinstance(schema, dict):
        return "_(no schema)_"
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if not props:
        return "_(no parameters)_"
    lines = ["| Param | Type | Required | Description |", "|---|---|---|---|"]
    for name, spec in props.items():
        t = spec.get("type") or spec.get("anyOf") or "?"
        if isinstance(t, list):
            t = " \\| ".join(map(str, t))
        elif not isinstance(t, str):
            t = "complex"
        desc = (spec.get("description") or "").strip()
        # squash newlines and pipes so markdown table survives
        desc = desc.replace("|", "\\|").replace("\n", " ")
        if len(desc) > 280:
            desc = desc[:277] + "…"
        req = "yes" if name in required else "—"
        lines.append(f"| `{name}` | {t} | {req} | {desc} |")
    return "\n".join(lines)


def group_for(name: str) -> str:
    for group, names in GROUPS:
        if name in names:
            return group
    if name.startswith("mcp__"):
        return "MCP server tools"
    return "Other / unmatched"


def render(tap: dict, *, strip_reminders: bool, include_schemas: bool, classification: dict | None = None, summary: dict | None = None) -> str:
    meta = tap.get("_meta") or {}
    req = tap.get("request") or tap  # be lenient
    model = req.get("model", "unknown")
    tools = req.get("tools") or []
    sys_blocks = system_blocks(req.get("system"))

    # Find a cc_version hint in the billing header if present
    cc_version = "unknown"
    cc_entrypoint = "unknown"
    for b in sys_blocks:
        m = re.search(r"cc_version=([\w.\-]+)", b)
        if m:
            cc_version = m.group(1)
        m = re.search(r"cc_entrypoint=([\w\-]+)", b)
        if m:
            cc_entrypoint = m.group(1)

    # Drop transport-only blocks for the main rendering
    visible_blocks = [b for b in sys_blocks if not TRANSPORT_BLOCK_RE.match(b.strip())]

    out: list[str] = []
    out.append("# Claude Code design reference\n")
    out.append(
        "_Verbatim system prompt + tool definitions captured from a live "
        "Claude Code API request. Use this as a reference when authoring "
        "skills, subagents, or tools to follow the same writing conventions."
        "_\n"
    )
    out.append("## Metadata\n")
    out.append(f"- Captured at: `{meta.get('captured_at', '?')}`")
    out.append(f"- Model: `{model}`")
    out.append(f"- Claude Code version: `{cc_version}`")
    out.append(f"- Entrypoint: `{cc_entrypoint}`")
    out.append(f"- Tool count: **{len(tools)}**")
    out.append(f"- System prompt blocks: **{len(visible_blocks)}**")
    if summary:
        c = summary["counts"]
        bits = [f"{c[k]} {k}" for k in ("eager", "deferred") if c.get(k)]
        if bits:
            out.append(f"- Tool breakdown: **{', '.join(bits)}**")
    out.append("")
    if summary and summary.get("interpretation"):
        out.append("> **How to read this capture:** " + summary["interpretation"])
        out.append("")

    # System prompt section
    out.append("## System prompt (verbatim)\n")
    out.append(
        "Multiple text blocks are joined in order. Session-specific bits "
        "(reminders, user context appendix) " +
        ("**have been stripped**." if strip_reminders else "are preserved.") +
        "\n"
    )
    for i, block in enumerate(visible_blocks, 1):
        cleaned = strip_session(block, strip_reminders=strip_reminders)
        if not cleaned.strip():
            continue
        out.append(f"### Block {i}\n")
        out.append("```")
        out.append(cleaned.rstrip())
        out.append("```\n")

    # Tool index
    out.append("## Tool index\n")
    by_group: dict[str, list[str]] = {}
    tool_lookup = {t["name"]: t for t in tools if isinstance(t, dict) and "name" in t}
    for name in tool_lookup:
        by_group.setdefault(group_for(name), []).append(name)

    # Render groups in the canonical GROUPS order, then any extras
    seen_groups = set()
    for group_name, _ in GROUPS:
        if group_name in by_group:
            seen_groups.add(group_name)
            names = sorted(by_group[group_name])
            out.append(f"- **{group_name}** — " + ", ".join(f"`{n}`" for n in names))
    for group_name in sorted(by_group):
        if group_name in seen_groups:
            continue
        names = sorted(by_group[group_name])
        out.append(f"- **{group_name}** — " + ", ".join(f"`{n}`" for n in names))
    out.append("")

    # Per-tool detail, grouped in the same order
    out.append("## Tool definitions\n")
    out.append(
        "_Each tool's `description` field is the actual prose the model "
        "sees in the system prompt. This is the writing to imitate when "
        "describing your own skills' tools._\n"
    )
    rendered_names: set[str] = set()

    def render_one(name: str):
        if name in rendered_names:
            return
        rendered_names.add(name)
        tool = tool_lookup[name]
        desc = (tool.get("description") or "").rstrip()
        info = (classification or {}).get(name) or {"label": "unknown", "reason": "no signal"}
        out.append(f"### `{name}`\n")
        out.append(f"**Classification:** `{info['label']}` — {info['reason']}\n")
        out.append(f"**Description:**\n")
        out.append("```")
        out.append(desc)
        out.append("```\n")
        if include_schemas:
            out.append("**Parameters:**\n")
            out.append(fmt_schema(tool.get("input_schema") or {}))
            out.append("")

    for group_name, ordered_names in GROUPS:
        names_here = [n for n in ordered_names if n in tool_lookup]
        if not names_here:
            continue
        out.append(f"### Group: {group_name}\n")
        for name in names_here:
            render_one(name)

    # Then everything else (MCP, mcp__*, unmatched)
    leftover = [n for n in tool_lookup if n not in rendered_names]
    if leftover:
        out.append("### Group: Other / MCP / unmatched\n")
        for name in sorted(leftover):
            render_one(name)

    # Tools that are deferred but not in this turn's tools[] (reminder-only)
    # — list them by name in a small footer since we have no schema for them.
    if classification:
        no_schema = sorted(
            n for n, info in classification.items()
            if info.get("label") == "deferred" and n not in tool_lookup
        )
        if no_schema:
            out.append("## Deferred tools without schema in this turn\n")
            out.append(
                "_These tool names appeared only in the deferred-tools system-reminder, "
                "not in `tools[]`. The model knows they exist; their full schema would "
                "be loaded if/when the model calls ToolSearch for them._\n"
            )
            for name in no_schema:
                out.append(f"- `{name}`")
            out.append("")

    return "\n".join(out).rstrip() + "\n"


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Claude Code reference — {surface} {version}</title>
<style>
  :root {{
    --bg: #fafafa;
    --fg: #1f2328;
    --muted: #57606a;
    --border: #d0d7de;
    --card: #ffffff;
    --code-bg: #f6f8fa;
    --accent: #0969da;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0d1117;
      --fg: #e6edf3;
      --muted: #8b949e;
      --border: #30363d;
      --card: #161b22;
      --code-bg: #161b22;
      --accent: #58a6ff;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
    color: var(--fg);
    background: var(--bg);
    display: grid;
    grid-template-columns: 280px 1fr;
    min-height: 100vh;
  }}
  aside {{
    position: sticky;
    top: 0;
    height: 100vh;
    overflow-y: auto;
    background: var(--card);
    border-right: 1px solid var(--border);
    padding: 20px 16px;
  }}
  aside h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin: 16px 0 8px; }}
  aside ul {{ list-style: none; padding: 0; margin: 0; }}
  aside li.group {{ font-size: 11px; text-transform: uppercase; color: var(--muted); margin-top: 12px; }}
  aside a {{ display: block; padding: 4px 8px; color: var(--fg); text-decoration: none; border-radius: 4px; font-size: 13px; }}
  aside a:hover {{ background: var(--code-bg); }}
  #filter {{
    width: 100%;
    padding: 8px 10px;
    margin-bottom: 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg);
    color: var(--fg);
    font: inherit;
  }}
  main {{ padding: 24px 40px; max-width: 1000px; }}
  header.page {{ border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }}
  header.page h1 {{ margin: 0 0 4px; font-size: 24px; }}
  header.page .meta {{ color: var(--muted); font-size: 14px; }}
  header.page .meta span {{ margin-right: 18px; }}
  details {{ margin-bottom: 16px; }}
  details summary {{ cursor: pointer; padding: 8px 0; font-weight: 600; }}
  pre {{
    background: var(--code-bg);
    border: 1px solid var(--border);
    padding: 14px 16px;
    border-radius: 6px;
    overflow-x: auto;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 13px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-wrap: break-word;
    margin: 0 0 12px;
  }}
  article.tool {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px 24px;
    margin-bottom: 16px;
  }}
  article.tool h3 {{ margin: 0 0 12px; display: flex; align-items: baseline; justify-content: space-between; gap: 12px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 18px; }}
  article.tool h3 .group-tag {{ font-family: -apple-system, system-ui, sans-serif; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: normal; }}
  button.copy {{
    font: inherit;
    font-size: 12px;
    padding: 4px 10px;
    background: transparent;
    color: var(--accent);
    border: 1px solid var(--border);
    border-radius: 4px;
    cursor: pointer;
  }}
  button.copy:hover {{ background: var(--code-bg); }}
  button.copy.copied {{ color: green; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; vertical-align: middle; margin-left: 6px; }}
  .badge.eager {{ background: #dafbe1; color: #1a7f37; }}
  .badge.deferred {{ background: #fff8c5; color: #9a6700; }}
  .badge.unknown {{ background: #eaeef2; color: #57606a; }}
  @media (prefers-color-scheme: dark) {{
    .badge.eager {{ background: #033a16; color: #56d364; }}
    .badge.deferred {{ background: #4d2d00; color: #e3b341; }}
    .badge.unknown {{ background: #21262d; color: #8b949e; }}
  }}
  .interpretation-banner {{
    margin: 12px 0 0;
    padding: 12px 16px;
    background: #fff8c5;
    border-left: 4px solid #d4a72c;
    border-radius: 4px;
    font-size: 13px;
    line-height: 1.5;
    color: #59410a;
  }}
  .interpretation-banner strong {{ color: #59410a; }}
  @media (prefers-color-scheme: dark) {{
    .interpretation-banner {{ background: #2d2009; border-left-color: #d4a72c; color: #f0d568; }}
    .interpretation-banner strong {{ color: #f0d568; }}
  }}
  .deferred-pending-block {{ background: var(--code-bg); border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px; margin: 24px 0; }}
  .deferred-pending-block h3 {{ margin: 0 0 8px; font-size: 14px; }}
  .deferred-pending-block code {{ display: inline-block; margin: 2px 4px 2px 0; padding: 2px 8px; background: var(--card); border-radius: 4px; font-size: 12px; }}
  .filter-chips {{ margin: 6px 0 12px; display: flex; gap: 6px; flex-wrap: wrap; }}
  .filter-chips button {{ font: inherit; font-size: 12px; padding: 4px 10px; border: 1px solid var(--border); background: transparent; color: var(--fg); border-radius: 12px; cursor: pointer; }}
  .filter-chips button.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
  .summary-line {{ font-size: 13px; color: var(--muted); margin: 4px 0 14px; }}
  .summary-line .badge {{ margin: 0 4px 0 0; }}
  aside li a .badge {{ font-size: 9px; padding: 1px 5px; margin-left: 4px; }}
  h4 {{ margin: 16px 0 8px; font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0 0 8px; font-size: 13px; }}
  th, td {{ padding: 6px 10px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }}
  th {{ background: var(--code-bg); font-weight: 600; }}
  td code {{ background: var(--code-bg); padding: 1px 5px; border-radius: 3px; font-size: 12px; }}
  .group-heading {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin: 32px 0 12px; }}
  @media (max-width: 720px) {{
    body {{ grid-template-columns: 1fr; }}
    aside {{ position: static; height: auto; }}
  }}
</style>
</head>
<body>
<aside>
  <input id="filter" type="search" placeholder="Filter tools…" autofocus>
  <div class="filter-chips">
    <button data-class="all" class="active">All</button>
    <button data-class="eager">Eager</button>
    <button data-class="deferred">Deferred</button>
  </div>
  <h2>Sections</h2>
  <ul>
    <li><a href="#system-prompt">System prompt</a></li>
    <li><a href="#tools">Tool definitions</a></li>
  </ul>
  <h2>Tools</h2>
  <ul id="tool-nav">
    {nav_items}
  </ul>
</aside>
<main>
  <header class="page">
    <h1>Claude Code reference</h1>
    <p class="meta">
      <span><strong>Surface:</strong> {surface}</span>
      <span><strong>Version:</strong> {version}</span>
      <span><strong>Model:</strong> {model}</span>
      <span><strong>Captured:</strong> {captured_at}</span>
      <span><strong>Tools:</strong> {tool_count}</span>
    </p>
    {summary_line}
  </header>

  <section id="system-prompt">
    <h2>System prompt</h2>
    <p class="meta">{sys_note}</p>
    {system_blocks}
  </section>

  <section id="tools">
    <h2>Tool definitions</h2>
    {tool_articles}
  </section>
</main>
<script>
  function copyJson(name, btn) {{
    const node = document.getElementById('json-' + name);
    if (!node) return;
    navigator.clipboard.writeText(node.textContent.trim()).then(() => {{
      btn.classList.add('copied');
      const t = btn.textContent;
      btn.textContent = 'Copied';
      setTimeout(() => {{ btn.classList.remove('copied'); btn.textContent = t; }}, 1200);
    }});
  }}
  const filter = document.getElementById('filter');
  let activeClass = 'all';

  function applyFilters() {{
    const q = filter.value.toLowerCase();
    document.querySelectorAll('article.tool').forEach(el => {{
      const textHit = el.dataset.search.includes(q);
      const classHit = activeClass === 'all' || el.dataset.classification === activeClass;
      el.style.display = (textHit && classHit) ? '' : 'none';
    }});
    document.querySelectorAll('#tool-nav li[data-tool]').forEach(el => {{
      const a = el.querySelector('a');
      const textHit = a.textContent.toLowerCase().includes(q);
      const classHit = activeClass === 'all' || el.dataset.classification === activeClass;
      el.style.display = (textHit && classHit) ? '' : 'none';
    }});
  }}

  filter.addEventListener('input', applyFilters);
  document.querySelectorAll('.filter-chips button').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.filter-chips button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeClass = btn.dataset.class;
      applyFilters();
    }});
  }});
</script>
</body>
</html>
"""


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_html(tap: dict, *, strip_reminders: bool, classification: dict | None = None, summary: dict | None = None) -> str:
    """Render the same data the markdown exporter sees, as a single
    self-contained HTML page with filter and copy-as-JSON for every tool.
    """
    meta = tap.get("_meta") or {}
    req = tap.get("request") or tap
    model = req.get("model", "unknown")
    tools = [t for t in (req.get("tools") or []) if isinstance(t, dict) and "name" in t]
    sys_blocks = system_blocks(req.get("system"))

    cc_version = "unknown"
    cc_entrypoint = "unknown"
    for b in sys_blocks:
        if (m := re.search(r"cc_version=([\w.\-]+)", b)):
            cc_version = m.group(1)
        if (m := re.search(r"cc_entrypoint=([\w\-]+)", b)):
            cc_entrypoint = m.group(1)
    surface = normalize_surface(cc_entrypoint)

    visible_blocks = [b for b in sys_blocks if not TRANSPORT_BLOCK_RE.match(b.strip())]

    # Map name -> tool dict, and group lookup
    tool_lookup = {t["name"]: t for t in tools}

    def group_of(name: str) -> str:
        return group_for(name)

    def tool_class(name: str) -> tuple[str, str]:
        info = (classification or {}).get(name) or {"label": "unknown", "reason": "no classification signal"}
        return info["label"], info["reason"]

    def badge_html(label: str) -> str:
        labels = {"eager": "Eager", "deferred": "Deferred", "unknown": "?"}
        return f'<span class="badge {label}">{labels.get(label, label)}</span>'

    def nav_li(name: str) -> str:
        label, _ = tool_class(name)
        return (
            f'<li data-tool="{_html_escape(name)}" data-classification="{label}">'
            f'<a href="#tool-{_html_escape(name)}">{_html_escape(name)}{badge_html(label)}</a>'
            f'</li>'
        )

    # Build nav (grouped, in canonical GROUPS order then leftovers)
    nav_lines: list[str] = []
    seen: set[str] = set()
    for group_name, names in GROUPS:
        names_here = [n for n in names if n in tool_lookup]
        if not names_here:
            continue
        nav_lines.append(f'<li class="group">{_html_escape(group_name)}</li>')
        for n in names_here:
            seen.add(n)
            nav_lines.append(nav_li(n))
    leftover = sorted(n for n in tool_lookup if n not in seen)
    if leftover:
        nav_lines.append('<li class="group">Other / MCP</li>')
        for n in leftover:
            nav_lines.append(nav_li(n))

    # Build system prompt blocks (each in its own <details>)
    sys_html_parts: list[str] = []
    for i, block in enumerate(visible_blocks, 1):
        cleaned = strip_session(block, strip_reminders=strip_reminders)
        if not cleaned.strip():
            continue
        sys_html_parts.append(
            f'<details {"open" if i == 1 else ""}>'
            f'<summary>Block {i} ({len(cleaned):,} chars)</summary>'
            f'<pre>{_html_escape(cleaned.rstrip())}</pre>'
            f'</details>'
        )

    # Build tool articles
    article_parts: list[str] = []
    rendered: set[str] = set()

    def render_tool(name: str) -> str:
        tool = tool_lookup[name]
        desc = (tool.get("description") or "").rstrip()
        schema = tool.get("input_schema") or {}
        # Schema table
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        rows = []
        for pname, spec in props.items():
            t = spec.get("type") or spec.get("anyOf") or "?"
            if isinstance(t, list):
                t = " | ".join(map(str, t))
            elif not isinstance(t, str):
                t = "complex"
            pdesc = (spec.get("description") or "").strip()
            if len(pdesc) > 400:
                pdesc = pdesc[:397] + "…"
            rows.append(
                f"<tr><td><code>{_html_escape(pname)}</code></td>"
                f"<td><code>{_html_escape(t)}</code></td>"
                f"<td>{'yes' if pname in required else '—'}</td>"
                f"<td>{_html_escape(pdesc)}</td></tr>"
            )
        table = ""
        if rows:
            table = (
                "<h4>Parameters</h4>"
                "<table><thead><tr><th>Param</th><th>Type</th><th>Required</th><th>Description</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table>"
            )
        else:
            table = "<p><em>(no parameters)</em></p>"
        # Raw JSON blob (full tool definition) for copy-button
        raw = json.dumps(
            {"name": name, "description": desc, "input_schema": schema},
            indent=2,
        )
        # Classification badge + provenance tooltip
        label, why = tool_class(name)
        # Build search index for client-side filter
        search_blob = (name + " " + desc + " " + raw + " " + label).lower()
        return (
            f'<article class="tool" id="tool-{_html_escape(name)}" '
            f'data-search="{_html_escape(search_blob)}" '
            f'data-classification="{label}">'
            f'<h3><span><code>{_html_escape(name)}</code>'
            f'{badge_html(label)}'
            f' <span class="group-tag">{_html_escape(group_of(name))}</span></span>'
            f'<button class="copy" onclick="copyJson(\'{_html_escape(name)}\', this)">Copy JSON</button>'
            f'</h3>'
            f'<p class="summary-line" title="{_html_escape(why)}">{_html_escape(why)}</p>'
            f'<h4>Description</h4>'
            f'<pre>{_html_escape(desc)}</pre>'
            f'{table}'
            f'<script type="application/json" id="json-{_html_escape(name)}">{_html_escape(raw)}</script>'
            f'</article>'
        )

    for group_name, names in GROUPS:
        names_here = [n for n in names if n in tool_lookup]
        if not names_here:
            continue
        article_parts.append(f'<h3 class="group-heading">{_html_escape(group_name)}</h3>')
        for n in names_here:
            rendered.add(n)
            article_parts.append(render_tool(n))
    leftover = sorted(n for n in tool_lookup if n not in rendered)
    if leftover:
        article_parts.append('<h3 class="group-heading">Other / MCP / unmatched</h3>')
        for n in leftover:
            article_parts.append(render_tool(n))

    # Deferred tools without schema this turn — named in the reminder but
    # not yet loaded into tools[]. Show names only.
    if classification:
        no_schema = sorted(
            n for n, info in classification.items()
            if info.get("label") == "deferred" and n not in tool_lookup
        )
        if no_schema:
            chips = " ".join(f'<code>{_html_escape(n)}</code>' for n in no_schema)
            article_parts.append(
                '<div class="deferred-pending-block">'
                f'<h3>Deferred tools without schema this turn — {len(no_schema)} names</h3>'
                '<p style="margin:0 0 10px;color:var(--muted);font-size:13px">'
                'These appeared in the deferred-tools system-reminder but their schemas '
                'are not in this turn\'s <code>tools[]</code>. The model knows they exist; '
                'a <code>ToolSearch</code> call would load the full definition.</p>'
                f'{chips}'
                '</div>'
            )

    sys_note = (
        "Multiple blocks joined in order. Session-specific reminders "
        + ("<strong>stripped</strong>." if strip_reminders else "preserved.")
    )

    # Summary line: counts per classification + interpretation caveat
    if summary:
        c = summary["counts"]
        bits = [f'{badge_html(k)} {c[k]}' for k in ("eager", "deferred") if c.get(k)]
        summary_html = (
            '<p class="summary-line">' + " &nbsp;·&nbsp; ".join(bits) + '</p>'
        )
        if summary.get("interpretation"):
            summary_html += (
                '<div class="interpretation-banner">'
                '<strong>How to read this capture:</strong> '
                + _html_escape(summary["interpretation"])
                + '</div>'
            )
    else:
        summary_html = ''

    return HTML_TEMPLATE.format(
        surface=_html_escape(surface),
        version=_html_escape(cc_version),
        model=_html_escape(model),
        captured_at=_html_escape(meta.get("captured_at", "?")),
        tool_count=len(tools),
        sys_note=sys_note,
        summary_line=summary_html,
        nav_items="\n    ".join(nav_lines),
        system_blocks="\n    ".join(sys_html_parts) if sys_html_parts else "<p><em>(none)</em></p>",
        tool_articles="\n    ".join(article_parts) if article_parts else "<p><em>(none)</em></p>",
    )


def detect_surface(req: dict) -> str | None:
    """Pull cc_entrypoint from a request's system prompt blocks. Returns
    normalized surface name (cli/desktop) or None."""
    for block in system_blocks(req.get("system")):
        m = re.search(r'cc_entrypoint=([\w\-]+)', block)
        if m:
            return normalize_surface(m.group(1))
    return None


# Match `<system-reminder>...</system-reminder>` blocks loosely — the body
# may contain literal `<name>` (from the example `select:<name>` syntax),
# so we can't use `[^<]`. Lazy `.*?` with DOTALL gives the smallest body
# between matching tags, which is what we want.
DEFERRED_REMINDER_RE = re.compile(
    r'<system-reminder>(?P<body>.*?)</system-reminder>',
    re.DOTALL,
)
DEFERRED_PHRASE = "The following deferred tools"


def parse_deferred_reminder(req: dict) -> set[str]:
    """Look for Claude Code's custom system-reminder that lists deferred
    tools by name. Format (confirmed in the CLI binary):

        <system-reminder>
        The following deferred tools are now available via ToolSearch.
        Their schemas are NOT loaded — calling them directly will fail
        with InputValidationError. Use ToolSearch with query
        "select:<name>[,<name>...]" to load tool schemas before calling
        them:
        AskUserQuestion
        CronCreate
        ...
        </system-reminder>

    Returns the set of tool names listed (empty if no such reminder).
    Searches both the system prompt and the messages array (since
    Claude Code injects these reminders into the user message).
    """
    text = ""
    for block in system_blocks(req.get("system")):
        text += block + "\n"
    for m in req.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            text += c + "\n"
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    text += (b.get("text") or "") + "\n"

    names: set[str] = set()
    for match in DEFERRED_REMINDER_RE.finditer(text):
        body = match.group("body")
        if DEFERRED_PHRASE not in body:
            continue
        for line in body.split("\n"):
            s = line.strip()
            # Tool names are CamelCase identifiers or `mcp__<server>__<tool>`
            # where the server segment can include UUIDs with hyphens.
            if re.fullmatch(r'[A-Z][A-Za-z0-9_]*', s) or re.fullmatch(r'mcp__[A-Za-z0-9_\-]+__[A-Za-z0-9_]+', s):
                names.add(s)
    return names


def classify_tools(req: dict) -> dict[str, dict]:
    """Classify every tool in a captured turn as EAGER or DEFERRED — binary.

    Per Anthropic's published Claude Code engineering posts and the API
    docs, deferral works like this on the wire:

      - A deferred tool appears in `tools[]` as a *stub*: just `name` +
        `defer_loading: true`, no `description`, no `input_schema`.
      - An eager tool appears in `tools[]` with the full schema and
        either `defer_loading: false` or no flag at all.
      - Claude Code may *also* inject a `<system-reminder>` listing the
        deferred tool names in a human-readable way.

    So a tool is DEFERRED iff:
      (a) tools[i].defer_loading is true, OR
      (b) it's named in the deferred-tools system-reminder

    Otherwise it's EAGER.

    If the capture shows neither signal anywhere, every tool is
    classified eager — but we record that fact in the summary so the
    reader knows this session didn't use deferral at all (don't
    over-generalize from one tap).
    """
    out: dict[str, dict] = {}
    reminder_names = parse_deferred_reminder(req)

    # Walk tools[] and label each. Stubs and reminder-named both → deferred.
    for tool in req.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name:
            continue
        if tool.get("defer_loading") is True or name in reminder_names:
            out[name] = {
                "label": "deferred",
                "reason": (
                    "tools[].defer_loading=true (stub in request)"
                    if tool.get("defer_loading") is True
                    else "listed in deferred-tools system-reminder"
                ),
            }
        else:
            out[name] = {"label": "eager", "reason": "full schema in tools[], no deferral marker"}

    # Reminder-only tools (named but no tools[] entry — schema not loaded
    # this turn). Still classified as deferred.
    for name in reminder_names:
        if name not in out:
            out[name] = {
                "label": "deferred",
                "reason": "listed in deferred-tools system-reminder; schema not in this turn's tools[]",
            }
    return out


def summarize_classification(req: dict) -> dict:
    """Return summary stats + the per-tool map for a single tap."""
    per_tool = classify_tools(req)
    counts = {"eager": 0, "deferred": 0}
    for info in per_tool.values():
        counts[info["label"]] = counts.get(info["label"], 0) + 1
    has_signal = counts["deferred"] > 0
    return {
        "per_tool": per_tool,
        "counts": counts,
        "has_deferral_signal": has_signal,
        "interpretation": (
            f"This capture uses deferral: {counts['eager']} eager, "
            f"{counts['deferred']} deferred. The eager set is what Claude Code "
            "always loads at session start; deferred tools are fetched via "
            "ToolSearch when the model selects them. This is the authoritative "
            "split for this session, read directly off `tools[].defer_loading` "
            "and the deferred-tools system-reminder."
            if has_signal else
            f"This capture shows NO deferral — all {counts['eager']} tools were "
            "sent eagerly with full schemas. This does NOT mean Claude Code "
            "never defers tools; per Anthropic's [engineering posts]"
            "(https://x.com/trq212/status/2024574133011673516), Claude Code "
            "uses `defer_loading: true` stubs to keep prompt caches stable "
            "across MCP-heavy sessions. But this particular captured turn "
            "didn't trigger it — could be a configuration (e.g. one-shot "
            "`claude --print` mode), a tool-count threshold, the Claude Code "
            "version, or something else. To see deferral in action, capture a "
            "fresh interactive Claude Desktop session through monitor-claude-code."
        ),
    }


def normalize_surface(entrypoint: str | None) -> str:
    """Collapse the various cc_entrypoint values into the two surfaces a
    human thinks in. `claude-code` is the interactive CLI, `sdk-cli` is
    the headless `claude --print` mode — both are "the CLI". Anything
    else passes through as-is so unexpected entrypoints stay visible."""
    if not entrypoint:
        return "unknown"
    if entrypoint == "claude-desktop":
        return "desktop"
    if entrypoint in {"sdk-cli", "claude-code"}:
        return "cli"
    return entrypoint


def extract_metadata(tap: dict) -> tuple[str, str, str]:
    """Return (surface, cc_version, captured_at_utc_compact) from a tap."""
    req = tap.get("request") or tap
    cc_version = "unknown"
    cc_entrypoint = None
    for block in system_blocks(req.get("system")):
        if (m := re.search(r"cc_version=([\w.\-]+)", block)):
            cc_version = m.group(1)
        if (m := re.search(r"cc_entrypoint=([\w\-]+)", block)):
            cc_entrypoint = m.group(1)
    raw = (tap.get("_meta") or {}).get("captured_at") or ""
    try:
        ts = datetime.fromisoformat(raw).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except ValueError:
        ts = re.sub(r"[^A-Za-z0-9]", "", raw) or "unknown"
    return normalize_surface(cc_entrypoint), cc_version, ts


def archive_filename(surface: str, version: str, captured_ts: str, ext: str = "md") -> str:
    return f"cc-reference-{surface}-{version}-{captured_ts}.{ext}"


def replace_symlink(link: Path, target_name: str) -> None:
    """Idempotently point `link` at `target_name` (relative to link's dir)."""
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target_name)


def update_symlinks(out_dir: Path, surface: str, target: Path, ext: str = "md") -> list[str]:
    """Keep the latest-per-surface pointer and the canonical pointer fresh
    for one file extension (md or html). Returns a list of human-readable
    lines describing what was wired up."""
    notes = []
    surface_link = out_dir / f"cc-reference-{surface}.{ext}"
    replace_symlink(surface_link, target.name)
    notes.append(f"{surface_link.name} -> {target.name}")

    # cc-reference.<ext> is always the PRIMARY_SURFACE pointer, regardless
    # of which surface we just captured. If no PRIMARY_SURFACE capture
    # exists yet, fall back to whatever was just written.
    canonical = out_dir / f"cc-reference.{ext}"
    primary_link = out_dir / f"cc-reference-{PRIMARY_SURFACE}.{ext}"
    if primary_link.exists() or primary_link.is_symlink():
        replace_symlink(canonical, primary_link.name)
        notes.append(f"{canonical.name} -> {primary_link.name} (primary)")
    elif not canonical.exists() and not canonical.is_symlink():
        replace_symlink(canonical, surface_link.name)
        notes.append(f"{canonical.name} -> {surface_link.name} (no {PRIMARY_SURFACE} capture yet)")
    return notes


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tap", type=Path, help="Path to a tap JSON (default: latest)")
    p.add_argument(
        "--keep-reminders",
        action="store_true",
        help="Don't strip <system-reminder> blocks from the system prompt",
    )
    p.add_argument(
        "--no-schemas",
        action="store_true",
        help="Skip parameter schema tables (smaller, less useful)",
    )
    p.add_argument(
        "-o", "--output",
        type=Path,
        help="Write to a specific file path (skips auto-archive).",
    )
    p.add_argument(
        "--stdout",
        action="store_true",
        help="Print to stdout instead of writing a file.",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Auto-archive directory (default: {DEFAULT_OUT_DIR}).",
    )
    args = p.parse_args()

    tap_path = args.tap or latest_tap()
    if not tap_path or not tap_path.is_file():
        print(
            f"error: no tap file found (looked in {TAPS_DIR}). "
            "Run a Claude Code session through the monitor-claude-code proxy first.",
            file=sys.stderr,
        )
        return 2

    tap = json.loads(tap_path.read_text())

    # Per-tap classification using the authoritative wire signals:
    #   1. tools[i].defer_loading (official Anthropic API)
    #   2. Claude Code's <system-reminder> listing deferred tools by name
    summary = summarize_classification(tap.get("request") or tap)
    classification = summary["per_tool"]

    md = render(
        tap,
        strip_reminders=not args.keep_reminders,
        include_schemas=not args.no_schemas,
        classification=classification,
        summary=summary,
    )

    # Three output modes:
    # 1) --stdout       → write to stdout, do not touch the archive
    # 2) -o PATH        → write to that exact path, do not touch the archive
    # 3) (default)      → auto-archive into out_dir, update symlinks
    if args.stdout:
        sys.stdout.write(md)
        return 0

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(md)
        print(f"wrote {args.output} ({len(md):,} chars from {tap_path.name})", file=sys.stderr)
        return 0

    surface, version, captured_ts = extract_metadata(tap)
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    md_target = out_dir / archive_filename(surface, version, captured_ts, "md")
    html_target = out_dir / archive_filename(surface, version, captured_ts, "html")

    # Skip the write only if the most recent .md for this surface has
    # the same meaningful content as what we'd produce. We strip the
    # `Captured at: ...` line before comparing — two taps from the same
    # Claude Code version with the same tool set produce identical
    # exports modulo that one line, and piling up near-duplicate files
    # is the exact thing the user complained about.
    def _normalize_for_dedup(text: str) -> str:
        return re.sub(r"^- Captured at:.*\n", "", text, flags=re.MULTILINE)

    current_link = out_dir / f"cc-reference-{surface}.md"
    if current_link.is_symlink():
        try:
            existing = (out_dir / current_link.readlink()).read_text()
            if _normalize_for_dedup(existing) == _normalize_for_dedup(md):
                print(
                    f"no change for surface={surface} version={version}; "
                    f"latest already at {current_link.readlink().name}",
                    file=sys.stderr,
                )
                update_symlinks(out_dir, surface, out_dir / current_link.readlink(), "md")
                # Also refresh the html symlink if a matching .html exists
                html_existing = current_link.readlink().with_suffix(".html")
                if (out_dir / html_existing).exists():
                    update_symlinks(out_dir, surface, out_dir / html_existing, "html")
                return 0
        except (OSError, FileNotFoundError):
            pass

    md_target.write_text(md)
    html = render_html(
        tap,
        strip_reminders=not args.keep_reminders,
        classification=classification,
        summary=summary,
    )
    html_target.write_text(html)

    print(
        f"archived {md_target.name} ({len(md):,} md) + {html_target.name} ({len(html):,} html); "
        f"surface={surface} version={version}",
        file=sys.stderr,
    )
    for note in update_symlinks(out_dir, surface, md_target, "md"):
        print(f"  {note}", file=sys.stderr)
    for note in update_symlinks(out_dir, surface, html_target, "html"):
        print(f"  {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
