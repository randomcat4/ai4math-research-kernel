# AI4Math Research Kernel

An auditable local research kernel for typed mathematical evidence, explicit claim graphs,
composition closure, resumable runs, and thin external-tool adapters.

Status: early implementation of RK-PRD-2. This repository does not claim that the full system,
any adapter, or any mathematical result is verified merely because code exists.

## Public interface

```python
kernel.create(request, capability)
kernel.apply(request, capability)
kernel.inspect(run_id, after_cursor=None, limit=100)
kernel.export(request, capability)
```

The `rkctl` command is a one-JSON-object stdin/stdout wrapper around the same interface.

## Configuration

All deployment-specific values are injected through `KernelConfig`, a configuration mapping,
or registered adapter/verifier profiles. No business rule depends on a model name, provider,
server address, local absolute path, or current research project.

## Development

```text
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m mypy src/rk
```

On Linux, use `.venv/bin/python`.

The normative implementation specifications are in `docs/spec/`; `docs/prd2.md` is preserved
as the parent specification.
