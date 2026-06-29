# Contributing to build123d-mcp

Contributions are welcome — bug fixes, new tools, docs, and benchmark runs all help.

## Licensing (no CLA required)

This project is licensed under **Apache License 2.0**. By submitting a pull
request you agree that your contribution is provided under the same licence —
this is the standard *inbound = outbound* model (Apache 2.0 §5; GitHub Terms of
Service §D.6). **No separate Contributor Licence Agreement (CLA) is required.**

By contributing you confirm that you wrote the contribution or otherwise have
the right to submit it under Apache 2.0.

## Making a change

1. Fork the repo and create a feature branch (don't work on `main`).
2. Make your change, matching the surrounding style. Keep PRs focused — one
   logical change per PR.
3. Add or update tests for any behaviour you change.
4. Run the suite locally: `uv run pytest` (it auto-installs dependencies). The
   target is 100% passing.
5. Open a pull request with a clear description of *what* and *why*.

## CI

Every PR runs the full matrix on GitHub Actions: Linux / macOS / Windows ×
Python 3.12 / 3.13 / 3.14 × build123d 0.10 / 0.11. Fork PRs from first-time
contributors need a maintainer to approve the workflow run before it executes.

## Adding a new tool

See the **"Adding a new tool"** section in [CLAUDE.md](CLAUDE.md) for the exact
four-step wiring (tool function → `WorkerSession` stub → `server.py` registration
→ boundary-coverage classification).
