# AGENTS.md — Qwythos-9B on RunPod Serverless

Guidance for coding agents (Claude Code, Codex, Devin) working in this repository. This file is the single source of project rules; `CLAUDE.md` only imports it via `@AGENTS.md`.

> 2026-09-28: merged from the former `CLAUDE.md` (the plan-specific guide, kept verbatim below) and the former `AGENTS.md` (generic repository guidelines, folded into "Conventions for future code"). Originals are backed up in `C:\Users\User\agent-config-backup-20260928\projects\Qwythos-9B RunPod Serverless\`.

## What this repository is

This is a **planning / specification repository**, not a software project. It currently contains a single document, `README.md`, written in Traditional Chinese, that specifies how to deploy the model `empero-ai/Qwythos-9B-Claude-Mythos-5-1M` ("Qwythos-9B") on **RunPod Serverless** and call it from a local machine via an OpenAI-compatible API.

There is no build, lint, or test tooling — there is no source code. Work here means editing the plan in `README.md` (or adding deployment artifacts such as request scripts or endpoint config). Keep new prose consistent with the existing Traditional Chinese style unless asked otherwise.

## Core architecture decision

The local machine runs **only the client/agent/application**, never the model. All inference happens on a cloud GPU behind a RunPod Serverless endpoint:

```
Local app/agent → OpenAI-compatible request → RunPod Serverless Endpoint → vLLM worker → Qwythos-9B on cloud GPU
```

- Base URL: `https://api.runpod.ai/v2/{ENDPOINT_ID}/openai/v1`
- Served model name: `qwythos-9b` (overrides the full HF repo name)
- Runtime: **vLLM**, **Queue-based** endpoint (not Load Balancer) — chosen because long-context prefill is slow and the `/run` `/runsync` `/status` `/stream` queue modes handle long tasks better.

## Scale-to-zero on-demand cost model

The whole point of the design is "GPU runs only while in use." This is achieved with endpoint settings `Active workers=0`, `Max workers=1`, `Idle timeout=5–30s`, `FlashBoot=enabled`, cached model enabled. The first request triggers a cold start (container + model load); the worker auto-stops after idle timeout. **Billing covers cold start, execution, and the idle-timeout window** — idle timeout is not free.

## The central technical constraint: KV cache, not model weights

Qwythos-9B is only ~9B params; what makes its claimed ~1M context hard is **KV cache growth**, not weight size. Any decision in this repo about context length, GPU choice, or memory flags should be reasoned about in terms of KV cache. The committed strategy, in strict priority order:

1. Limit `MAX_MODEL_LEN`.
2. Enable `KV_CACHE_DTYPE=fp8` (only under long-context stress testing).
3. Upgrade to a larger-VRAM GPU.
4. CPU swap/offload (`SWAP_SPACE`) only as a last-resort fallback to avoid hard failure — **never** as a daily performance config.

CPU KV offload is explicitly rejected as a primary approach because it moves KV access onto the CPU/RAM/PCIe path and badly hurts long-context latency. (The `llama.cpp` flags `--no-kv-offload`, `-ctk q4_0`, `-ctv q4_0` are mentioned only as a separate local RTX 4060 8GB extreme-test technique, not part of the RunPod path.)

## Context-length ramp — do not skip steps

Never start at 1M context. Raise `MAX_MODEL_LEN` one tier at a time and confirm stability before advancing, or cold starts will OOM during KV-cache init and waste startup cost:

```
65536 → 131072 → 262144 → 512000 → 1010000
```

This maps to the four test phases in the README: 64k POC (A100 80GB) → 128k/262k → 512k (switch to H100/H200, enable fp8) → near-1M (H200/B200, async `/run` only, may end up "experiment only").

## First-version vLLM environment variables

```
MODEL_NAME=empero-ai/Qwythos-9B-Claude-Mythos-5-1M
OPENAI_SERVED_MODEL_NAME_OVERRIDE=qwythos-9b
DTYPE=bfloat16
TRUST_REMOTE_CODE=true
MAX_MODEL_LEN=65536
GPU_MEMORY_UTILIZATION=0.90
```

Add `KV_CACHE_DTYPE=fp8` only for long-context stress tests.

## When updating this plan

- GPU per-second prices in §4 are point-in-time and must be re-verified against the RunPod pricing page before any real deployment — flag them as stale rather than trusting them.
- vLLM support for this model's architecture (Qwen3.5 / newer) is unconfirmed and must be validated against the actual worker image.
- Reference docs (RunPod Serverless, pricing, endpoint config, vLLM OpenAI compatibility/env vars, and the HF model card) are linked in README §12.

## Conventions for future code

- If executable code is added, organize it by purpose: `src/` (worker or client code), `tests/` (automated tests), `scripts/` (setup, smoke tests, deployment helpers), `docs/` (operational notes beyond the README). Document any new required command in `README.md`.
- Use clear module names that describe deployment behavior (e.g. `runpod_client.py`, `smoke_test.py`, `serverless_worker.py`) and explicit uppercase environment variables (`MAX_MODEL_LEN`, `KV_CACHE_DTYPE`, `RUNPOD_API_KEY`).
- Deployment scripts get a lightweight smoke test that checks the endpoint answers through the OpenAI-compatible API with model name `qwythos-9b`. Name tests after the behavior (e.g. `test_openai_compatible_request.py`); run `python -m pytest` once a suite exists.
- Markdown: short sections, fenced code blocks with language tags. Optional check when Node tooling is available: `npx markdownlint-cli2 "**/*.md"`.
- Commits: short imperative messages (e.g. `Add RunPod smoke test`) or conventional prefixes (`docs: update deployment notes`). Change summaries list validation steps, operational impact, affected variables, expected GPU class, context-length target, and whether secrets or endpoint IDs changed.

## Security

Do not commit real RunPod API keys, endpoint IDs tied to private infrastructure, or Hugging Face tokens. Use placeholders like `{ENDPOINT_ID}` and document required variables separately from their values.
