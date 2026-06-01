#!/usr/bin/env python3
"""monitor-claude-code forwarding proxy + viewer server.

Listens on 127.0.0.1:9999. Forwards every request to https://api.anthropic.com
and saves the body of POST /v1/messages to ../taps/<seq>-<timestamp>.json
before forwarding. Also serves the viewer UI at GET /.

No external dependencies — stdlib only. Runs Python 3.10+.
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import socketserver
import ssl
import sys
import time
from pathlib import Path
from threading import Lock

UPSTREAM_HOST = "api.anthropic.com"
SKILL_ROOT = Path(__file__).resolve().parent.parent
TAPS_DIR = SKILL_ROOT / "taps"
STATIC_DIR = SKILL_ROOT / "static"

# Headers we strip when forwarding (per-hop or content-encoding aware).
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

_seq_lock = Lock()
_seq_counter = 0

# Signature of Claude Desktop's tail-classifier probe — fired after every
# agent pause to decide whether to push-notify the user's phone. Real API
# traffic, but pure noise relative to the actual conversation. Detected by
# the verbatim opening of its system prompt (string lives in the Desktop
# binary). When matched, we don't persist the tap — we still forward
# upstream so Desktop's notification logic keeps working.
_CLASSIFIER_SIGNATURE = "A user kicked off a Claude Code agent to do a coding task and walked away."

# Cache of {tap_filename: summary_dict} so _list_taps() doesn't reparse
# every JSON file on every poll. Keyed by name; we trust mtime won't
# change after a tap is written (we never rewrite tap files).
_summary_cache: dict[str, dict] = {}
# Cached parsed messages array per tap (needed for _infer_produced).
# Same trust model as _summary_cache.
_messages_cache: dict[str, list] = {}
# Cap on how many of the newest tap files /api/taps returns. Anything
# older is still on disk but hidden from the viewer.
_LIST_CAP = 500


def _is_classifier_probe(req: dict) -> bool:
    sysv = req.get("system")
    if isinstance(sysv, str):
        return _CLASSIFIER_SIGNATURE in sysv[:2000]
    if isinstance(sysv, list):
        for b in sysv[:5]:
            if isinstance(b, dict):
                t = b.get("text") or ""
                if _CLASSIFIER_SIGNATURE in t[:2000]:
                    return True
    return False


def next_seq() -> int:
    global _seq_counter
    with _seq_lock:
        if _seq_counter == 0:
            existing = sorted(TAPS_DIR.glob("*.json"))
            for f in existing:
                try:
                    n = int(f.name.split("-", 1)[0])
                    if n > _seq_counter:
                        _seq_counter = n
                except ValueError:
                    pass
        _seq_counter += 1
        return _seq_counter


class TapHandler(http.server.BaseHTTPRequestHandler):
    server_version = "monitor-claude-code/0.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}\n")

    # ---- routing ----

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/viewer":
            self._serve_static("viewer.html", "text/html; charset=utf-8")
        elif self.path == "/api/taps":
            self._list_taps()
        elif self.path.startswith("/api/taps/"):
            self._get_tap(self.path[len("/api/taps/"):])
        elif self.path == "/api/latest":
            self._latest_tap()
        elif self.path.startswith("/static/"):
            fname = self.path[len("/static/"):].split("?", 1)[0]
            ctype = self._guess_ctype(fname)
            self._serve_static(fname, ctype)
        else:
            self._proxy("GET")

    def do_POST(self) -> None:
        if self.path.startswith("/v1/messages"):
            self._proxy_messages()
        elif self.path == "/api/snapshot":
            self._snapshot()
        else:
            self._proxy("POST")

    def do_DELETE(self) -> None:
        if self.path == "/api/taps":
            self._wipe_taps()
        else:
            self._proxy("DELETE")

    def _wipe_taps(self) -> None:
        """Delete all captured tap files. Called from the UI's clear button."""
        removed = 0
        for f in TAPS_DIR.glob("*.json"):
            try:
                f.unlink()
                removed += 1
            except Exception:
                pass
        sys.stderr.write(f"[wipe] removed {removed} tap(s)\n")
        self._send_json({"ok": True, "removed": removed})

    def do_PUT(self) -> None:
        self._proxy("PUT")

    # ---- proxy logic ----

    def _proxy_messages(self) -> None:
        """Capture and forward POST /v1/messages."""
        body_len = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(body_len) if body_len else b""

        # Persist before forwarding so a slow upstream doesn't block the tap.
        skip_write = False
        try:
            parsed = json.loads(body.decode("utf-8"))
            if _is_classifier_probe(parsed):
                skip_write = True
                sys.stderr.write("[skip] classifier probe (Desktop notification system)\n")
            tap_payload: dict = {
                "_meta": {
                    "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "path": self.path,
                    "method": "POST",
                },
                "request": parsed,
            }
        except Exception as e:
            tap_payload = {
                "_meta": {
                    "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "path": self.path,
                    "method": "POST",
                    "parse_error": str(e),
                },
                "request_raw": body.decode("utf-8", errors="replace"),
            }

        if not skip_write:
            seq = next_seq()
            ts = time.strftime("%Y%m%d-%H%M%S")
            tap_path = TAPS_DIR / f"{seq:04d}-{ts}.json"
            TAPS_DIR.mkdir(parents=True, exist_ok=True)
            tap_path.write_text(json.dumps(tap_payload, indent=2))

        # Forward upstream.
        self._forward("POST", body)

    def _proxy(self, method: str) -> None:
        body_len = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(body_len) if body_len else b""
        self._forward(method, body)

    def _forward(self, method: str, body: bytes) -> None:
        ctx = ssl.create_default_context()
        try:
            conn = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=300, context=ctx)
        except Exception as e:
            self.send_error(502, f"upstream connect failed: {e}")
            return

        upstream_headers: dict[str, str] = {}
        for k, v in self.headers.items():
            if k.lower() in HOP_BY_HOP or k.lower() == "host":
                continue
            upstream_headers[k] = v
        upstream_headers["Host"] = UPSTREAM_HOST

        try:
            conn.request(method, self.path, body=body if body else None, headers=upstream_headers)
            resp = conn.getresponse()
        except Exception as e:
            try:
                self.send_error(502, f"upstream request failed: {e}")
            except Exception:
                pass
            conn.close()
            return

        # Mirror status + headers.
        try:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in HOP_BY_HOP:
                    continue
                self.send_header(k, v)
            self.end_headers()
        except Exception:
            conn.close()
            return

        # Stream the body. SSE arrives in chunks; forward as fast as we get it.
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            sys.stderr.write(f"stream forward error: {e}\n")
        finally:
            conn.close()

    # ---- viewer API ----

    def _list_taps(self) -> None:
        """Return tap metadata with session + turn grouping and inferred outcome.

        For each tap we attach:
          - session_id: parsed from request.metadata.user_id JSON blob
          - turn_number: count of plain-text user messages within the session
          - turn_preview: first ~100 chars of the latest user-typed text
                          (skipping <system-reminder> and <command-message> blocks)
          - n_messages: total count in messages array
          - produced: what the model emitted in this call, inferred by diffing
                      against the NEXT tap's messages array, restricted to taps
                      in the SAME session and SAME turn. null for the latest tap
                      in a turn (no next call) or for cross-session boundaries.

        Returns descending-by-seq (newest first) for the UI.
        """
        # Newest-first directory scan, capped at _LIST_CAP. Anything older
        # stays on disk but is hidden from the viewer — listing 4000+ files
        # was pegging the proxy.
        all_files = sorted(TAPS_DIR.glob("*.json"), reverse=True)[:_LIST_CAP]
        files = list(reversed(all_files))  # ascending for diff pass below

        items = []
        cached_messages: list = []
        for f in files:
            cached = _summary_cache.get(f.name)
            if cached is not None:
                items.append(dict(cached))  # copy — we'll mutate "produced" below
                cached_messages.append(_messages_cache.get(f.name, []))
                continue
            stat = f.stat()
            try:
                with f.open() as fh:
                    data = json.load(fh)
                req = data.get("request", {}) if isinstance(data, dict) else {}
                messages = req.get("messages") or []
                session_id = self._extract_session_id(req)
            except Exception:
                messages = []
                req = {}
                session_id = None

            turn_n, turn_preview = self._compute_turn(messages)
            # Find the FIRST user message in this tap's messages array — that's
            # the session opener (used as the session's auto-name).
            first_user_text = ""
            for msg in messages:
                if not isinstance(msg, dict) or msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    first_user_text = content
                    break
                if isinstance(content, list):
                    has_tool_result = any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content
                    )
                    if has_tool_result:
                        continue  # not a real user turn
                    for b in reversed(content):
                        if isinstance(b, dict) and b.get("type") == "text":
                            t = (b.get("text") or "").strip()
                            if not t.startswith("<system-reminder>") and not t.startswith("<command-message>"):
                                first_user_text = t
                                break
                    if first_user_text:
                        break
            summary = {
                "name": f.name,
                "seq": int(f.name.split("-", 1)[0]) if "-" in f.name else 0,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "session_id": session_id,
                "session_first_message": first_user_text[:120],
                "turn_number": turn_n,
                "turn_preview": turn_preview,
                "n_messages": len(messages),
                "produced": None,  # filled in next pass
            }
            _summary_cache[f.name] = dict(summary)
            _messages_cache[f.name] = messages
            items.append(summary)
            cached_messages.append(messages)

        # Second pass: infer what each tap "produced" by diffing with the next tap.
        # Only diff WITHIN a session — across sessions the messages aren't comparable.
        for i in range(len(items) - 1):
            cur, nxt = items[i], items[i + 1]
            same_session = (
                cur["session_id"] is not None
                and cur["session_id"] == nxt["session_id"]
            )
            if not same_session:
                # Cross-session boundary — the session ended, so the last call
                # of the cur session emitted final text (that's what ended the loop).
                cur["produced"] = {"type": "text_final"}
                continue
            if cur["turn_number"] != nxt["turn_number"]:
                # Same session, new turn → last call of the previous turn was text-only.
                cur["produced"] = {"type": "text_final"}
                continue
            # Same session AND same turn — diff messages to infer outcome.
            cur["produced"] = self._infer_produced(
                cached_messages[i], cached_messages[i + 1]
            )

        items.reverse()  # newest first for UI
        self._send_json(items)

    @staticmethod
    def _extract_session_id(req: dict) -> str | None:
        """Pull session_id out of the metadata.user_id JSON blob."""
        try:
            md = req.get("metadata") or {}
            uid = md.get("user_id")
            if isinstance(uid, str):
                parsed = json.loads(uid)
                sid = parsed.get("session_id")
                if isinstance(sid, str):
                    return sid
        except Exception:
            pass
        return None

    @staticmethod
    def _compute_turn(messages: list) -> tuple[int, str]:
        """Count plain-text user messages; capture preview of the latest one."""
        n = 0
        latest_preview = ""
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            text = ""
            is_text_turn = False
            if isinstance(content, str):
                is_text_turn = True
                text = content
            elif isinstance(content, list):
                has_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if not has_tool_result:
                    is_text_turn = True
                    # Find the last text block that isn't a <system-reminder>.
                    for b in reversed(content):
                        if not isinstance(b, dict) or b.get("type") != "text":
                            continue
                        t = (b.get("text") or "").strip()
                        if not t.startswith("<system-reminder>") and not t.startswith("<command-message>"):
                            text = t
                            break
                    if not text and content:
                        # All blocks are reminders — fall back to first text we can find.
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "text":
                                text = (b.get("text") or "").strip()
                                if text:
                                    break
            if is_text_turn:
                n += 1
                latest_preview = text[:100]
        return n, latest_preview

    @staticmethod
    def _infer_produced(cur_msgs: list, nxt_msgs: list) -> dict | None:
        """Look at messages new in nxt_msgs vs cur_msgs and find what the
        assistant produced. Returns one of:
          { type: "tool_use", tools: [name, ...] }
          { type: "text" }
          { type: "thinking" }
          None  (couldn't infer)
        """
        if len(nxt_msgs) <= len(cur_msgs):
            return None
        new_msgs = nxt_msgs[len(cur_msgs):]
        for msg in new_msgs:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                return {"type": "text"}
            if not isinstance(content, list):
                continue
            tool_uses = []
            has_text = False
            has_thinking = False
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "tool_use":
                    name = b.get("name")
                    if isinstance(name, str):
                        tool_uses.append(name)
                elif t == "text":
                    has_text = True
                elif t == "thinking" or t == "redacted_thinking":
                    has_thinking = True
            if tool_uses:
                return {"type": "tool_use", "tools": tool_uses}
            if has_text:
                return {"type": "text"}
            if has_thinking:
                return {"type": "thinking"}
        return None

    def _get_tap(self, name: str) -> None:
        if "/" in name or ".." in name:
            self.send_error(400, "bad tap name")
            return
        path = TAPS_DIR / name
        if not path.exists():
            self.send_error(404, "tap not found")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _latest_tap(self) -> None:
        files = sorted(TAPS_DIR.glob("*.json"), reverse=True)
        if not files:
            self._send_json(None)
            return
        with files[0].open() as f:
            data = json.load(f)
        self._send_json({"name": files[0].name, "data": data})

    def _snapshot(self) -> None:
        """Run a one-shot `claude -p` through the proxy to capture an envelope.

        This spawns a fresh `claude` subprocess with ANTHROPIC_BASE_URL pointing
        at us. The subprocess fires exactly one POST /v1/messages, which we
        intercept and tap like any other request, then forwards upstream so
        Claude actually responds. Net effect: the viewer goes from empty to
        populated in 3-10 seconds with the user's real Claude Code envelope.

        Costs the user ~$0.001 in tokens for one short turn. Requires `claude`
        on PATH and a logged-in session (which it almost certainly has if it's
        running this proxy).
        """
        import shutil
        import subprocess

        # Read body for optional override (e.g. custom prompt). Default is "ping".
        body_len = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(body_len) if body_len else b""
        prompt = "ping"
        try:
            if body:
                payload = json.loads(body.decode("utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("prompt"), str):
                    prompt = payload["prompt"][:500]  # cap length
        except Exception:
            pass  # ignore malformed; use default

        claude_bin = shutil.which("claude")
        if not claude_bin:
            self._send_json({
                "ok": False,
                "error": "`claude` not found on PATH. The proxy can't auto-launch a snapshot session.",
            })
            return

        env = os.environ.copy()
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{os.environ.get('MONITOR_PROMPT_PORT', '9999')}"

        before = len(list(TAPS_DIR.glob("*.json")))
        sys.stderr.write(f"[snapshot] spawning: claude -p {prompt!r} (BASE_URL={env['ANTHROPIC_BASE_URL']})\n")

        try:
            result = subprocess.run(
                [claude_bin, "-p", prompt],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            self._send_json({
                "ok": False,
                "error": "snapshot timed out after 60s — `claude` may be stuck or unauthenticated.",
            })
            return
        except Exception as e:
            self._send_json({"ok": False, "error": f"snapshot failed: {e}"})
            return

        # Wait briefly for the tap file to land (in case the subprocess returns
        # before our POST handler finishes writing).
        for _ in range(10):
            after = len(list(TAPS_DIR.glob("*.json")))
            if after > before:
                break
            time.sleep(0.1)

        new_taps = sorted(TAPS_DIR.glob("*.json"))[before:]
        self._send_json({
            "ok": True,
            "exit_code": result.returncode,
            "stdout_preview": (result.stdout or "")[:300],
            "stderr_preview": (result.stderr or "")[:300],
            "new_tap_files": [f.name for f in new_taps],
        })

    def _serve_static(self, fname: str, ctype: str) -> None:
        path = STATIC_DIR / fname
        if not path.exists():
            self.send_error(404, f"{fname} not found")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _guess_ctype(fname: str) -> str:
        if fname.endswith(".css"):
            return "text/css"
        if fname.endswith(".js"):
            return "application/javascript"
        if fname.endswith(".html"):
            return "text/html; charset=utf-8"
        if fname.endswith(".json"):
            return "application/json; charset=utf-8"
        return "application/octet-stream"


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    port = int(os.environ.get("MONITOR_PROMPT_PORT", "9999"))
    bind = os.environ.get("MONITOR_PROMPT_BIND", "127.0.0.1")
    TAPS_DIR.mkdir(parents=True, exist_ok=True)

    server = ThreadingServer((bind, port), TapHandler)
    sys.stderr.write(f"monitor-claude-code listening on http://{bind}:{port}\n")
    sys.stderr.write(f"  viewer:  http://{bind}:{port}/\n")
    sys.stderr.write(f"  proxy:   set ANTHROPIC_BASE_URL=http://{bind}:{port} for Claude Code\n")
    sys.stderr.write(f"  taps:    {TAPS_DIR}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nstopping monitor-claude-code\n")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
