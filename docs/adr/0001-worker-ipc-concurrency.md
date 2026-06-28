# ADR 0001 — WorkerSession IPC is serialised; multi-tenant isolation is the embedder's job

- **Status:** Accepted
- **Date:** 2026-06-28
- **Issue:** #322 (concurrency bug); context from #268/#272 (HTTP transport)

## Context

`server.py` resolves every tool call to a `WorkerSession` (`worker.py`), a
parent-side proxy that talks to a persistent worker subprocess (see
[ADR 0002](0002-worker-subprocess-crash-containment.md)) over a single
`multiprocessing.Pipe`. The session is *stateful by design*: the namespace,
named objects, and snapshots accumulate across calls — that persistence is the
product's core value.

Two facts collide under the HTTP transport (`--transport http`, added in
#268/#272):

1. **One shared session.** With no per-request middleware installed (the
   default), `server._resolve_session()` returns a single module-level
   `WorkerSession` for *all* requests. The `_session_var` contextvar exists as
   an extension hook, but the shipped CLI does not wire per-request sessions.
2. **Concurrent execution.** The streamable-HTTP ASGI app fields requests
   concurrently, and FastMCP runs the sync tool closures off the event loop.
   So two requests can be inside `WorkerSession._call()` at the same time.

`_call()` did `send -> poll -> recv` on the pipe with **no lock**. That pair is
not atomic, so two concurrent callers could interleave and one could `recv()`
the *other's* response — wrong result to the wrong caller, or a desynced IPC
stream. This needs only one client issuing parallel/pipelined calls (or two
tabs / two `curl`s); it is not a multi-tenant-only problem. The `--in-process`
fallback shares one `Session`/OCC kernel across threads and has the same
hazard.

## Decision

Two separate concerns, two separate answers:

1. **IPC concurrency safety — fix now.** Serialise the request/reply critical
   section with a `threading.Lock` held across `send -> poll -> recv -> restart`.
   Subclasses override `_do_call`, not `_call`, so `InProcessSession` inherits
   the same guard over its shared `Session`. A single OCC worker is serial
   anyway, so serialising costs no real throughput.

2. **Multi-tenant data isolation — do *not* build speculatively.** The shared
   session-per-process model is intended for single-user web/embedded
   deployments and is documented as such (README "HTTP transport"; the CLI
   prints a single-shared-session warning). True multi-tenancy is a real
   feature — stateful MCP sessions, a per-session worker subprocess with
   lifecycle/eviction, and per-tenant resource caps — and a per-tenant OCC
   subprocess is memory-heavy. It is left to embedders via the `_session_var`
   hook until a concrete hosting requirement exists.

## Consequences

- Concurrent calls against one `WorkerSession` are now correct (serialised),
  not subtly corruptible. Covered by `tests/test_worker_concurrency.py`.
- Throughput is unchanged in practice (the worker was already serial).
- HTTP mode remains single-CAD-session: concurrent clients still *share* state
  (one namespace). That is by design — the lock makes the sharing safe, it does
  not make sessions independent. Multi-user isolation still requires embedder
  middleware on `_session_var`.
- If multi-tenant CLI hosting becomes a goal, it is a new, larger piece of work
  (session lifecycle + per-session worker pool + eviction), tracked separately.
