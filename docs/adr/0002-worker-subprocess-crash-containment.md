# ADR 0002 — Geometry runs in a spawned worker subprocess for native-crash containment

- **Status:** Accepted (retrospective — records the #21 design, reinforced by closing #137)
- **Date:** 2026-06-28
- **Issues:** #21 (persistent worker), #137 (opt-in ephemeral subprocess — rejected), #143 (`--in-process` fallback)

## Context

`execute()` runs model/LLM-authored code. The security sandbox has three
layers (AST allowlist, restricted builtins, exec timeout) — but all three guard
only *Python-level* behaviour. A native **OpenCASCADE (OCCT) segfault or hard
abort cannot be caught**: no Python `try/except` or signal handler reliably
recovers from a native crash. Run in-process, such a fault takes down the whole
MCP server — and with it the persistent session (namespace, named objects,
snapshots) that is the product's core value.

OCC also runs its own native threads (TBB), which makes `fork()` unsafe:
forking a process with live OCC/TBB threads can deadlock or corrupt state in the
child.

## Decision

All geometry/OCC work runs in a **persistent worker subprocess**, created with
the `multiprocessing` **spawn** context. The parent process (`WorkerSession` in
`worker.py`) never imports or touches OCC; it proxies each operation to the
worker over a `Pipe`. On either a worker crash or an operation timeout, the
parent **SIGKILLs the worker and starts a fresh one**.

Alternatives considered and rejected:

- **In-process `exec()`** — simplest, but no native-crash containment. The
  whole reason this ADR exists.
- **Fork-per-call** — the original design (replaced in #21). Unsafe with live
  OCC/TBB threads, and pays process-setup cost on every call.
- **Opt-in ephemeral per-call subprocess** (proposed in #137) — an
  `isolated=True` / `try` tool that spins up a fresh process per call.
  Rejected: crash containment should be the **default for every call**, not an
  opt-in path a caller must remember to use. The persistent-worker design
  already contains crashes for *all* operations, so #137 was closed as
  superseded.

## Consequences

- **Native crashes are contained by default.** An OCCT segfault kills the
  worker, not the server; the parent detects the dead worker and restarts it.
- **A SIGKILL destroys all session state.** This is the central tradeoff: every
  worker restart — whether from a crash or a mere op-timeout — loses the
  namespace, named objects, and snapshots. That single fact is the forcing
  function behind a family of follow-ups:
  - per-op timeout budgets so a slow-but-valid op doesn't needlessly nuke the
    session (#214), and clear "state was lost" messaging on every restart path
    (#215);
  - pushing un-interruptible native calls **out of process, hard-bounded below
    the op-timeout**, so a heavy render / mesh-check / defect-scan / design-audit
    can't trip the SIGKILL — render (#308), export mesh gate (#294/#295),
    `locate_gate_defects` (#310), `design_audit` (#330);
  - the general policy that no un-interruptible native call should run unbounded
    in the worker (#307), and the cold-large-part short-timeout bug (#296).
- **Spawn, not fork** — safe with OCC/TBB threads; the per-process startup cost
  (re-importing OCC) is paid once and amortised because the worker is
  persistent, not per-call.
- **`--in-process` fallback** (#143) exists for MCP hosts that block
  subprocess creation (e.g. some sandboxed Windows hosts). It runs OCC in the
  server process and **explicitly trades away** crash containment and operation
  timeouts — a documented degraded mode.
- The shared worker pipe must be accessed serially — see
  [ADR 0001](0001-worker-ipc-concurrency.md), which adds the lock this
  architecture depends on under concurrent (HTTP) callers.
