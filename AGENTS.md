# Repository Guidelines

## Project Structure & Module Organization

This repository currently contains deployment notes for running Qwythos-9B on RunPod Serverless. The main reference is `README.md`, which documents the intended model, vLLM environment variables, RunPod endpoint behavior, GPU sizing, and rollout phases.

There is no source package, test suite, or assets directory yet. If executable code is added, keep it organized by purpose, for example:

- `src/` for application or worker code.
- `tests/` for automated tests.
- `scripts/` for setup, smoke tests, or deployment helpers.
- `docs/` for longer operational notes beyond the README.

## Build, Test, and Development Commands

No build system is configured in this repository. For documentation-only edits, there is no required build step.

Useful checks when tooling is available:

- `git diff -- README.md AGENTS.md` reviews documentation changes.
- `npx markdownlint-cli2 "**/*.md"` checks Markdown formatting if Node tooling is available.
- `python -m pytest` should be used only after a Python test suite is introduced.

Document any new required command in `README.md` when adding code or automation.

## Coding Style & Naming Conventions

Use Markdown headings in sentence or title case and keep sections short. Prefer fenced code blocks with language tags where applicable, such as `text`, `bash`, or `python`.

For future code, use clear module names that describe deployment behavior, for example `runpod_client.py`, `smoke_test.py`, or `serverless_worker.py`. Keep configuration examples explicit and uppercase environment variables, such as `MAX_MODEL_LEN`, `KV_CACHE_DTYPE`, and `RUNPOD_API_KEY`.

## Testing Guidelines

There are no automated tests yet. When code is added, include focused tests under `tests/` and name them after the behavior being verified, for example `test_openai_compatible_request.py`.

For deployment scripts, include a lightweight smoke test that verifies the configured RunPod endpoint responds through the OpenAI-compatible API using model name `qwythos-9b`.

## Commit & Pull Request Guidelines

This directory has no local Git history, so no existing commit convention can be inferred. Use short, imperative commit messages such as `Add RunPod smoke test` or conventional prefixes such as `docs: update deployment notes`.

Pull requests should include a concise summary, validation steps performed, and any operational impact. For configuration changes, mention affected variables, expected GPU class, context length target, and whether secrets or endpoint IDs were changed.

## Security & Configuration Tips

Do not commit real RunPod API keys, endpoint IDs tied to private infrastructure, or Hugging Face tokens. Use placeholders like `{ENDPOINT_ID}` and document required variables separately from their values.
