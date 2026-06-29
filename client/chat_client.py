#!/usr/bin/env python3
"""Interactive OpenAI-compatible chat client for Qwythos-9B.

One manual-test interface for BOTH ends of the plan:

  --target local   -> http://127.0.0.1:8080/v1   (llama-server, started by serve_local.ps1)
  --target runpod  -> https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1   (RunPod Serverless)

Because both speak the OpenAI API, the only thing that changes between local testing and
the cloud deployment is base_url + api_key -- exactly the architecture in README sec.3/6.

Examples:
  # Local (after running serve_local.ps1):
  python chat_client.py --target local

  # RunPod (reads RUNPOD_API_KEY + RUNPOD_ENDPOINT_ID from the environment):
  python chat_client.py --target runpod

  # One-shot, non-interactive (smoke test / scripting):
  python chat_client.py --target local --once "Say hello and identify yourself."

  # Just check the endpoint is up and which model it serves:
  python chat_client.py --target local --list-models

In-REPL commands:
  /help                 show commands
  /info                 show current connection + settings
  /system <text>        set the system prompt (resets history)
  /reset                clear conversation (keep system prompt)
  /model <name>         switch served model name
  /stream on|off        toggle streaming
  /temp <float>         set temperature
  /maxtokens <int>      set max_tokens (blank/0 = let the server decide)
  /file <path>          send a file's contents as your next message (long-context test)
  /paste                enter multi-line paste mode; end with a line containing only /end
  /exit  (or /quit)     leave
"""
import argparse
import os
import sys
import time

try:
    from openai import OpenAI
except ImportError:
    sys.exit("The 'openai' package is required:  pip install openai")


def build_client(args):
    """Resolve base_url / api_key / model from target preset, env, and CLI flags."""
    if args.base_url:                       # explicit override wins
        base_url = args.base_url
        api_key = args.api_key or os.environ.get("OPENAI_API_KEY", "sk-noauth")
    elif args.target == "runpod":
        endpoint = args.endpoint_id or os.environ.get("RUNPOD_ENDPOINT_ID")
        if not endpoint:
            sys.exit("RunPod target needs an endpoint id: --endpoint-id or $RUNPOD_ENDPOINT_ID")
        base_url = f"https://api.runpod.ai/v2/{endpoint}/openai/v1"
        api_key = args.api_key or os.environ.get("RUNPOD_API_KEY")
        if not api_key:
            sys.exit("RunPod target needs an API key: --api-key or $RUNPOD_API_KEY")
    else:                                   # local llama-server
        base_url = "http://127.0.0.1:8080/v1"
        api_key = args.api_key or "sk-local"   # llama-server ignores it

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=args.timeout)
    return client, base_url


def list_models(client):
    return [m.id for m in client.models.list().data]


def resolve_model(client, requested):
    """If no model was given, auto-pick the one the server actually serves."""
    if requested:
        return requested
    try:
        ids = list_models(client)
        if ids:
            return ids[0]
    except Exception:
        pass
    return "qwythos-9b"


def send(client, model, messages, stream, temperature, max_tokens):
    """Send one chat turn; print the reply (streaming or not). Returns the reply text."""
    kwargs = dict(model=model, messages=messages, temperature=temperature)
    if max_tokens:
        kwargs["max_tokens"] = max_tokens

    t0 = time.time()
    if stream:
        kwargs["stream"] = True
        try:
            kwargs["stream_options"] = {"include_usage": True}
            resp = client.chat.completions.create(**kwargs)
        except Exception:
            kwargs.pop("stream_options", None)      # some servers reject it
            resp = client.chat.completions.create(**kwargs)
        ttft = None
        parts, usage = [], None
        for chunk in resp:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                if ttft is None:
                    ttft = time.time() - t0
                parts.append(delta)
                print(delta, end="", flush=True)
        print()
        text = "".join(parts)
        _print_timing(t0, ttft, usage)
        return text
    else:
        resp = client.chat.completions.create(**kwargs)
        text = resp.choices[0].message.content or ""
        print(text)
        _print_timing(t0, None, getattr(resp, "usage", None))
        return text


def _print_timing(t0, ttft, usage):
    dt = time.time() - t0
    bits = [f"{dt:.1f}s total"]
    if ttft is not None:
        bits.append(f"{ttft:.1f}s to first token")
    if usage:
        ct = getattr(usage, "completion_tokens", None)
        pt = getattr(usage, "prompt_tokens", None)
        if pt is not None:
            bits.append(f"{pt} prompt tok")
        if ct is not None:
            bits.append(f"{ct} gen tok")
            if dt > 0:
                bits.append(f"{ct/dt:.1f} tok/s")
    print(f"  [{' | '.join(bits)}]", file=sys.stderr)


