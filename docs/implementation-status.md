# Implementation status

This file describes executable code, not mathematical truth and not completion of RK-PRD-2.

## Milestone 1

- The public `ResearchKernel.create/apply/inspect/export` seam is executable.
- SQLite migrations, revision compare-and-set, idempotent create/apply receipts, immutable CAS,
  HMAC capabilities, strict wire validation, evidence ingest, and event pages are connected.
- The pure guard covers all 27 v1 apply commands. Its 34 mutation opcodes have a fail-closed
  dispatcher; an unknown opcode cannot commit an accepted receipt.
- The tested vertical slice creates a draft contract, registers a root claim, freezes and starts
  the run, ingests scoped evidence, interrupts, resumes, and finalizes honestly as `UNRESOLVED`.
- Archon, Rethlas, LeanSearch, and jixia are thin injected adapters. None is executed implicitly.

## Explicit limits

- `AmendContract` returns `TEMPORARILY_UNAVAILABLE`. The frozen companion specification names a
  patch artifact but does not define its physical patch format. The kernel will not invent one.
- Adapter profile validation and recorded fixtures exist, but no production external run is part
  of this milestone.
- Export is byte-deterministic for one persisted revision. Historical event replay for exporting
  an older revision is not implemented; such a request is rejected.
- Raw artifact embedding is not implemented. A dossier request with
  `include_raw_artifacts=true` is rejected rather than silently omitting bytes.
- The jixia adapter validates its envelope and environment match. Its upstream fine-grained result
  schema is not frozen, so this code does not invent field-level mathematical semantics.
- `RegisterRoute` enters `ACTIVE`: the frozen transition permits `SCOUT/ACTIVE`, while the v1 wire
  defines neither a requested status nor a later activation command. Defaulting to `SCOUT` would
  make both expansion and attempts unreachable.

## Migration 0002 rationale

The frozen v1 schema accepted artifact inputs during `create` but had no run-to-artifact relation,
while `RunHandle` intentionally returns no artifact IDs. That made the next public command,
`FreezeContract`, impossible to construct without opening SQLite directly. Migration 0002 adds
`run_artifacts` and exposes only immutable metadata in `inspect`; host paths and bytes remain
hidden. It also prevents one run's guard from treating another run's artifacts as available.
