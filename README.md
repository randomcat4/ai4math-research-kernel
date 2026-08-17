# AI4Math Research Kernel

An auditable research workspace for turning mathematical questions into typed claims, replayable
evidence, independently reviewed closures, and publication-ready dossiers.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://github.com/randomcat4/ai4math-research-kernel/blob/agent/bootstrap-research-kernel/pyproject.toml)
[![Development version](https://img.shields.io/badge/development-v0.2.0-3056D3)](https://github.com/randomcat4/ai4math-research-kernel/tree/agent/bootstrap-research-kernel)
[![Development branch](https://img.shields.io/badge/branch-agent%2Fbootstrap--research--kernel-6F42C1)](https://github.com/randomcat4/ai4math-research-kernel/tree/agent/bootstrap-research-kernel)

> **Where is the implementation?** The default `main` branch preserves the original specification
> baseline. The implemented v0.2 product is published on
> [`agent/bootstrap-research-kernel`](https://github.com/randomcat4/ai4math-research-kernel/tree/agent/bootstrap-research-kernel),
> currently based on `b40a069`. Use that branch to run RK or contribute code.

## Why RK exists

Mathematical research agents can produce useful searches, proof sketches, formalization attempts,
counterexamples, and certificates while blurring the distinction between “generated,” “checked,”
and “proved.” RK makes that distinction part of the system design.

Every accepted state change is attached to a typed command, a scoped capability, a revision, and
the artifacts that support it. Model output remains candidate material. A mathematical claim can
advance only through the verification gate assigned to that claim, and a final result additionally
requires a complete dependency closure and independent review.

## Implemented development product

The public development branch contains:

- a Python 3.12 research kernel with SQLite persistence, migrations, append-only events, optimistic
  revisions, scoped capabilities, and content-addressed artifacts;
- a claim/evidence graph with dependencies, invalidation, revocation, bridge records, verification
  state, and explicit closure witnesses;
- route planning, mathematical roles, scheduling, budgets, pause/resume, durable work, and human
  guidance;
- adapters for Lean/Mathlib replay, LeanSearch, jixia, literature services, deterministic checking,
  Z3, SymPy, local proof models, OpenCode, and OpenAI-compatible endpoints;
- a bilingual `rkctl`, an HTTP application, and a React research console;
- material ingestion, review queues, publication views, reports, closure-review packages, and
  TeX/PDF paper export;
- Python and TypeScript SDKs plus release and desktop packaging scaffolds.

Read the full
[`development-branch README`](https://github.com/randomcat4/ai4math-research-kernel/blob/agent/bootstrap-research-kernel/README.md)
for the architecture, command examples, repository map, development checks, and current limitations.

## Authority model

```text
problem contract
  -> research routes
  -> atomic claim candidates
  -> claim-scoped verification
  -> dependency and composition closure
  -> independent closure review
  -> candidate paper
  -> independent whole-paper review
  -> final export
```

Different tools receive deliberately different authority:

| Component | Role | Maximum authority by itself |
|---|---|---|
| Language or proof model | Generate routes, prose, code, or Lean candidates | Soft candidate only |
| Literature search | Return bibliographic candidates | Discovery evidence; no-hit is not proof |
| LeanSearch | Suggest premises | Search evidence only |
| jixia | Analyze Lean structure and state | Structural feedback only |
| Z3, SymPy, enumeration | Produce scoped computational evidence | Claim-bound checker evidence when configured and replayed |
| Lean/Mathlib replay | Check a named formal declaration in a pinned environment | Machine authority for that declaration, not automatic authority for the original prose problem |
| Human review | Record semantic and mathematical judgment | Review evidence within the reviewer's signed scope |

No adapter success automatically widens from a subclaim to the root theorem. The root conclusion
must match the frozen problem, include every effective dependency, satisfy the selected composition
obligations, and pass the required independent gates.

## Quick start

Requirements: Python 3.12 or newer. Node.js/npm is needed only for the web console; mathematical
tools and model endpoints are optional deployment integrations.

```bash
git clone https://github.com/randomcat4/ai4math-research-kernel.git
cd ai4math-research-kernel
git switch agent/bootstrap-research-kernel
python -m venv .venv
```

Install the kernel and development dependencies:

```bash
# Windows
.venv/Scripts/python -m pip install -e ".[dev,math-tools]"

# macOS or Linux
.venv/bin/python -m pip install -e ".[dev,math-tools]"
```

Then inspect the bilingual command surface:

```bash
rkctl --help
rkctl 准备题目 problem.json
```

Secrets and capability keys belong outside Git. Begin with
[`config.example.toml`](https://github.com/randomcat4/ai4math-research-kernel/blob/agent/bootstrap-research-kernel/config.example.toml)
and use environment variables or protected local files for provider credentials.

## Documentation

- [Implementation status](https://github.com/randomcat4/ai4math-research-kernel/blob/agent/bootstrap-research-kernel/docs/implementation-status.md)
- [Research state machine](https://github.com/randomcat4/ai4math-research-kernel/blob/agent/bootstrap-research-kernel/docs/rkfsm.md)
- [Component and trust audit](https://github.com/randomcat4/ai4math-research-kernel/blob/agent/bootstrap-research-kernel/docs/rkcomponents.md)
- [Product requirements and architecture](https://github.com/randomcat4/ai4math-research-kernel/tree/agent/bootstrap-research-kernel/docs/product)
- [Normative kernel specification](docs/spec/README.md)

## Current limitations

- RK does not prove a natural-language problem merely because a generated Lean declaration compiles.
- Semantic fidelity and genuinely independent mathematical review still require accountable reviewers.
- External services can be unavailable or version-opaque; missing attestations remain visible.
- OS-level hostile isolation and read-only proof-library caches are not universally enforced.
- Large-model and specialist-prover paths remain candidate generators unless a trusted verifier
  replays their output under the required scope.

## License

No `LICENSE` file is currently published in this repository. Public visibility alone does not grant
permission to redistribute or incorporate the code; contact the repository owner before reuse.