def read_file_message(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        print(f"  cannot read {path}: {e}", file=sys.stderr)
        return None


def read_paste():
    print("  -- paste mode: end with a line containing only /end --")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "/end":
            break
        lines.append(line)
    return "\n".join(lines)


def repl(client, base_url, state):
    print(f"Connected: {base_url}")
    print(f"Model: {state['model']}   (/help for commands, /exit to quit)\n")
    messages = []
    if state["system"]:
        messages.append({"role": "system", "content": state["system"]})

    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not user:
            continue

        if user.startswith("/"):
            cmd, _, arg = user[1:].partition(" ")
            cmd, arg = cmd.lower(), arg.strip()
            if cmd in ("exit", "quit"):
                return
            elif cmd == "help":
                print(__doc__[__doc__.index("In-REPL commands:"):])
                continue
            elif cmd == "info":
                print(f"  base_url={base_url}\n  model={state['model']}  stream={state['stream']}"
                      f"  temp={state['temperature']}  max_tokens={state['max_tokens'] or 'server'}"
                      f"\n  system={state['system'] or '(none)'}  history_turns={len(messages)}")
                continue
            elif cmd == "system":
                state["system"] = arg
                messages = [{"role": "system", "content": arg}] if arg else []
                print("  system prompt set; history cleared.")
                continue
            elif cmd == "reset":
                messages = [{"role": "system", "content": state["system"]}] if state["system"] else []
                print("  history cleared.")
                continue
            elif cmd == "model":
                if arg:
                    state["model"] = arg
                    print(f"  model -> {arg}")
                continue
            elif cmd == "stream":
                state["stream"] = (arg.lower() != "off")
                print(f"  stream -> {state['stream']}")
                continue
            elif cmd == "temp":
                try:
                    state["temperature"] = float(arg)
                    print(f"  temperature -> {state['temperature']}")
                except ValueError:
                    print("  usage: /temp 0.6")
                continue
            elif cmd == "maxtokens":
                try:
                    state["max_tokens"] = int(arg) if arg else 0
                    print(f"  max_tokens -> {state['max_tokens'] or 'server default'}")
                except ValueError:
                    print("  usage: /maxtokens 512")
                continue
            elif cmd == "file":
                content = read_file_message(arg)
                if content is None:
                    continue
                print(f"  loaded {len(content)} chars from {arg}")
                user = content
            elif cmd == "paste":
                user = read_paste()
                if not user.strip():
                    continue
            else:
                print(f"  unknown command /{cmd} (try /help)")
                continue

        messages.append({"role": "user", "content": user})
        try:
            reply = send(client, state["model"], messages,
                         state["stream"], state["temperature"], state["max_tokens"])
        except KeyboardInterrupt:
            print("\n  (interrupted)")
            messages.pop()
            continue
        except Exception as e:
            print(f"  request failed: {e}", file=sys.stderr)
            messages.pop()      # don't keep a turn that never got answered
            continue
        messages.append({"role": "assistant", "content": reply})


def main():
    ap = argparse.ArgumentParser(description="Interactive OpenAI-compatible chat client for Qwythos-9B.")
    ap.add_argument("--target", choices=["local", "runpod"], default="local")
    ap.add_argument("--base-url", help="explicit base URL (overrides --target)")
    ap.add_argument("--api-key", help="API key (else from env per target)")
    ap.add_argument("--endpoint-id", help="RunPod endpoint id (else $RUNPOD_ENDPOINT_ID)")
    ap.add_argument("--model", help="served model name (default: auto-detect, else qwythos-9b)")
    ap.add_argument("--system", default="", help="system prompt")
    ap.add_argument("--temperature", type=float, default=0.6)
    ap.add_argument("--max-tokens", type=int, default=0, help="0 = let the server decide")
    ap.add_argument("--no-stream", action="store_true")
    ap.add_argument("--timeout", type=float, default=600.0, help="request timeout s (RunPod cold start can be slow)")
    ap.add_argument("--list-models", action="store_true", help="list served models and exit")
    ap.add_argument("--once", help="send one message non-interactively and exit")
    args = ap.parse_args()

    client, base_url = build_client(args)

    if args.list_models:
        try:
            for mid in list_models(client):
                print(mid)
        except Exception as e:
            sys.exit(f"could not list models from {base_url}: {e}")
        return

    model = resolve_model(client, args.model)

    if args.once is not None:
        messages = []
        if args.system:
            messages.append({"role": "system", "content": args.system})
        messages.append({"role": "user", "content": args.once})
        try:
            send(client, model, messages, not args.no_stream, args.temperature, args.max_tokens)
        except Exception as e:
            sys.exit(f"request failed: {e}")
        return

    state = {
        "model": model,
        "system": args.system,
        "stream": not args.no_stream,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
    }
    repl(client, base_url, state)


if __name__ == "__main__":
    main()
