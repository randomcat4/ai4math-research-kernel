# Repository collaboration rules

- The only public module interface is `ResearchKernel.create/apply/inspect/export` plus the
  `rkctl` CLI. Keep storage, guards, adapters, and orchestration behind that seam.
- Do not hard-code model names, providers, API keys, hostnames, ports, absolute workspace
  paths, budgets, Lean toolchains, Mathlib commits, adapter commits, or verifier profiles.
  Put varying values in configuration or the capability/profile registry.
- Never read, copy, or search `C:\canglan\`. It is outside this repository's scope.
- Never commit secrets, capability credentials, remote SSH configuration, raw model keys, or
  unrelated mathematical source artifacts.
- Tests must exercise observable behaviour through the public interface unless they are pure
  property tests for `TransitionGuard` or composition canonicalization.
- Agents share the worktree. Do not revert or overwrite other agents' edits; adapt to them and
  keep ownership to the files assigned by the primary agent.
- Use UTF-8, deterministic JSON, UTC RFC 3339 timestamps, UUID strings, and lowercase SHA-256.
- A soft model verdict must never promote the machine axis. Local lemmas must never silently
  close a parent composition obligation.
