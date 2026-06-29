#!/usr/bin/env python3
"""Minimal local file agent for Qwythos-9B (OpenAI-compatible tool calling).

Turns the local llama-server (serve_local.ps1) into a tiny coding agent that can
list / read / write / mkdir files -- but ONLY inside a single sandbox directory.
Every tool call is path-jailed: anything that resolves outside the sandbox root
(via .., absolute paths, or symlinks) is refused before any I/O happens.

  python file_agent.py                      # REPL, default sandbox
  python file_agent.py --root "C:\\path"     # different sandbox root
  python file_agent.py --once "make a guess-the-number game in game.py"

This is the *agent harness* (the tool-execution loop) that the llama.cpp web UI
lacks -- the browser UI only chats; this wrapper is what lets the model touch
files. The model still does the deciding; the sandbox is what keeps it safe.

NOTE: read/write/list/mkdir only -- no delete, no shell. A 9B local model is far
weaker at agentic tool use than a frontier model; expect to babysit it.
"""
import argparse
import json
import os
import sys

try:
    from openai import OpenAI
except ImportError:
    sys.exit("The 'openai' package is required:  pip install openai")

DEFAULT_ROOT = r"C:\Users\User\Desktop\Road to AU\mini game"
MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 1_000_000
MAX_STEPS = 25          # tool-call iterations per user turn before we bail


class Sandbox:
    """All file I/O is confined to `root`. Paths are resolved and verified to
    stay inside root *before* any operation touches disk."""

    def __init__(self, root):
        self.root = os.path.realpath(root)
        os.makedirs(self.root, exist_ok=True)

    def _resolve(self, rel):
        rel = (rel or ".").strip().replace("\\", "/")
        full = os.path.realpath(os.path.join(self.root, rel))
        r = os.path.normcase(self.root)
        f = os.path.normcase(full)
        if f != r and not f.startswith(r + os.sep):
            raise PermissionError(f"path escapes sandbox: {rel}")
        return full

    def _rel(self, full):
        return os.path.relpath(full, self.root).replace("\\", "/")

    def list_dir(self, path="."):
        full = self._resolve(path)
        if not os.path.isdir(full):
            return {"error": f"not a directory: {path}"}
        entries = []
        for name in sorted(os.listdir(full)):
            p = os.path.join(full, name)
            if os.path.isdir(p):
                entries.append({"name": name, "type": "dir"})
            else:
                entries.append({"name": name, "type": "file", "bytes": os.path.getsize(p)})
        return {"path": self._rel(full) or ".", "entries": entries}

    def read_file(self, path):
        full = self._resolve(path)
        if not os.path.isfile(full):
            return {"error": f"not a file: {path}"}
        if os.path.getsize(full) > MAX_READ_BYTES:
            return {"error": f"file too large (> {MAX_READ_BYTES} bytes)"}
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            return {"path": self._rel(full), "content": fh.read()}

    def write_file(self, path, content):
        if content is None:
            content = ""
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            return {"error": f"content too large (> {MAX_WRITE_BYTES} bytes)"}
        full = self._resolve(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        existed = os.path.isfile(full)
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        return {"path": self._rel(full), "bytes": len(content.encode("utf-8")),
                "action": "overwrote" if existed else "created"}

    def make_dir(self, path):
        full = self._resolve(path)
        os.makedirs(full, exist_ok=True)
        return {"path": self._rel(full), "action": "ensured directory"}


TOOLS = [
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List files and subdirectories inside the sandbox.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "directory relative to sandbox root; '.' for root"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file inside the sandbox.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "file path relative to sandbox root"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a UTF-8 text file inside the sandbox. Parent dirs are created.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "file path relative to sandbox root"},
            "content": {"type": "string", "description": "full file contents"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "make_dir",
        "description": "Create a directory (and parents) inside the sandbox.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "directory path relative to sandbox root"}},
            "required": ["path"]}}},
]

SYSTEM = (
    "You are a coding assistant that builds and edits files in a sandboxed project "
    "directory using the provided tools. You can ONLY touch files inside the sandbox; "
    "any path outside it will be refused. Use relative paths. When the user asks for "
    "code or files, actually create them with write_file rather than just printing code. "
    "Inspect with list_dir/read_file first when editing existing files. After finishing, "
    "briefly summarize in Traditional Chinese what you did."
)


def run_tool(sandbox, name, args):
    try:
        if name == "list_dir":
            return sandbox.list_dir(args.get("path", "."))
        if name == "read_file":
            return sandbox.read_file(args["path"])
        if name == "write_file":
            return sandbox.write_file(args["path"], args.get("content", ""))
        if name == "make_dir":
            return sandbox.make_dir(args["path"])
        return {"error": f"unknown tool {name}"}
    except (PermissionError, KeyError, OSError) as e:
        return {"error": f"{type(e).__name__}: {e}"}


def agent_turn(client, model, messages, sandbox, temperature):
    """Run the model + tool loop until it produces a final text answer."""
    for _ in range(MAX_STEPS):
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS, tool_choice="auto",
            temperature=temperature, max_tokens=2048)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            return msg.content or ""
        # Record the assistant's tool-call turn, then execute each call.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in msg.tool_calls],
        })
        for c in msg.tool_calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = run_tool(sandbox, c.function.name, args)
            shown = {k: v for k, v in args.items() if k != "content"}
            if "content" in args:
                shown["content"] = f"<{len(args['content'])} chars>"
            tag = "ok" if "error" not in result else "REFUSED/err"
            print(f"  · {c.function.name}({json.dumps(shown, ensure_ascii=False)}) -> {tag}"
                  + (f": {result['error']}" if 'error' in result else ""), file=sys.stderr)
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": json.dumps(result, ensure_ascii=False)})
    return "(stopped: hit MAX_STEPS tool-call limit)"


def main():
    ap = argparse.ArgumentParser(description="Sandboxed local file agent for Qwythos-9B.")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--api-key", default="sk-local")
    ap.add_argument("--model", default="qwythos-9b")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="sandbox root directory")
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--once", help="run one instruction non-interactively and exit")
    args = ap.parse_args()

    sandbox = Sandbox(args.root)
    client = OpenAI(base_url=args.base_url, api_key=args.api_key, timeout=600)
    messages = [{"role": "system",
                 "content": SYSTEM + f"\n\nSandbox root (display only): {sandbox.root}"}]

    print(f"Sandbox: {sandbox.root}")
    print(f"Model:   {args.model} @ {args.base_url}")
    print("Tools:   list_dir, read_file, write_file, make_dir  (jailed to sandbox)\n")

    if args.once is not None:
        messages.append({"role": "user", "content": args.once})
        print(agent_turn(client, args.model, messages, sandbox, args.temperature))
        return

    print("Type an instruction (/exit to quit).\n")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not user:
            continue
        if user in ("/exit", "/quit"):
            return
        messages.append({"role": "user", "content": user})
        try:
            print(agent_turn(client, args.model, messages, sandbox, args.temperature))
        except Exception as e:
            print(f"  request failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
