"""One-object stdin/stdout CLI for the ResearchKernel public interface."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from rk.adapters.base import DuplicateJsonKey, load_json
from rk.capability import FileKeyResolver, HmacCapabilityVerifier
from rk.config import KernelConfig
from rk.domain import (
    ApplyRequest,
    CapabilityError,
    CreateRequest,
    ExportRequest,
    KernelError,
    RequestValidationError,
)
from rk.kernel import ResearchKernel
from rk.runtime import SystemClock
from rk.storage import RunNotFound, StorageConflict
from rk.wire import WireValidator


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rkctl",
        description="ResearchKernel 命令行: 所有请求与响应均为单个 UTF-8 JSON 对象。",
    )
    parser.add_argument("--config", "--配置", dest="config", type=Path, help="内核配置文件")
    commands = parser.add_subparsers(dest="operation", required=True)
    for name, alias, help_text in (
        ("create", "创建", "创建研究运行"),
        ("apply", "应用", "应用一条状态命令"),
        ("export", "导出", "导出审计材料"),
    ):
        item = commands.add_parser(name, aliases=[alias], help=help_text)
        item.set_defaults(operation=name)
        item.add_argument(
            "--cap-file",
            "--凭据文件",
            dest="cap_file",
            type=Path,
            required=True,
            help="能力凭据 JSON 文件",
        )
    inspect = commands.add_parser("inspect", aliases=["查看"], help="查看运行快照或事件")
    inspect.set_defaults(operation="inspect")
    inspect.add_argument("--handle", "--句柄", dest="handle", required=True, help="运行 ID")
    inspect.add_argument(
        "--after-cursor", "--游标后", dest="after_cursor", type=int, help="只返回该游标后的事件"
    )
    inspect.add_argument("--limit", "--条数", dest="limit", type=int, default=100)
    return parser


def _read_object() -> dict[str, Any]:
    raw = sys.stdin.buffer.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise RequestValidationError("stdin must not contain a UTF-8 BOM")
    value = load_json(raw)
    if not isinstance(value, dict):
        raise RequestValidationError("stdin must contain exactly one JSON object")
    return value


def _write(value: dict[str, Any]) -> None:
    sys.stdout.buffer.write(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _problem(code: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": "rk.problem.v1",
        "code": code,
        "message": message,
        "details": [],
    }


def _verifier(config: KernelConfig) -> HmacCapabilityVerifier:
    if config.capability_key_path is None or config.capability_key_id is None:
        raise CapabilityError("CAPABILITY_DENIED")
    return HmacCapabilityVerifier(
        FileKeyResolver(config.capability_key_path, config.capability_key_id),
        SystemClock(),
    )


def _run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config = KernelConfig.load(args.config)
    if args.operation == "inspect":
        kernel = ResearchKernel.from_config(config)
        result = kernel.inspect(args.handle, args.after_cursor, args.limit)
        return result.to_dict(), 0

    value = _read_object()
    validator = WireValidator(config.command_schema_path, config.receipt_schema_path)
    validator.validate_request(value)
    if value.get("operation") != args.operation:
        raise RequestValidationError("CLI subcommand and request operation differ")
    run_id = str(value["run_id"]) if "run_id" in value else None
    action = str(value["command"]["type"]) if args.operation == "apply" else str(args.operation)
    capability = _verifier(config).verify(args.cap_file, action, run_id)
    kernel = ResearchKernel.from_config(config)
    if args.operation == "create":
        return kernel.create(CreateRequest.from_mapping(value), capability).to_dict(), 0
    if args.operation == "apply":
        receipt = kernel.apply(ApplyRequest.from_mapping(value), capability)
        if receipt.accepted:
            return receipt.to_dict(), 0
        exit_code = 4 if receipt.rejection_code == "REVISION_CONFLICT" else 3
        return receipt.to_dict(), exit_code
    return kernel.export(ExportRequest.from_mapping(value), capability).to_dict(), 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        value, exit_code = _run(args)
        _write(value)
        return exit_code
    except (
        RequestValidationError,
        DuplicateJsonKey,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        _write(_problem("INGEST_SCHEMA_INVALID", str(exc)))
        return 2
    except CapabilityError:
        _write(_problem("CAPABILITY_DENIED", "capability was denied"))
        return 5
    except StorageConflict as exc:
        _write(_problem("IDEMPOTENCY_KEY_REUSED", str(exc)))
        return 3
    except RunNotFound:
        _write(_problem("RUN_NOT_FOUND", "run was not found"))
        return 3
    except sqlite3.OperationalError as exc:
        _write(_problem("TEMPORARILY_UNAVAILABLE", str(exc)))
        return 6
    except (KernelError, OSError, ValueError, KeyError) as exc:
        _write(_problem("INTERNAL_ERROR", str(exc)))
        return 7


if __name__ == "__main__":
    raise SystemExit(main())
