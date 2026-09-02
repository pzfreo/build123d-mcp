# ADR 0003 — CAD session identity is an out-of-band handle, not the MCP protocol session

- **Status:** Accepted
- **Date:** 2026-08-05
- **Issue:** #428 (SDK v2 migration scope); supersedes the "multi-tenant isolation is
  the embedder's job" position in [ADR 0001](0001-worker-ipc-concurrency.md)

## Context

Two different things are called a "session":

1. **The MCP protocol session.** Streamable HTTP can issue an `Mcp-Session-Id` at
   initialize which the client echoes on later requests. It describes a *connection*.
2. **The CAD session.** The build123d namespace — imports, variables,
   `current_shape`, `objects`, snapshots — living in a `WorkerSession` subprocess.
   It describes a user's *work in progress*.

Until now these were fully decoupled and the CAD session was process-global:
`_resolve_session()` returned a module-level singleton, so every HTTP client shared
one namespace. The `WorkerSession` pipe lock (ADR 0001) made that *safe* — replies
never mispair — but not *isolated*: one client's `reset()` destroyed another's model.

ADR 0001 recorded per-tenant isolation as the embedder's job. That was right when
there was no hosting story; it stopped being right once one was planned.

## Decision

A CAD session is identified by an opaque handle carried in the **`Mcp-Cad-Session`
HTTP header**, resolved by `CadSessionMiddleware` into a per-handle `WorkerSession`
held in a `SessionRegistry`.

**The handle is supplied from outside the MCP client** — by an auth gateway mapping
authenticated identity to a stable handle, or pasted into a client's static server
config. It is never issued to the client by us.

Sessions are **created on first sight of a handle**, capped by `--max-sessions`, and
evicted after `--session-idle-timeout` seconds of inactivity. A request with no
header falls back to the shared singleton, so existing deployments are unaffected.

## Why not the MCP protocol session id

It is the only identity mechanism MCP clients handle automatically, which makes it
tempting. Rejected because:

- **It dies on reconnect.** A new protocol session means a new identity, so a
  transport blip silently discards a half-built model and hands back an empty
  namespace. A gateway-issued handle is stable across reconnects.
- **It couples lifetime to connection lifetime.** CAD sessions are expensive
  (a subprocess with the OCC kernel loaded); protocol sessions are cheap and
  numerous. Binding them gives unbounded subprocess fan-out with no eviction policy.
- **It reintroduces hidden affinity.** Requests would depend on protocol-managed
  session state that a load balancer cannot see, which is precisely what #428 set
  out to avoid.

Using it would also require turning off `stateless_http`, which we keep on
deliberately: session identity is ours, not the transport's.

## Why a header and not a tool argument

A tool argument lives in the JSON-RPC body. **A session-aware load balancer cannot
route on it without parsing MCP.** A header is visible to every intermediary — which
is the same reason the 2026-07-28 protocol puts its own routing metadata
(`MCP-Method`, `MCP-Name`, `MCP-Param-*`) in headers.

It also avoids changing 38 tool signatures, the worker wire, and every docstring,
and keeps session plumbing out of the model's prompt surface.

## Why create-on-first-use rather than an explicit create call

A `create_session` tool returning a handle cannot work: the model can read a token
out of a tool result, but attaching it to subsequent HTTP headers is transport-level
behaviour, and **nothing in MCP lets a server ask a client to adopt a custom
header**. So a gateway-injected or statically configured handle is *always* unknown
to the server on that client's first request; rejecting unknown handles would mean
rejecting every client's first call.

The cost is that a typo'd handle silently allocates a session. That is bounded by
the session cap and the idle TTL rather than by rejection.

## Consequences

- **We do not authenticate handles.** Anyone who can reach the port and guess a
  handle gets that namespace. Handles are `secrets.token_urlsafe` so guessing is
  impractical, but the real control is an auth gateway in front. The CLI says so at
  startup.
- **Handles are process-local.** Running more than one server process requires a
  session-aware load balancer keyed on the header; there is no shared session store,
  deliberately.
- **Eviction is visible to users.** A client returning after its TTL expires gets a
  fresh, empty session under the same handle rather than an error, since the
  alternative — failing the request — is not something a client can act on.
- **Stdio is untouched.** One client, one namespace, no registry, no middleware.
