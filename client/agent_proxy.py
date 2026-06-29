#!/usr/bin/env python3
"""Agent proxy: gives the llama.cpp web UI a file-tool layer without touching the UI.

The llama.cpp browser UI is a precompiled SvelteKit app baked into llama-server, so
we can't edit it. Instead this sits IN FRONT of llama-server:

    browser (open :8081) --> agent_proxy (:8081) --> llama-server (:8080)

- GET / and all static assets are reverse-proxied straight from llama-server, so you
  get the exact same UI -- but served from the proxy's origin.
- Because the UI calls its API with RELATIVE urls (./v1/...), those now hit the proxy.
- POST .../chat/completions is intercepted: the proxy injects the file tools, runs the
  tool-execution loop against llama-server (read/write/list/mkdir, jailed to the sandbox),
  and returns only the final assistant message (prefixed with a note of what it touched).
- Everything else (/v1/models, /props, ...) is passed through unchanged.

So from the browser you just chat, and file operations happen transparently and safely.

  python agent_proxy.py                       # listen :8081, upstream :8080, default sandbox
  python agent_proxy.py --root "C:\\path" --port 8081 --upstream http://127.0.0.1:8080

Then open  http://127.0.0.1:8081  (NOT :8080) in the browser.
"""
import argparse
import json
import sys
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from openai import OpenAI
except ImportError:
    sys.exit("The 'openai' package is required:  pip install openai")

# Reuse the sandbox + tools from the CLI agent so there's one definition of "what the
# model may do" and one path-jail implementation.
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from file_agent import Sandbox, TOOLS, run_tool, SYSTEM, MAX_STEPS, DEFAULT_ROOT

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "content-length"}


def _merge_system(messages):
    """The model's chat template requires exactly one system message, at the start.
    Fold any incoming system messages + our agent instructions into a single leader."""
    sys_parts = [SYSTEM]
    rest = []
    for m in messages:
        if m.get("role") == "system":
            if m.get("content"):
                sys_parts.append(m["content"])
        else:
            rest.append(m)
    return [{"role": "system", "content": "\n\n".join(sys_parts)}] + rest


def run_agent(client, model, messages, sandbox, temperature, max_tokens):
    """Run model + tool loop. Returns (final_text, actions[list of (name,arg,ok)])."""
    msgs = _merge_system(messages)
    actions = []
    for _ in range(MAX_STEPS):
        resp = client.chat.completions.create(
            model=model, messages=msgs, tools=TOOLS, tool_choice="auto",
            temperature=temperature, max_tokens=max_tokens or 2048)
        m = resp.choices[0].message
        if not m.tool_calls:
            return (m.content or ""), actions
        msgs.append({
            "role": "assistant", "content": m.content or "",
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.function.name, "arguments": c.function.arguments}}
                           for c in m.tool_calls]})
        for c in m.tool_calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = run_tool(sandbox, c.function.name, args)
            ok = "error" not in result
            actions.append((c.function.name, args.get("path", ""), ok))
            print(f"  · {c.function.name}({args.get('path','')}) -> {'ok' if ok else 'REFUSED: '+result.get('error','')}",
                  file=sys.stderr)
            msgs.append({"role": "tool", "tool_call_id": c.id,
                         "content": json.dumps(result, ensure_ascii=False)})
    return "(stopped: hit tool-call step limit)", actions


def _action_prefix(actions):
    if not actions:
        return ""
    lines = [f"🔧 {n}(`{p}`) {'✓' if ok else '✗ 拒絕'}" for n, p, ok in actions]
    return "> " + "  \n> ".join(lines) + "\n\n"


class Handler(BaseHTTPRequestHandler):
    # injected by make_handler
    upstream = "http://127.0.0.1:8080"
    client = None
    sandbox = None

    def log_message(self, *a):  # quieter
        pass

    # ---- passthrough reverse proxy for everything we don't intercept ----
    def _passthrough(self, body=None):
        url = self.upstream + self.path
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP
                   and k.lower() != "host"}
        req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=600) as up:
                data = up.read()
                self.send_response(up.status)
                for k, v in up.headers.items():
                    if k.lower() not in HOP_BY_HOP:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json(502, {"error": {"message": f"proxy upstream error: {e}", "code": 502}})

    def _json(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._passthrough()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if self.path.rstrip("/").endswith("chat/completions"):
            self._handle_chat(body)
        else:
            self._passthrough(body)

    # ---- the interesting part: run the agent loop for chat requests ----
    def _handle_chat(self, body):
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return self._json(400, {"error": {"message": f"invalid request body: {e}", "code": 400}})

        model = payload.get("model") or "qwythos-9b"
        messages = payload.get("messages", [])
        temperature = payload.get("temperature", 0.3)
        max_tokens = payload.get("max_tokens") or 0
        stream = bool(payload.get("stream"))

        try:
            text, actions = run_agent(self.client, model, messages, self.sandbox,
                                      temperature, max_tokens)
        except Exception as e:
            return self._json(502, {"error": {"message": f"agent error: {e}", "code": 502}})

        content = _action_prefix(actions) + text
        if stream:
            self._stream_back(model, content)
        else:
            self._json(200, {
                "id": "chatcmpl-proxy", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}]})

    def _stream_back(self, model, content):
        """Emit the final answer as a minimal OpenAI SSE stream so the UI renders it."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def chunk(delta=None, finish=None):
            d = {"id": "chatcmpl-proxy", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(d, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        chunk(delta={"role": "assistant"})
        # send in modest slices so the UI shows progressive text
        step = 24
        for i in range(0, len(content), step):
            chunk(delta={"content": content[i:i + step]})
        chunk(finish="stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def make_handler(upstream, client, sandbox):
    return type("BoundHandler", (Handler,),
                {"upstream": upstream, "client": client, "sandbox": sandbox})


def main():
    ap = argparse.ArgumentParser(description="Agent proxy that adds a file-tool layer to the llama.cpp web UI.")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--upstream", default="http://127.0.0.1:8080", help="llama-server base URL")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="sandbox root directory")
    args = ap.parse_args()

    sandbox = Sandbox(args.root)
    client = OpenAI(base_url=args.upstream.rstrip("/") + "/v1", api_key="sk-local", timeout=600)
    handler = make_handler(args.upstream.rstrip("/"), client, sandbox)

    print(f"Agent proxy  : http://{args.host}:{args.port}   <-- open THIS in the browser")
    print(f"Upstream     : {args.upstream}")
    print(f"Sandbox      : {sandbox.root}")
    print("Tools        : list_dir, read_file, write_file, make_dir  (jailed)\n")
    print("Chat in the browser as usual; file ops run transparently. Ctrl+C to stop.")
    ThreadingHTTPServer((args.host, args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
