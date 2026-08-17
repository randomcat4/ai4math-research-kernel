$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
$ruff = Join-Path $repositoryRoot '.venv\Scripts\ruff.exe'
$typescriptSdk = Join-Path $repositoryRoot 'sdk\typescript'

& $python -m pytest -q
& $ruff check src tests scripts\rkgenerateproduct.py sdk\python
& $python -m mypy src sdk\python scripts\rkgenerateproduct.py
& npm --prefix $typescriptSdk run typecheck
& npm --prefix $typescriptSdk run build
