# AI4Math Research Kernel

An auditable local research kernel for typed mathematical evidence, explicit claim graphs,
composition closure, resumable runs, and thin external-tool adapters.

Status: executable milestone 1 of RK-PRD-2. The contract/claim/evidence lifecycle is connected
end to end, but the full product is not complete. No adapter or mathematical result is verified
merely because code exists. See `docs/implementation-status.md` for exact limits.

## Model and Lean roles

The components have deliberately different authority and workloads:

- **OpenCode + configured model** is a headless execution shell for a candidate LeanWorker.
  RK runs it with a fresh state directory and a deny-all tool policy. Its text is soft evidence.
- **QED-Nano** is an optional high-throughput generator for natural-language proof candidates.
  It is not a Lean prover and never receives machine-verification authority.
- **DeepSeek-Prover-V2-7B** is an optional specialist that generates Lean 4 proof candidates.
  A generated file counts only after the independent pinned Lean replay accepts it.
- **LeanSearch** supplies premise candidates; **jixia** extracts declarations, symbols, and
  elaboration structure; **Lean replay** is the final machine-verification boundary.

The current audited remote smoke path is:

```text
LeanSearch -> OpenCode/DeepSeek candidate -> jixia structure -> independent Lean replay
```

See `docs/rkmodelreport.md` for measured model and Lean-component results. The report separates
model output, adapter completion, and kernel acceptance.

Reproducible remote benchmark entry points are also kept in the repository:

```text
python scripts/rkmodelbench.py qed MODEL_PATH OUTPUT_DIR
python scripts/rkmodelbench.py deepseek-prover MODEL_PATH OUTPUT_DIR
python scripts/rkleanverifybench.py OUTPUT_DIR MATHLIB_PROJECT LAKE_BINARY
bash scripts/rkleanbench.sh MATHLIB_PROJECT TOOLCHAIN_ROOT JIXIA_BINARY OUTPUT_DIR
```

The model scripts use the upstream single-turn defaults tested in the report. They are
evaluation utilities, not an alternate truth path into the kernel.

## Public interface

```python
kernel.create(request, capability)
kernel.apply(request, capability)
kernel.inspect(run_id, after_cursor=None, limit=100)
kernel.export(request, capability)
```

The `rkctl` command is a one-JSON-object stdin/stdout wrapper around the same interface.
English and Chinese subcommands are equivalent:

```text
rkctl create  --cap-file cap.json < create.json
rkctl 创建    --凭据文件 cap.json < create.json
rkctl apply   --cap-file cap.json < command.json
rkctl 应用    --凭据文件 cap.json < command.json
rkctl inspect --handle RUN_ID --limit 100
rkctl 查看    --句柄 RUN_ID --条数 100
rkctl export  --cap-file cap.json < export.json
rkctl 导出    --凭据文件 cap.json < export.json
```

The wire JSON remains language-neutral and keeps the normative English operation names.
Run `rkctl --help` or `rkctl 查看 --help` for Chinese help text.

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
