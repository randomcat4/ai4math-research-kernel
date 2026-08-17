# AI4Math Research Kernel

An auditable research workspace for turning mathematical questions into typed claims, replayable
evidence, independently reviewed closures, and publication-ready dossiers.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Version](https://img.shields.io/badge/version-0.2.0-3056D3)](pyproject.toml)
[![Development branch](https://img.shields.io/badge/branch-agent%2Fbootstrap--research--kernel-6F42C1)](https://github.com/randomcat4/ai4math-research-kernel/tree/agent/bootstrap-research-kernel)

> **Development status.** The implemented product lives on
> [`agent/bootstrap-research-kernel`](https://github.com/randomcat4/ai4math-research-kernel/tree/agent/bootstrap-research-kernel);
> the default `main` branch is the earlier specification baseline. This README describes the public
> development branch at baseline `b40a069`. The project is an internal prototype under active
> development, not a service that autonomously certifies arbitrary mathematics.

## Why RK exists

Mathematical research agents can produce useful searches, proof sketches, formalization attempts,
counterexamples, and certificates while blurring the distinction between “generated,” “checked,”
and “proved.” RK makes that distinction part of the system design.

Every accepted state change is attached to a typed command, a scoped capability, a revision, and
the artifacts that support it. Model output remains candidate material. A mathematical claim can
advance only through the verification gate assigned to that claim, and a final result additionally
requires a complete dependency closure and independent review.

## What is implemented

- **Research kernel:** immutable requests and receipts, optimistic revisions, append-only events,
  SQLite persistence, migrations, and content-addressed artifacts.
- **Claim authority graph:** atomic claims, dependencies, invalidation, revocation, bridge records,
  verification state, and explicit closure witnesses.
- **Research orchestration:** route planning, role scheduling, budgets, pause/resume, durable jobs,
  work activity, and human guidance.
- **Mathematical tools:** adapters for Lean/Mathlib replay, LeanSearch, jixia, literature services,
  deterministic checking, Z3, SymPy, local proof models, OpenCode, and OpenAI-compatible endpoints.
- **Evidence boundaries:** machine, peer, and hybrid composition modes; attempt-bound receipts;
  capability-scoped writes; and explicit soft-evidence ceilings for non-kernel tools.
- **Product surface:** bilingual `rkctl`, an HTTP application, a React research console, material
  ingestion, review queues, publication views, and operational diagnostics.
- **Deliverables:** auditable reports, closure-review packages, deterministic candidate papers,
  whole-paper review records, and TeX/PDF export.
- **Integration surfaces:** Python and TypeScript SDKs plus Windows, Linux, and desktop packaging
  scaffolds.

See [`docs/implementation-status.md`](docs/implementation-status.md) for the evidence ledger and
known limitations. A component being wired or returning successfully does not grant it mathematical
authority.

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

RK deliberately assigns different trust levels to different components:

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

## Repository map

```text
.
├── src/rk/                    # kernel, storage, orchestration, adapters, HTTP and product services
│   ├── adapters/              # external tools with explicit trust ceilings
│   ├── http/                  # HTTP application and production runtime
│   └── product/               # research-console application services
├── frontend/                  # React 19 + Vite research console
├── sdk/                       # Python and TypeScript product SDKs
├── migrations/                # versioned SQLite migrations
├── tests/                     # kernel, product, API, SDK and integration tests
├── scripts/                   # reproducible diagnostics, benchmarks and remote runs
├── packaging/                 # release and desktop packaging
├── docs/spec/                 # normative kernel contracts
├── docs/product/              # product requirements, authority model and architecture
└── config.example.toml        # local deployment configuration without secrets
```

## Quick start

Requirements:

- Python 3.12 or newer;
- Node.js/npm only if you want to build the web console;
- Lean, Mathlib, jixia, model endpoints, and local proof models are optional deployment integrations.

Clone the implemented branch:

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

Inspect the bilingual command surface:

```bash
rkctl --help
rkctl 准备题目 problem.json
```

The first administrator setup creates a local service workspace and configuration. Secrets and
capability keys belong outside Git; start from [`config.example.toml`](config.example.toml) and use
environment variables or protected local files for provider credentials.

The normal researcher journey is:

```text
准备题目 -> 提交并研究 -> 状态 -> 继续研究/暂停研究 -> 审查 -> 导出报告
```

Advanced commands expose claim submission and verification, Lean replay, bridge registration,
contract amendment, proof closure, paper review, and final export. Run `rkctl <command> --help`
before using a write command.

## Web console

The repository includes a React 19/Vite console backed by the RK HTTP application.

```bash
cd frontend
npm ci
npm run build
```

The console visualizes research status, claims and dependencies, routes, reviews, tool activity,
published workspaces, and operational state. It is an operator/researcher interface over the same
authority model, not a second source of mathematical truth.

## Development checks

```bash
python -m pytest
python -m ruff check .
python -m mypy src/rk

cd frontend
npm ci
npm run build

cd ../sdk/typescript
npm ci
npm run typecheck
```

Some integrations require separately installed tools, pinned model assets, or external services.
Keep those tests distinguishable from deterministic local tests and preserve their receipts rather
than converting an unavailable service into a synthetic pass.

## Documentation

- [`PRODUCT.md`](PRODUCT.md) — product purpose, users, personality, and experience principles.
- [`docs/rkfsm.md`](docs/rkfsm.md) — research state machine in mathematical language.
- [`docs/implementation-status.md`](docs/implementation-status.md) — executable status and gaps.
- [`docs/rkleane2e.json`](docs/rkleane2e.json) — machine-readable Lean integration evidence.
- [`docs/rkcomponents.md`](docs/rkcomponents.md) — component integration and trust audit.
- [`docs/rkmodelreport.md`](docs/rkmodelreport.md) — measured model and Lean-component runs.
- [`docs/spec/`](docs/spec/) — normative API, schema, transition, composition, and adapter contracts.
- [`docs/product/`](docs/product/) — product architecture and authority specifications.

## Current limitations

- RK does not prove a natural-language problem merely because a generated Lean declaration compiles.
- Semantic fidelity and genuinely independent mathematical review still require accountable reviewers.
- External services can be unavailable or version-opaque; their missing attestations remain visible.
- OS-level hostile isolation and read-only proof-library caches are not universally enforced.
- Large-model and specialist-prover paths remain candidate generators unless a separate trusted
  verifier replays their output under the required scope.

## License

No `LICENSE` file is currently published in this repository. Public visibility alone does not grant
permission to redistribute or incorporate the code; contact the repository owner before reuse.
