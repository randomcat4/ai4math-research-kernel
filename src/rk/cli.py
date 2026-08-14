"""Mathematician-first Chinese CLI plus the lossless RK JSON protocol mode."""
# ruff: noqa: RUF001 -- Chinese user-facing prose intentionally uses Chinese punctuation.

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import mimetypes
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Never

from rk.adapters.attestation import IndependentVerifierArtifactAdapter, VerifierIdentity
from rk.adapters.base import AdapterProfile, DuplicateJsonKey, canonical_json_sha256, load_json
from rk.adapters.lean import LeanReplayAdapter
from rk.capability import FileKeyResolver, HmacCapabilityVerifier, sign_credential
from rk.component_runtime import build_component_runtime
from rk.composition import selected_subgraph_digest
from rk.config import KernelConfig
from rk.domain import (
    ApplyRequest,
    ArtifactInput,
    CapabilityError,
    CreateRequest,
    ExportRequest,
    KernelError,
    RequestValidationError,
    RunSnapshot,
    TypedCommand,
    VerifiedCapability,
    frozen_mapping,
)
from rk.host_execution import HostExecutionNotAuthoritative, HostExecutionReceiptService
from rk.kernel import ResearchKernel
from rk.paper import VerifiedPaper
from rk.reporting import (
    event_page_summary,
    markdown_to_mathjax_html,
    merge_workflow_snapshot,
    read_cas_artifact,
    result_summary,
    snapshot_summary,
    workflow_report_appendix,
    workflow_summary,
)
from rk.runtime import SystemClock, Uuid7Generator
from rk.storage import RunNotFound, SQLiteStorage, StorageConflict
from rk.strategy import StrategyRunner
from rk.wire import WireValidator


class ChineseArgumentParser(argparse.ArgumentParser):
    """Argparse with Chinese headings and actionable parse failures."""

    def format_help(self) -> str:
        rendered = (
            super()
            .format_help()
            .replace("usage:", "用法:")
            .replace("positional arguments:", "位置参数:")
            .replace("options:", "选项:")
            .replace("show this help message and exit", "显示帮助并退出")
            .replace("show program's version number and exit", "显示版本并退出")
        )
        visible = (line for line in rendered.splitlines() if "==SUPPRESS==" not in line)
        return "\n".join(visible) + "\n"

    def error(self, message: str) -> Never:
        translations = {
            "the following arguments are required": "缺少必填参数",
            "unrecognized arguments": "无法识别的参数",
            "invalid choice": "不是可选值",
        }
        for source, target in translations.items():
            message = message.replace(source, target)
        raise RequestValidationError(f"命令参数有误：{message}；请运行 rkctl --help 查看示例")


def _parser() -> argparse.ArgumentParser:
    parser = ChineseArgumentParser(
        prog="rkctl",
        description="给数学家的可审计研究命令行。先运行“rkctl 准备题目 我的题目.json”。",
        epilog=(
            "最常用：准备题目 → 提交并研究 → 状态 → 继续研究/暂停研究 → "
            "审查 → 导出报告。括号内英文只为兼容旧脚本。"
        ),
    )
    parser.add_argument("--配置", "--config", dest="config", type=Path, help=_config_help())
    parser.add_argument("--version", action="version", version=f"rkctl {_package_version()}")
    commands = parser.add_subparsers(
        dest="operation", required=True, metavar="命令", parser_class=ChineseArgumentParser
    )

    initialize = commands.add_parser(
        "初始化服务", aliases=["init"], help="管理员首次创建研究服务配置"
    )
    initialize.set_defaults(operation="initialize")
    initialize.add_argument("directory", metavar="服务目录", type=Path)
    initialize.add_argument(
        "--模型", "--model", dest="model", default="deepseek-v4-pro", metavar="模型名"
    )
    initialize.add_argument(
        "--模型接口",
        "--endpoint",
        dest="endpoint",
        default="https://api.deepseek.com/chat/completions",
        metavar="HTTPS地址",
    )
    initialize.add_argument(
        "--密钥变量",
        "--key-env",
        dest="key_env",
        default="DEEPSEEK_API_KEY",
        metavar="环境变量名",
    )

    math_tools = commands.add_parser(
        "配置数学工具", aliases=["configure-math"], help="接入 Mathlib、Lean 与可选 jixia"
    )
    math_tools.set_defaults(operation="configure_math")
    math_tools.add_argument("directory", metavar="服务目录", type=Path)
    math_tools.add_argument("--Mathlib路径", dest="mathlib", required=True, type=Path)
    math_tools.add_argument("--Lean工具链", dest="toolchain", required=True, type=Path)
    math_tools.add_argument("--jixia路径", dest="jixia", type=Path)

    prepare = commands.add_parser("准备题目", aliases=["prepare"], help="生成可填写的中文题目模板")
    prepare.set_defaults(operation="prepare")
    prepare.add_argument("output", metavar="题目文件", type=Path)
    prepare.add_argument("--覆盖", "--force", dest="force", action="store_true")

    submit = commands.add_parser("提交题目", aliases=["submit"], help="保存题目，稍后再开始研究")
    submit.set_defaults(operation="submit")
    submit.add_argument("topic_file", metavar="题目文件", type=Path)

    submit_run = commands.add_parser(
        "提交并研究", aliases=["submit-run"], help="保存题目并立即开始第一轮研究"
    )
    submit_run.set_defaults(operation="submit_run")
    submit_run.add_argument("topic_file", metavar="题目文件", type=Path)

    for name, aliases, operation, help_text in (
        ("开始研究", ["开始", "start"], "start", "开始一个已经保存的题目"),
        ("继续研究", ["继续", "continue"], "continue", "推进下一轮研究"),
        ("暂停研究", ["暂停", "pause"], "pause", "安全暂停研究"),
        ("恢复研究", ["恢复", "resume"], "resume", "恢复已暂停的研究"),
    ):
        command = commands.add_parser(name, aliases=aliases, help=help_text)
        command.set_defaults(operation=operation)
        command.add_argument("run_id", metavar="研究编号")

    review = commands.add_parser("审查", aliases=["review"], help="导入数学家的审查意见")
    review.set_defaults(operation="review")
    review.add_argument("run_id", metavar="研究编号")
    review.add_argument("review_file", metavar="审查文件", type=Path)
    review.add_argument(
        "--结论",
        "--verdict",
        dest="verdict",
        required=True,
        type=_review_verdict,
        metavar="通过/需修订/不通过/无法判断",
    )
    review.add_argument(
        "--类型",
        "--kind",
        dest="review_kind",
        choices=("同行", "语义", "质量", "peer", "semantic", "quality"),
        default="同行",
        help="审查维度；同行/语义审查可进入数学门，质量审查只作质量标签",
    )
    review.add_argument(
        "--盲审",
        "--blind",
        dest="blind_review",
        action="store_true",
        help="确认审查者未接触作者身份或生成上下文",
    )

    hint = commands.add_parser(
        "指导研究", aliases=["指导", "hint"], help="给研究编排器高层策略提示"
    )
    hint.set_defaults(operation="hint")
    hint.add_argument("run_id", metavar="研究编号")
    hint.add_argument("hint", metavar="提示内容")
    hint.add_argument(
        "--类型",
        dest="hint_kind",
        choices=["换表示", "停止路线", "优先引理", "修改策略", "其他"],
        default="其他",
    )

    revoke = commands.add_parser("撤销事实", aliases=["revoke-fact"], help="撤销错误事实及其下游")
    revoke.set_defaults(operation="revoke_fact")
    revoke.add_argument("run_id", metavar="研究编号")
    revoke.add_argument("fact_label", metavar="事实标签")
    revoke.add_argument("--原因", dest="reason", required=True)

    claim = commands.add_parser(
        "提交事实", aliases=["submit-fact"], help="由托管 Worker 提交一个原子数学 Claim"
    )
    claim.set_defaults(operation="submit_fact")
    claim.add_argument("run_id", metavar="研究编号")
    claim.add_argument("claim_file", metavar="事实文件", type=Path)

    verify_fact = commands.add_parser(
        "验证事实", aliases=["verify-fact"], help="由独立 Verifier 接受或拒绝一个原子 Claim"
    )
    verify_fact.set_defaults(operation="verify_fact")
    verify_fact.add_argument("run_id", metavar="研究编号")
    verify_fact.add_argument("fact_label", metavar="事实标签")
    verify_fact.add_argument("review_file", metavar="独立验证签名产物 JSON", type=Path)

    lean_fact = commands.add_parser(
        "Lean验证事实", aliases=["lean-verify-fact"],
        help="把当前 Claim 绑定到固定 Mathlib 环境，真实重放并由宿主晋级",
    )
    lean_fact.set_defaults(operation="lean_verify_fact")
    lean_fact.add_argument("run_id", metavar="研究编号")
    lean_fact.add_argument("fact_label", metavar="事实标签")
    lean_fact.add_argument("source_file", metavar="Lean源文件", type=Path)
    lean_fact.add_argument("--声明", dest="declaration", required=True, metavar="定理名")
    lean_fact.add_argument("--声明类型", dest="declaration_type", required=True, metavar="Lean类型")

    soft_verify = commands.add_parser(
        "软验证事实", aliases=["soft-verify-fact"],
        help="由注册的 Rethlas 等软 verifier 批评当前 Claim；永不直接授予真值",
    )
    soft_verify.set_defaults(operation="soft_verify_fact")
    soft_verify.add_argument("run_id", metavar="研究编号")
    soft_verify.add_argument("fact_label", metavar="事实标签")

    search_fact = commands.add_parser(
        "检索事实", aliases=["search-fact"], help="按当前目标检索已通过统一写门的事实"
    )
    search_fact.set_defaults(operation="search_fact")
    search_fact.add_argument("run_id", metavar="研究编号")
    search_fact.add_argument("query", metavar="检索词")
    search_fact.add_argument("--条数", dest="limit", type=int, default=10)

    bridge = commands.add_parser(
        "登记桥接", aliases=["register-bridge"], help="登记经目标域审查和回译的一等 BridgeSpec"
    )
    bridge.set_defaults(operation="register_bridge")
    bridge.add_argument("run_id", metavar="研究编号")
    bridge.add_argument("source_label", metavar="源 Claim 标签")
    bridge.add_argument("target_label", metavar="目标 Claim 标签")
    bridge.add_argument("spec_file", metavar="桥接规格 JSON", type=Path)
    bridge.add_argument("audit_file", metavar="目标域审查文件", type=Path)
    bridge.add_argument("backtranslation_file", metavar="回译文件", type=Path)

    amend = commands.add_parser(
        "修订合同", aliases=["amend-contract"], help="裁决合同缺陷并建立新的冻结版本"
    )
    amend.set_defaults(operation="amend_contract")
    amend.add_argument("run_id", metavar="研究编号")
    amend.add_argument("affected_label", metavar="受影响 Claim 标签")
    amend.add_argument("topic_file", metavar="修订后题目文件", type=Path)
    amend.add_argument("defect_file", metavar="缺陷证据文件", type=Path)
    amend.add_argument("impact_file", metavar="影响分析文件", type=Path)
    amend.add_argument("approval_file", metavar="合同所有者批准文件", type=Path)

    close = commands.add_parser(
        "闭合证明", aliases=["close-proof"], help="提交组合义务与 ClosureWitness"
    )
    close.set_defaults(operation="close_proof")
    close.add_argument("run_id", metavar="研究编号")
    close.add_argument("parent_label", metavar="最终 Claim 标签")
    close.add_argument("review_file", metavar="独立整体验证签名产物 JSON", type=Path)

    prepare_close = commands.add_parser(
        "准备闭合审查包",
        aliases=["prepare-closure-review"],
        help="冻结待审子图与组合义务，交给独立 Verifier 逐项审查和签名",
    )
    prepare_close.set_defaults(operation="prepare_closure_review")
    prepare_close.add_argument("run_id", metavar="研究编号")
    prepare_close.add_argument("parent_label", metavar="最终 Claim 标签")
    prepare_close.add_argument("--输出", dest="output", type=Path, required=True)
    prepare_close.add_argument("--覆盖", dest="force", action="store_true")

    queue = commands.add_parser(
        "执行事实队列", aliases=["run-fact-queue"], help="逐 Claim 提交并等待验证后推进"
    )
    queue.set_defaults(operation="run_fact_queue")
    queue.add_argument("run_id", metavar="研究编号")
    queue.add_argument("queue_file", metavar="事实队列 JSON", type=Path)

    paper_review = commands.add_parser(
        "复核论文", aliases=["review-paper"], help="登记整篇数学一致性复核"
    )
    paper_review.set_defaults(operation="review_paper")
    paper_review.add_argument("run_id", metavar="研究编号")
    paper_review.add_argument("fact_label", metavar="最终事实标签")
    paper_review.add_argument("review_file", metavar="独立整篇复核签名产物 JSON", type=Path)

    paper = commands.add_parser("导出论文", aliases=["paper"], help="从有效事实闭包生成 TeX 或 PDF")
    paper.set_defaults(operation="paper")
    paper.add_argument("run_id", metavar="研究编号")
    paper.add_argument("fact_label", metavar="最终事实标签")
    paper.add_argument("--格式", dest="paper_format", choices=["tex", "pdf"], default="pdf")
    paper.add_argument("--输出", dest="output", type=Path, required=True)
    paper.add_argument("--覆盖", dest="force", action="store_true")

    candidate_paper = commands.add_parser(
        "生成候选论文", aliases=["candidate-paper"],
        help="在 Finalize 前生成确定性候选 TeX，供独立整篇复核精确绑定",
    )
    candidate_paper.set_defaults(operation="candidate_paper")
    candidate_paper.add_argument("run_id", metavar="研究编号")
    candidate_paper.add_argument("fact_label", metavar="最终 ROOT 标签")
    candidate_paper.add_argument("--输出", dest="output", type=Path, required=True)
    candidate_paper.add_argument("--覆盖", dest="force", action="store_true")

    status = commands.add_parser(
        "状态", aliases=["查看", "status", "inspect"], help="用中文摘要查看研究进度"
    )
    status.set_defaults(operation="inspect")
    status.add_argument("run_id", nargs="?", metavar="研究编号")
    status.add_argument("--handle", "--句柄", dest="handle", help=argparse.SUPPRESS)
    status.add_argument(
        "--after-cursor", "--游标后", dest="after_cursor", type=int, help="查看该游标后的事件"
    )
    status.add_argument("--limit", "--条数", dest="limit", type=int, default=100)
    _audit_flag(status, hidden=True)

    report = commands.add_parser("导出报告", aliases=["report"], help="保存便于阅读的研究报告")
    report.set_defaults(operation="report")
    report.add_argument("run_id", metavar="研究编号")
    report.add_argument(
        "--格式",
        "--format",
        dest="format",
        type=_report_format,
        metavar="网页/文稿",
        default="html",
    )
    report.add_argument("--输出", "--output", dest="output", type=Path, required=True)
    report.add_argument("--覆盖", "--force", dest="force", action="store_true")

    # The exact v0.1 JSON protocol remains available for automation and forensic audit.
    for name, alias, help_text in (
        ("create", "创建", argparse.SUPPRESS),
        ("apply", "应用", argparse.SUPPRESS),
        ("export", "导出", argparse.SUPPRESS),
    ):
        item = commands.add_parser(name, aliases=[alias], help=help_text)
        item.set_defaults(operation=name, audit_json=True)
        _capability_flag(item)
    return parser


def _capability_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cap-file",
        "--凭据文件",
        dest="cap_file",
        type=Path,
        required=True,
        help="由 RK 管理员签发的权限文件；它不是模型密钥",
    )


def _audit_flag(parser: argparse.ArgumentParser, *, hidden: bool = False) -> None:
    parser.add_argument(
        "--audit-json",
        "--审计JSON",
        dest="audit_json",
        action="store_true",
        help=argparse.SUPPRESS if hidden else "输出完整机器可读 JSON，不显示普通中文摘要",
    )


def _config_help() -> str:
    return f"配置文件；默认位置为 {_default_config_path()}，不依赖当前目录"


def _default_config_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / "rk" / "config.toml"
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "rk" / "config.toml"


def _package_version() -> str:
    try:
        return version("ai4math-research-kernel")
    except PackageNotFoundError:
        return "开发工作树"


def _report_format(value: str) -> str:
    aliases = {
        "markdown": "markdown",
        "文稿": "markdown",
        "html": "html",
        "网页": "html",
    }
    try:
        return aliases[value.lower()]
    except KeyError as exc:
        raise argparse.ArgumentTypeError("格式应为：网页或文稿") from exc


def _review_verdict(value: str) -> str:
    aliases = {
        "通过": "ACCEPT",
        "accept": "ACCEPT",
        "需修订": "NEEDS_REVISION",
        "needs_revision": "NEEDS_REVISION",
        "不通过": "REJECT",
        "reject": "REJECT",
        "无法判断": "ABSTAIN",
        "abstain": "ABSTAIN",
    }
    try:
        return aliases[value.lower()]
    except KeyError as exc:
        raise argparse.ArgumentTypeError("审查结论应为：通过、需修订、不通过或无法判断") from exc


def _load_config(path: Path | None) -> KernelConfig:
    explicit = path or (Path(os.environ["RK_CONFIG"]) if os.environ.get("RK_CONFIG") else None)
    default_path = _default_config_path()
    chosen = explicit or (default_path if default_path.is_file() else None)
    if chosen is not None:
        return KernelConfig.load(chosen)
    project_root = Path(__file__).resolve().parents[2]
    spec_root = project_root / "docs" / "spec"
    state_root = default_path.parent / "state"
    return KernelConfig.from_mapping(
        {
            "workspace_root": str(state_root),
            "spec_root": str(spec_root),
            "inbox_roots": [str(default_path.parent / "inbox")],
        },
        base=project_root,
    )


def _read_object(path: Path | None = None) -> dict[str, Any]:
    raw = path.read_bytes() if path is not None else sys.stdin.buffer.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise RequestValidationError("文件含 UTF-8 BOM；请另存为 UTF-8（无 BOM）")
    value = load_json(raw)
    if not isinstance(value, dict):
        raise RequestValidationError("输入必须是一个 JSON 对象")
    return value


def _write_json(value: dict[str, Any]) -> None:
    sys.stdout.buffer.write(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _write_text(value: str) -> None:
    sys.stdout.buffer.write(value.encode("utf-8") + b"\n")


def _problem(code: str, message: str, next_step: str) -> dict[str, Any]:
    return {
        "schema_version": "rk.problem.v1",
        "code": code,
        "message": message,
        "details": [],
        "next_step": next_step,
    }


def _verifier(config: KernelConfig) -> HmacCapabilityVerifier:
    if config.capability_key_path is None or config.capability_key_id is None:
        raise CapabilityError("未配置权限验证；请让管理员在配置文件中设置 capability_key_path")
    return HmacCapabilityVerifier(
        FileKeyResolver(config.capability_key_path, config.capability_key_id), SystemClock()
    )


def _managed_capability_path(config: KernelConfig) -> Path:
    configured = config.product.get("mathematician_capability_file")
    if configured is None:
        return config.workspace_root / "secrets" / "mathematician.cap.json"
    path = Path(str(configured)).expanduser()
    return path.resolve() if path.is_absolute() else (config.workspace_root / path).resolve()


def _managed_capability(config: KernelConfig, action: str, run_id: str | None = None) -> Any:
    path = _managed_capability_path(config)
    if not path.is_file():
        raise CapabilityError(
            "研究服务尚未由管理员启用；缺少托管权限文件。数学家不需要自行创建权限文件"
        )
    return _verifier(config).verify(path, action, run_id)


def _managed_product_capability(config: KernelConfig, run_id: str | None = None) -> Any:
    """Load the managed product identity without inventing a fake read-only command action."""

    path = _managed_capability_path(config)
    if not path.is_file():
        raise CapabilityError(
            "研究服务尚未由管理员启用；缺少托管权限文件。数学家不需要自行创建权限文件"
        )
    raw = _read_object(path)
    allowed = raw.get("allowed_actions")
    if not isinstance(allowed, list) or not allowed:
        raise CapabilityError("托管权限文件没有可用的产品动作")
    return _verifier(config).verify(path, str(allowed[0]), run_id)


def _run_protocol(
    args: argparse.Namespace, config: KernelConfig, value: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    validator = WireValidator(config.command_schema_path, config.receipt_schema_path)
    validator.validate_request(value)
    operation = str(value.get("operation"))
    expected = "create" if args.operation in {"submit", "submit_run"} else args.operation
    if operation != expected:
        raise RequestValidationError("命令与 JSON 中的 operation 不一致")
    run_id = str(value["run_id"]) if "run_id" in value else None
    action = str(value["command"]["type"]) if operation == "apply" else operation
    cap_file = getattr(args, "cap_file", None)
    capability = (
        _verifier(config).verify(cap_file, action, run_id)
        if cap_file is not None
        else _managed_capability(config, action, run_id)
    )
    kernel = ResearchKernel.from_config(config)
    if operation == "create":
        return kernel.create(CreateRequest.from_mapping(value), capability).to_dict(), 0
    if operation == "apply":
        receipt = kernel.apply(ApplyRequest.from_mapping(value), capability)
        code = 0
        if not receipt.accepted:
            code = 4 if receipt.rejection_code == "REVISION_CONFLICT" else 3
        return receipt.to_dict(), code
    return kernel.export(ExportRequest.from_mapping(value), capability).to_dict(), 0


def _run(args: argparse.Namespace) -> tuple[dict[str, Any] | str, int]:
    if args.operation == "initialize":
        return _initialize_service(args), 0
    if args.operation == "configure_math":
        return _configure_math_tools(args), 0
    if args.operation == "prepare":
        _write_template(args.output, args.force)
        return (
            f"已生成题目模板：{args.output.resolve()}\n"
            "请填写“题目”“精确否定”“研究对象”和“量词”；不确定的约定不要猜。\n"
            f'填好后运行：rkctl 提交并研究 "{args.output}"',
            0,
        )
    config = _load_config(args.config)
    if args.operation == "inspect":
        handle = args.run_id or args.handle
        if not handle:
            raise RequestValidationError("请给出研究编号，例如：rkctl 状态 1234-…")
        value = (
            ResearchKernel.from_config(config)
            .inspect(handle, args.after_cursor, args.limit)
            .to_dict()
        )
        if args.audit_json:
            return value, 0
        if "events" in value:
            return event_page_summary(value), 0
        workflow = _workflow_status(config, handle)
        return snapshot_summary(merge_workflow_snapshot(value, workflow)), 0
    if args.operation in {"submit", "submit_run"}:
        value = _topic_request(
            _read_object(args.topic_file), args.topic_file, allowed_roots=config.inbox_roots
        )
        result, code = _run_protocol(args, config, value)
        if args.operation == "submit":
            return result_summary("create", result), code
        run_id = str(result["run_id"])
        try:
            workflow = _orchestrator(config, run_id, "StartRun").start(run_id)
        except (RuntimeError, OSError, ValueError, StorageConflict) as exc:
            return (
                f"题目已保存，研究尚未启动。\n研究编号：{run_id}\n原因：{exc}\n"
                f"修好部署后运行：rkctl 开始 {run_id}",
                6,
            )
        return workflow_summary("submit_run", workflow, run_id=run_id), code
    if args.operation in {"start", "continue", "pause", "resume", "review"}:
        return _run_workflow(args, config)
    if args.operation in {
        "hint",
        "submit_fact",
        "verify_fact",
        "search_fact",
        "register_bridge",
        "amend_contract",
        "prepare_closure_review",
        "close_proof",
        "run_fact_queue",
        "lean_verify_fact",
        "soft_verify_fact",
        "revoke_fact",
        "review_paper",
        "paper",
        "candidate_paper",
    }:
        return _run_fact_product_action(args, config)
    if args.operation == "report":
        return _export_report(args, config)
    value = _read_object()
    return _run_protocol(args, config, value)


def _initialize_service(args: argparse.Namespace) -> str:
    root = args.directory.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise FileExistsError("服务目录不是空目录；请选择新目录，避免覆盖已有研究")
    if not str(args.endpoint).startswith("https://"):
        raise RequestValidationError("模型接口必须使用 HTTPS")
    if not str(args.key_env).isidentifier():
        raise RequestValidationError("密钥变量名不是合法环境变量名")
    root.mkdir(parents=True, exist_ok=True)
    state = root / "state"
    inbox = root / "inbox"
    secret_root = root / "secrets"
    for path in (state, inbox, secret_root):
        path.mkdir(parents=True, exist_ok=True)
    key_path = secret_root / "capability.key"
    cap_path = secret_root / "main.cap.json"
    worker_cap_path = secret_root / "worker.cap.json"
    verifier_cap_path = secret_root / "verifier.cap.json"
    config_path = root / "config.json"
    key = secrets.token_bytes(48)
    key_path.write_bytes(key)
    now = datetime.now(UTC)
    key_id = f"rk-product-{uuid.uuid4()}"
    role_ids = {role: str(uuid.uuid4()) for role in ("main", "worker", "verifier")}

    def credential(role: str, actions: list[str]) -> dict[str, Any]:
        return sign_credential(
            {
                "schema_version": "rk.cap.v1",
                "capability_id": role_ids[role],
                "subject_id": f"rk-product-{role}",
                "issuer": "rk-product-host",
                "key_id": key_id,
                "allowed_actions": actions,
                "run_scope": ["*"],
                "issued_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "expires_at": (now + timedelta(days=3650))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "nonce": secrets.token_urlsafe(24),
            },
            key,
        )

    credentials = {
        cap_path: credential(
            "main",
            [
                "create",
                "export",
                "FreezeContract",
                "StartRun",
                "RegisterRoute",
                "AmendContract",
                "ProposeContractDefect",
                "RegisterBridge",
                "RegisterCompositionObligation",
                "SubmitClosureWitness",
                "PromoteClaim",
                "SubmitEvidence",
                "RevokeFact",
                "RecordResearchHint",
                "RecordComponentUsage",
            ],
        ),
        worker_cap_path: credential(
            "worker", ["SubmitEvidence", "RegisterClaim", "RegisterClaimEdge"]
        ),
        verifier_cap_path: credential(
            "verifier",
            [
                "SubmitEvidence",
                "RecordLeanFeedback",
                "RecordPeerReview",
                "VerifyAtomicClaim",
                "RecordPaperReview",
            ],
        ),
    }
    for path, value in credentials.items():
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name == "posix":
        key_path.chmod(0o600)
        for path in credentials:
            path.chmod(0o600)
    project_root = Path(__file__).resolve().parents[2]
    profile_base = {
        "version": "product-v1",
        "source_commit": "UNATTESTED",
        "timeout_seconds": 180,
        "max_response_bytes": 16 * 1024 * 1024,
        "env_whitelist": [],
    }
    config = {
        "workspace_root": str(state),
        "spec_root": str(project_root / "docs" / "spec"),
        "inbox_roots": [str(inbox)],
        "capability_key_path": str(key_path),
        "capability_key_id": key_id,
        "budget_policy": {
            "budget_controller_capability_ids": [role_ids["main"]],
            "global_budget_limits": {
                "INPUT_TOKEN": 1_000_000_000_000,
                "OUTPUT_TOKEN": 250_000_000_000,
                "WALL_SECOND": 3_600_000_000,
                "API_MICRO_CURRENCY": 100_000_000,
            },
        },
        "adapter_profiles": {
            "research-model": {
                **profile_base,
                "name": "research-model",
                "env_whitelist": [str(args.key_env)],
                "endpoint": str(args.endpoint),
            },
            "research-search": {
                **profile_base,
                "name": "research-search",
                "endpoint": "https://leansearch.net/search",
            },
            "research-literature": {
                **profile_base,
                "name": "research-literature",
                "endpoint": "https://api.crossref.org/works",
            },
        },
        "product": {
            "mathematician_capability_file": str(cap_path),
            "worker_capability_file": str(worker_cap_path),
            "verifier_capability_file": str(verifier_cap_path),
            "main_capability_ids": [role_ids["main"]],
            "candidate_writer_capability_ids": [role_ids["worker"]],
            "verifier_capability_ids": [role_ids["verifier"]],
            "model": str(args.model),
            "model_max_tokens": 4096,
            "research_environment_names": [str(args.key_env)],
            "orchestration_minimum_routes": 2,
            "orchestration_maximum_routes": 4,
            "orchestration_route_revisions": 2,
            "orchestration_composition_revisions": 2,
            "orchestration_tool_cycles": 2,
            "orchestration_budget": {
                "max_work_units": 64,
                "max_input_tokens": 1_000_000,
                "max_output_tokens": 250_000,
                "max_wall_time_ms": 3_600_000,
            },
        },
    }
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return (
        f"研究服务已初始化：{root}\n"
        f"配置文件：{config_path}\n收件箱：{inbox}\n"
        f"启动前请设置环境变量 {args.key_env}，然后运行：\n"
        f'rkctl --配置 "{config_path}" 准备题目 "{root / "题目.json"}"'
    )


def _configure_math_tools(args: argparse.Namespace) -> str:
    root = args.directory.expanduser().resolve(strict=True)
    config_path = root / "config.json"
    if not config_path.is_file():
        raise FileNotFoundError("服务目录尚未初始化；请先运行“初始化服务”")
    mathlib = args.mathlib.expanduser().resolve(strict=True)
    toolchain = args.toolchain.expanduser().resolve(strict=True)
    lean = toolchain / "bin" / "lean"
    lake = toolchain / "bin" / "lake"
    if not lean.is_file() or not lake.is_file():
        raise FileNotFoundError("Lean 工具链中缺少 bin/lean 或 bin/lake")
    commit = subprocess.run(
        ["git", "-C", str(mathlib), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    toolchain_name = (mathlib / "lean-toolchain").read_text(encoding="utf-8").strip()
    project = root / "lean_project"
    if not project.exists():
        subprocess.run(
            ["git", "-C", str(mathlib), "worktree", "add", "--detach", str(project), commit],
            check=True,
        )
        (project / ".lake").mkdir(exist_ok=True)
        os.symlink(
            mathlib / ".lake" / "packages",
            project / ".lake" / "packages",
            target_is_directory=True,
        )
    search_roots = sorted(
        str(path.resolve()) for path in (mathlib / ".lake").rglob("build/lib/lean") if path.is_dir()
    )
    if not search_roots or not (mathlib / ".lake/build/lib/lean/Mathlib.olean").is_file():
        raise FileNotFoundError("Mathlib 尚未构建；请先取得官方缓存或完成 lake build")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    profiles = config.setdefault("adapter_profiles", {})
    inbox = root / "inbox"
    lean_output = inbox / "lean"
    lean_output.mkdir(parents=True, exist_ok=True)
    profiles["research-lean"] = {
        "name": "research-lean",
        "version": "product-v1",
        "source_commit": commit,
        "timeout_seconds": 300,
        "max_response_bytes": 8 * 1024 * 1024,
        "env_whitelist": ["LEAN_PATH"],
        "argv_prefix": [str(lake), "env", str(lean)],
        "workspace_root": str(project),
        "output_root": str(lean_output),
        "expected_toolchain": toolchain_name,
        "binary_path": str(lean),
        "binary_sha256": hashlib.sha256(lean.read_bytes()).hexdigest(),
        "allowed_axioms": ["propext", "Classical.choice", "Quot.sound"],
    }
    # The component registry consumes the flat research profile above.  The kernel guard
    # consumes this small version registry when a host execution is bound to a Claim.
    profiles["lean-replay"] = {
        "versions": {
            "product-v1": {
                "environment_profile_ids": ["lean-clean-product-v1"],
                "source_commits": [commit],
            }
        }
    }
    verifier_profiles = config.setdefault("verifier_profiles", {})
    verifier_ids = config.get("product", {}).get("verifier_capability_ids", [])
    verifier_profiles["lean-clean-product-v1"] = {
        "toolchain": toolchain_name,
        "mathlib_commit": commit,
        "adapter_name": "lean-replay",
        "binary_sha256": hashlib.sha256(lean.read_bytes()).hexdigest(),
        "verifier_writer_capability_ids": list(verifier_ids),
    }
    manifest_candidates = sorted(
        (Path(__file__).resolve().parents[2] / "docs" / "evidence").glob(
            f"mathlib-{commit[:7]}-closure.json"
        )
    )
    if len(manifest_candidates) > 1:
        raise RequestValidationError("发现多个同 commit 的 Mathlib 依赖闭包清单")
    if manifest_candidates:
        manifest_path = manifest_candidates[0].resolve()
    else:
        manifest_path = (root / "mathlib-closure.json").resolve()
        _write_mathlib_closure_manifest(mathlib, commit, toolchain_name, manifest_path)
    manifest = load_json(manifest_path.read_bytes())
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema_version") != "rk.mathlib_closure_anchor.v1"
        or manifest.get("mathlib_commit") != commit
        or manifest.get("toolchain") != toolchain_name
    ):
        raise RequestValidationError("Mathlib 依赖闭包清单与当前工具链不匹配")
    enabled = ["Lean", "Mathlib"]
    if args.jixia is not None:
        jixia = args.jixia.expanduser().resolve(strict=True)
        if not jixia.is_file():
            raise FileNotFoundError("jixia 路径不是可执行文件")
        jixia_repo = jixia.parents[3]
        jixia_output = inbox / "jixia"
        jixia_output.mkdir(parents=True, exist_ok=True)
        profiles["research-jixia"] = {
            "name": "research-jixia",
            "version": "product-v1",
            "source_commit": subprocess.run(
                ["git", "-C", str(jixia_repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout.strip(),
            "timeout_seconds": 300,
            "max_response_bytes": 64 * 1024 * 1024,
            "env_whitelist": ["LEAN_PATH"],
            "argv_prefix": [str(lake), "env", str(jixia)],
            "preflight_argv_prefix": [str(lake), "env", str(lean)],
            "repo_path": str(jixia_repo),
            "workspace_root": str(project),
            "output_root": str(jixia_output),
            "expected_toolchain": toolchain_name,
            "binary_path": str(jixia),
            "binary_sha256": hashlib.sha256(jixia.read_bytes()).hexdigest(),
        }
        enabled.append("jixia")
    product = config.setdefault("product", {})
    product["research_environment"] = {"LEAN_PATH": os.pathsep.join(search_roots)}
    receipt_key = root / "secrets" / "host-receipt.key"
    receipt_key.parent.mkdir(parents=True, exist_ok=True)
    if not receipt_key.exists():
        receipt_key.write_bytes(secrets.token_bytes(48))
        if os.name == "posix":
            receipt_key.chmod(0o600)
    product["host_receipt_key_path"] = str(receipt_key)
    host_capability_id = product.setdefault("host_execution_capability_id", str(uuid.uuid4()))
    budget = config.setdefault("budget_policy", {})
    controllers = budget.setdefault("budget_controller_capability_ids", [])
    if host_capability_id not in controllers:
        controllers.append(host_capability_id)
    product["lean_dependency_closure"] = {
        "root": str(mathlib / ".lake"),
        "sha256": str(manifest["dependency_closure_sha256"]),
        "manifest_path": str(manifest_path),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return "数学工具已接入：" + "、".join(enabled) + "\n现在可直接提交题目开始研究。"


def _write_mathlib_closure_manifest(
    mathlib: Path, commit: str, toolchain: str, output: Path
) -> None:
    dependency_root = mathlib / ".lake"
    files = sorted(
        (path for path in dependency_root.rglob("*.olean") if path.is_file()),
        key=lambda path: path.relative_to(dependency_root).as_posix(),
    )
    if not files:
        raise FileNotFoundError("Mathlib 依赖闭包中没有可锚定的 olean 文件")
    digest = hashlib.sha256()
    anchored: dict[str, str] = {}
    for path in files:
        relative_text = path.relative_to(dependency_root).as_posix()
        file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        relative = relative_text.encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(file_digest))
        anchored[relative_text] = file_digest
    value = {
        "schema_version": "rk.mathlib_closure_anchor.v1",
        "provenance": "ADMIN_CONFIGURED_CURRENT_MATHLIB_CACHE",
        "mathlib_commit": commit,
        "toolchain": toolchain,
        "dependency_root_relpath": ".lake",
        "olean_count": len(anchored),
        "olean_files": anchored,
        "dependency_closure_sha256": digest.hexdigest(),
    }
    output.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _export_report(
    args: argparse.Namespace, config: KernelConfig
) -> tuple[dict[str, Any] | str, int]:
    if args.output.exists() and not args.force:
        raise FileExistsError("报告文件已存在；如确定覆盖，请加 --覆盖")
    kernel_format = "JSON" if args.format == "json" else "MARKDOWN"
    snapshot = ResearchKernel.from_config(config).inspect(args.run_id).to_dict()
    revision = snapshot.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise RuntimeError("当前研究状态无法用于导出；请联系管理员检查部署")
    value = {
        "schema_version": "rk.command.v1",
        "operation": "export",
        "request_id": str(uuid.uuid4()),
        "run_id": args.run_id,
        "at_revision": revision,
        "dossier_spec": {
            "format": kernel_format,
            "include_raw_artifacts": False,
            "language": "zh-CN",
        },
    }
    protocol_args = argparse.Namespace(operation="export", cap_file=None)
    ref, code = _run_protocol(protocol_args, config, value)
    data = read_cas_artifact(config.cas_root, str(ref["sha256"]), int(ref["byte_count"]))
    workflow = _workflow_status(config, args.run_id)
    appendix = workflow_report_appendix(workflow)
    if appendix:
        data = data + appendix.encode("utf-8")
    if args.format == "html":
        data = markdown_to_mathjax_html(data.decode("utf-8")).encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    return (
        f"报告已保存：{args.output.resolve()}\n"
        f"格式：{args.format}；大小：{len(data)} 字节\n"
        + (
            "HTML 会在浏览器中用 MathJax 排版公式；离线时仍显示完整 TeX 原文。"
            if args.format == "html"
            else "该文件保存的是原始文本，不声称已经排版渲染。"
        ),
        code,
    )


def _run_workflow(
    args: argparse.Namespace, config: KernelConfig
) -> tuple[dict[str, Any] | str, int]:
    action_by_operation = {
        "start": "StartRun",
        "continue": "SubmitEvidence",
        "pause": "StartRun",
        "resume": "StartRun",
        "review": "SubmitEvidence",
    }
    orchestrator = _orchestrator(config, args.run_id, action_by_operation[args.operation])
    if args.operation == "start":
        result = orchestrator.start(args.run_id)
    elif args.operation == "continue":
        result = orchestrator.continue_run(args.run_id)
    elif args.operation == "pause":
        result = orchestrator.pause(args.run_id)
    elif args.operation == "resume":
        result = orchestrator.resume(args.run_id)
    else:
        review_file = _review_file(args.review_file, config.inbox_roots)
        review_kind = getattr(args, "review_kind", "同行")
        blind_review = bool(getattr(args, "blind_review", False))
        if review_kind in {"同行", "peer"} and not blind_review:
            result = orchestrator.review(args.run_id, review_file, args.verdict)
        else:
            result = orchestrator.review(
                args.run_id,
                review_file,
                args.verdict,
                review_kind=review_kind,
                blind_review=blind_review,
            )
    return workflow_summary(args.operation, result, run_id=args.run_id), 0


def _managed_role_capability(config: KernelConfig, role: str, action: str, run_id: str) -> Any:
    key = {
        "main": "mathematician_capability_file",
        "worker": "worker_capability_file",
        "verifier": "verifier_capability_file",
    }[role]
    configured = config.product.get(key)
    if not isinstance(configured, str) or not configured:
        raise CapabilityError(f"管理员尚未配置 {role} 托管身份")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = config.workspace_root / path
    return _verifier(config).verify(path.resolve(), action, run_id)


def _fact_id_by_label(kernel: ResearchKernel, run_id: str, label: str) -> str:
    snapshot = _run_snapshot(kernel, run_id)
    projection = snapshot.projection
    summary = _run_snapshot(kernel, run_id, fact_query={"operation": "summary"}).projection[
        "fact_graph"
    ]
    effective = set(summary["fact_ids"])
    matches = [
        str(item["claim_id"])
        for item in projection.get("claims", ())
        if isinstance(item, Mapping)
        and item.get("stable_label") == label
        and item.get("claim_id") in effective
    ]
    if len(matches) != 1:
        raise RequestValidationError("没有找到唯一、有效且已验证的事实标签")
    return matches[0]


def _apply_product_command(
    kernel: ResearchKernel,
    run_id: str,
    command_type: str,
    payload: Mapping[str, Any],
    cap: Any,
    artifact_inputs: tuple[ArtifactInput, ...] = (),
) -> Any:
    snapshot = _run_snapshot(kernel, run_id)
    receipt = kernel.apply(
        ApplyRequest(
            str(uuid.uuid4()),
            run_id,
            snapshot.revision,
            TypedCommand(command_type, frozen_mapping(payload)),
            artifact_inputs,
        ),
        cap,
    )
    if not receipt.accepted:
        conditions = ", ".join(f"{item.code}@{item.path}" for item in receipt.missing_conditions)
        suffix = f"（{conditions}）" if conditions else ""
        raise RequestValidationError(f"{command_type} 未完成：{receipt.rejection_code}{suffix}")
    return receipt


def _claim_by_label(kernel: ResearchKernel, run_id: str, label: str) -> Mapping[str, Any]:
    matches = [
        item
        for item in _run_snapshot(kernel, run_id).projection.get("claims", ())
        if isinstance(item, Mapping)
        and item.get("stable_label") == label
        and item.get("lifecycle") == "ACTIVE"
    ]
    if len(matches) != 1:
        raise RequestValidationError("没有找到唯一且仍有效的 Claim 标签")
    return matches[0]


def _product_inbox_file(path: Path, roots: tuple[Path, ...]) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise RequestValidationError("材料必须是普通文件")
    if not any(resolved.is_relative_to(root.resolve()) for root in roots):
        raise RequestValidationError("材料须放入管理员配置的收件箱目录")
    if resolved.stat().st_size > 16 * 1024 * 1024:
        raise RequestValidationError("材料超过 16 MiB，请拆分后再导入")
    return resolved


def _import_product_file(
    kernel: ResearchKernel,
    config: KernelConfig,
    run_id: str,
    path: Path,
    cap: Any,
    *,
    logical_name: str,
    role: str,
    media_type: str | None = None,
) -> Any:
    resolved = _product_inbox_file(path, config.inbox_roots)
    data = resolved.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return kernel.import_artifact(
        run_id,
        ArtifactInput(
            "product_artifact" + resolved.suffix.lower(),
            str(resolved),
            digest,
            len(data),
            media_type or mimetypes.guess_type(resolved.name)[0] or "text/plain",
        ),
        cap,
        logical_name=f"{logical_name}-{digest[:12]}",
        role=role,
    )


def _independent_verifier_adapter(config: KernelConfig) -> IndependentVerifierArtifactAdapter:
    raw = config.product.get("independent_verifier_identities")
    if not isinstance(raw, Mapping) or not raw:
        raise RequestValidationError("管理员尚未配置独立窄身份 verifier 注册表")
    identities: dict[str, VerifierIdentity] = {}
    for identity_id, value in raw.items():
        if not isinstance(value, Mapping):
            raise RequestValidationError("独立 verifier 身份配置必须是对象")
        key_path = value.get("public_key_path")
        subject_id = value.get("subject_id")
        if not isinstance(key_path, str) or not isinstance(subject_id, str):
            raise RequestValidationError(
                "独立 verifier 身份缺少 subject_id 或 public_key_path"
            )
        resolved = Path(key_path).resolve()
        try:
            public_key = resolved.read_bytes()
        except OSError as error:
            raise RequestValidationError("独立 verifier 验签密钥不可读") from error
        def verify_signature(
            message: bytes, signature: bytes, *, public_key_path: Path = resolved
        ) -> bool:
            with tempfile.TemporaryDirectory(prefix="rk-verifier-signature-") as folder:
                root = Path(folder)
                message_path = root / "message.bin"
                signature_path = root / "signature.bin"
                message_path.write_bytes(message)
                signature_path.write_bytes(signature)
                completed = subprocess.run(
                    [
                        "openssl", "pkeyutl", "-verify", "-pubin", "-inkey",
                        str(public_key_path), "-rawin", "-in", str(message_path),
                        "-sigfile", str(signature_path),
                    ],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                return completed.returncode == 0

        identities[str(identity_id)] = VerifierIdentity(
            str(identity_id),
            subject_id,
            hashlib.sha256(public_key).hexdigest(),
            verify_signature,
            bool(value.get("active", True)),
        )
    return IndependentVerifierArtifactAdapter(identities)


def _verify_signed_review(
    config: KernelConfig,
    path: Path,
    expected_binding: Mapping[str, Any],
) -> tuple[dict[str, Any], Path]:
    resolved = _product_inbox_file(path, config.inbox_roots)
    artifact = _read_object(resolved)
    result = _independent_verifier_adapter(config).run(
        {"artifact": artifact, "expected_binding": dict(expected_binding)}
    )
    if result.get("status") != "COMPLETED":
        raise RequestValidationError(
            f"独立 verifier 产物拒绝：{result.get('reason', 'UNKNOWN')}"
        )
    imported = result.get("import_fields")
    if not isinstance(imported, Mapping):
        raise RequestValidationError("独立 verifier 产物没有可导入字段")
    return dict(result), resolved


def _attesting_verifier_capability(
    config: KernelConfig, attested: Mapping[str, Any], action: str, run_id: str
) -> VerifiedCapability:
    registry = config.product.get("independent_verifier_identities")
    identity_id = str(attested.get("verifier_identity_id", ""))
    value = registry.get(identity_id) if isinstance(registry, Mapping) else None
    capability_file = value.get("capability_file") if isinstance(value, Mapping) else None
    if not isinstance(capability_file, str):
        raise RequestValidationError("独立 verifier 身份未绑定其窄 capability 文件")
    return _verifier(config).verify(Path(capability_file).resolve(), action, run_id)


def _host_product_capability(config: KernelConfig) -> VerifiedCapability:
    identifier = config.product.get("host_execution_capability_id")
    if not isinstance(identifier, str) or not identifier:
        raise CapabilityError("管理员尚未配置宿主执行身份")
    return VerifiedCapability(
        capability_id=identifier,
        subject_id="rk-product-host-execution",
        issuer="rk-product-host",
        allowed_actions=frozenset(
            {
                "HostExecute",
                "RegisterRoute",
                "RegisterAttempt",
                "BindExecution",
                "AcquireLease",
                "ReleaseLease",
                "RecordBudget",
                "SubmitEvidence",
            }
        ),
        run_scope=frozenset({"*"}),
        issued_at="2026-01-01T00:00:00.000Z",
        expires_at="2036-01-01T00:00:00.000Z",
    )


def _lean_verify_fact_product(
    args: argparse.Namespace,
    config: KernelConfig,
    kernel: ResearchKernel,
    snapshot: RunSnapshot,
) -> tuple[str, int]:
    if snapshot.status == "OPEN":
        root_id = snapshot.projection.get("root_claim_id")
        if not isinstance(root_id, str) or not root_id:
            current_contract = snapshot.projection.get("contract")
            current_statement_hash = (
                current_contract.get("statement_hash")
                if isinstance(current_contract, Mapping)
                else None
            )
            roots = [
                item
                for item in snapshot.projection.get("claims", ())
                if item.get("claim_kind") == "ROOT"
                and item.get("lifecycle") == "ACTIVE"
                and item.get("contract_version") == snapshot.current_contract_version
                and item.get("statement_hash") == current_statement_hash
            ]
            root_id = str(roots[0]["claim_id"]) if len(roots) == 1 else None
        if not isinstance(root_id, str) or not root_id:
            contract_record = snapshot.projection.get("contract")
            contract = (
                contract_record.get("contract")
                if isinstance(contract_record, Mapping)
                else None
            )
            artifact_id = (
                contract_record.get("contract_artifact_id")
                if isinstance(contract_record, Mapping)
                else None
            )
            statement_hash = (
                contract_record.get("statement_hash")
                if isinstance(contract_record, Mapping)
                else None
            )
            if (
                not isinstance(contract, Mapping)
                or not isinstance(artifact_id, str)
                or not isinstance(statement_hash, str)
            ):
                raise RequestValidationError("当前合同缺少规范 ROOT 所需的冻结工件")
            _apply_product_command(
                kernel,
                args.run_id,
                "RegisterClaim",
                {
                    "contract_version": snapshot.current_contract_version,
                    "claim_kind": "ROOT",
                    "stable_label": f"root-v{snapshot.current_contract_version}",
                    "statement_artifact_id": artifact_id,
                    "statement_hash": statement_hash,
                    "normalized_statement": contract,
                },
                _managed_role_capability(config, "worker", "RegisterClaim", args.run_id),
            )
            root_id = str(_claim_by_label(
                kernel, args.run_id, f"root-v{snapshot.current_contract_version}"
            )["claim_id"])
        if not isinstance(root_id, str) or not root_id:
            raise RequestValidationError("当前合同缺少唯一有效 ROOT，不能开始宿主 Lean 重放")
        literature_plan = config.inbox_roots[0] / (
            f".rk-lean-literature-plan-v{snapshot.current_contract_version}.json"
        )
        literature_bytes = json.dumps(
            {
                "purpose": "host Lean replay of an already registered atomic Claim",
                "contract_version": snapshot.current_contract_version,
                "scope": "no new novelty claim; retain current literature audit",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        literature_plan.write_bytes(literature_bytes)
        plan_artifact = _import_product_file(
            kernel,
            config,
            args.run_id,
            literature_plan,
            _managed_role_capability(config, "main", "SubmitEvidence", args.run_id),
            logical_name=f"lean-literature-plan-v{snapshot.current_contract_version}",
            role="RESEARCH_MATERIAL",
        )
        _apply_product_command(
            kernel,
            args.run_id,
            "StartRun",
            {
                "contract_version": snapshot.current_contract_version,
                "literature_plan_artifact_id": plan_artifact.artifact_id,
                "budget_policy": {
                    "global": {
                        name: int(value)
                        for name, value in config.budget_policy.get(
                            "global_budget_limits", {}
                        ).items()
                    }
                },
            },
            _managed_role_capability(config, "main", "StartRun", args.run_id),
        )
        snapshot = _run_snapshot(kernel, args.run_id)
    if snapshot.status != "RUNNING":
        raise RequestValidationError("当前研究不在可执行检查点；请先恢复或开始研究")
    claim = _claim_by_label(kernel, args.run_id, args.fact_label)
    if claim.get("machine") == "KERNEL_VERIFIED":
        return "该 Claim 已有宿主 Lean 内核回执并完成机器轴晋级；未重复执行。", 0
    raw_profile = config.adapter_profiles.get("research-lean")
    binding_profile = config.adapter_profiles.get("lean-replay")
    closure = config.product.get("lean_dependency_closure")
    receipt_key = config.product.get("host_receipt_key_path")
    if (
        not isinstance(raw_profile, Mapping)
        or not isinstance(binding_profile, Mapping)
        or not isinstance(closure, Mapping)
        or not isinstance(receipt_key, str)
    ):
        raise RequestValidationError("管理员尚未完成 Lean 宿主权威配置，请重新运行“配置数学工具”")
    source = _product_inbox_file(args.source_file, config.inbox_roots)
    workspace = Path(str(raw_profile["workspace_root"])).resolve()
    output_root = Path(str(raw_profile["output_root"])).resolve()
    module_token = hashlib.sha256(
        f"{args.run_id}:{claim['claim_id']}:{source.read_bytes().hex()}".encode()
    ).hexdigest()[:16]
    source_relpath = f"RKProduct/M{module_token}/Main.lean"
    output_relpath = f"RKProduct/M{module_token}/Main.olean"
    project_source = workspace / source_relpath
    project_output = output_root / output_relpath
    if project_output.exists():
        pending = [
            item
            for item in snapshot.projection.get("host_execution_receipts", ())
            if item.get("claim_id") == claim["claim_id"]
            and item.get("status") == "COMPLETED"
            and item.get("exit_code") == 0
            and item.get("authority_eligible") == 1
            and item.get("consumed_by_feedback_id") is None
            and item.get("output_sha256") == hashlib.sha256(project_output.read_bytes()).hexdigest()
        ]
        if len(pending) == 1:
            host_cap = _host_product_capability(config)
            adapter = LeanReplayAdapter(AdapterProfile.from_mapping(raw_profile))
            receipt_key_path = Path(receipt_key)
            recovery_profile = {
                "adapter_version": str(raw_profile["version"]),
                "environment_profile_id": "lean-clean-product-v1",
                "source_commit": str(raw_profile["source_commit"]),
                "component": "lean-replay:lean-clean-product-v1",
                "toolchain": str(raw_profile["expected_toolchain"]),
                "binary_path": str(raw_profile["binary_path"]),
                "binary_sha256": str(raw_profile["binary_sha256"]),
                "source_result_path": str(project_source),
                "output_result_path": str(project_output),
                "dependency_closure_root": str(closure["root"]),
                "dependency_closure_sha256": str(closure["sha256"]),
                "dependency_closure_manifest_path": str(closure["manifest_path"]),
                "dependency_closure_manifest_sha256": str(closure["manifest_sha256"]),
                "allowed_axioms": list(raw_profile["allowed_axioms"]),
                "expected_statement_hash": str(claim["statement_hash"]),
                "expected_declaration_types": {args.declaration: args.declaration_type},
                "expected_declaration_module": ".".join(
                    Path(source_relpath).with_suffix("").parts
                ),
            }
            recovery_service = HostExecutionReceiptService(
                storage=SQLiteStorage(config.db_path, config.busy_timeout_ms),
                strategy=StrategyRunner({"lean-replay": adapter}),
                signing_key_path=receipt_key_path,
                capability=host_cap,
                id_generator=Uuid7Generator(),
                clock=SystemClock(),
                host_profiles={"lean-replay": recovery_profile},
                budget_limits={
                    str(name): int(value)
                    for name, value in config.budget_policy.get(
                        "global_budget_limits", {}
                    ).items()
                },
            )
            _import_product_file(
                kernel,
                config,
                args.run_id,
                source,
                host_cap,
                logical_name=f"lean-source-{module_token}",
                role="RESEARCH_MATERIAL",
            )
            _import_product_file(
                kernel,
                config,
                args.run_id,
                project_output,
                host_cap,
                logical_name=f"lean-olean-{module_token}",
                role="COMPONENT_RESULT",
                media_type="application/x-lean-olean",
            )
            feedback_id = recovery_service.consume_lean_replay(
                receipt_id=str(pending[0]["receipt_id"])
            )
            return (
                "已从中断检查点消费真实宿主 Lean 回执并完成 Claim 晋级。\n"
                f"事实：{args.fact_label}\n回执：{pending[0]['receipt_id']}\n"
                f"反馈：{feedback_id}",
                0,
            )
        raise RequestValidationError(
            "该 Lean 输出路径已存在但没有唯一可恢复回执；请保留现场交由管理员排查"
        )
    project_source.parent.mkdir(parents=True, exist_ok=True)
    project_source.write_bytes(source.read_bytes())

    host_cap = _host_product_capability(config)
    route_label = f"host-lean:{claim['claim_id']}:{module_token}"
    guard_snapshot = SQLiteStorage(config.db_path, config.busy_timeout_ms).guard_snapshot(
        args.run_id
    )
    route = next(
        (
            item
            for item in guard_snapshot["projection"].get("routes", ())
            if item.get("label") == route_label
        ),
        None,
    )
    if route is None:
        _apply_product_command(
            kernel,
            args.run_id,
            "RegisterRoute",
            {
                "contract_version": snapshot.current_contract_version,
                "target_claim_id": claim["claim_id"],
                "label": route_label,
                "representation": "Lean 4 theorem bound to atomic Claim",
                "tool_family": "Lean/Mathlib clean replay",
                "approach_root": {
                    "label": route_label,
                    "parent_root_ids": [],
                    "contact_epoch": 0,
                    "contamination": {},
                },
                "budget_policy": {"attempts": 2},
            },
            host_cap,
        )
        guard_snapshot = SQLiteStorage(config.db_path, config.busy_timeout_ms).guard_snapshot(
            args.run_id
        )
        route = next(
            item
            for item in guard_snapshot["projection"]["routes"]
            if item["label"] == route_label
        )
    route_id = str(route["route_id"])

    request = {
        "source_relpath": source_relpath,
        "output_relpath": output_relpath,
        "declarations": [args.declaration],
        "environment": _research_environment(config),
    }
    request_bytes = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    request_path = config.inbox_roots[0] / f".rk-lean-request-{module_token}.json"
    request_path.write_bytes(request_bytes)
    request_artifact = _import_product_file(
        kernel,
        config,
        args.run_id,
        request_path,
        host_cap,
        logical_name=f"lean-request-{module_token}",
        role="RESEARCH_MATERIAL",
    )
    attempts = [
        item
        for item in guard_snapshot["projection"].get("attempts", ())
        if item["route_id"] == route_id
    ]
    ordinal = max((int(item["ordinal"]) for item in attempts), default=0) + 1
    _apply_product_command(
        kernel,
        args.run_id,
        "RegisterAttempt",
        {
            "route_id": route_id,
            "ordinal": ordinal,
            "isolation_epoch": ordinal,
            "work_relpath": f"host-lean/{module_token}/attempt-{ordinal}",
            "allowed_write_set": [f"lean-output-{module_token}-{ordinal}"],
            "input_snapshot_digest": canonical_json_sha256(request),
        },
        host_cap,
    )
    guard_snapshot = SQLiteStorage(config.db_path, config.busy_timeout_ms).guard_snapshot(
        args.run_id
    )
    attempt = max(
        (
            item
            for item in guard_snapshot["projection"]["attempts"]
            if item["route_id"] == route_id
        ),
        key=lambda item: int(item["ordinal"]),
    )
    attempt_id = str(attempt["attempt_id"])
    _apply_product_command(
        kernel,
        args.run_id,
        "BindExecution",
        {
            "route_id": route_id,
            "attempt_id": attempt_id,
            "adapter_name": "lean-replay",
            "adapter_version": str(raw_profile["version"]),
            "source_commit": str(raw_profile["source_commit"]),
            "environment_profile_id": "lean-clean-product-v1",
            "invocation_artifact_id": request_artifact.artifact_id,
            "external_ids": {},
        },
        host_cap,
    )
    _apply_product_command(
        kernel,
        args.run_id,
        "RecordBudget",
        {
            "route_id": route_id,
            "attempt_id": attempt_id,
            "event_kind": "RESERVATION",
            "resource_kind": "WALL_SECOND",
            "amount_microunits": 300_000_000,
            "unit": "microsecond",
            "provider_usage": {"component": "lean-replay:lean-clean-product-v1"},
        },
        host_cap,
    )
    holder = f"host-lean-{module_token}"
    _apply_product_command(
        kernel,
        args.run_id,
        "AcquireLease",
        {"attempt_id": attempt_id, "holder_id": holder, "ttl_seconds": 900},
        host_cap,
    )
    host_profile = {
        "adapter_version": str(raw_profile["version"]),
        "environment_profile_id": "lean-clean-product-v1",
        "source_commit": str(raw_profile["source_commit"]),
        "component": "lean-replay:lean-clean-product-v1",
        "toolchain": str(raw_profile["expected_toolchain"]),
        "binary_path": str(raw_profile["binary_path"]),
        "binary_sha256": str(raw_profile["binary_sha256"]),
        "source_result_path": str(project_source),
        "output_result_path": str(project_output),
        "dependency_closure_root": str(closure["root"]),
        "dependency_closure_sha256": str(closure["sha256"]),
        "dependency_closure_manifest_path": str(closure["manifest_path"]),
        "dependency_closure_manifest_sha256": str(closure["manifest_sha256"]),
        "allowed_axioms": list(raw_profile["allowed_axioms"]),
        "expected_statement_hash": str(claim["statement_hash"]),
        "expected_declaration_types": {args.declaration: args.declaration_type},
        "expected_declaration_module": ".".join(Path(source_relpath).with_suffix("").parts),
        "execution_environment": _research_environment(config),
    }
    adapter = LeanReplayAdapter(AdapterProfile.from_mapping(raw_profile))
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, config.busy_timeout_ms),
        strategy=StrategyRunner({"lean-replay": adapter}),
        signing_key_path=Path(receipt_key),
        capability=host_cap,
        id_generator=Uuid7Generator(),
        clock=SystemClock(),
        host_profiles={"lean-replay": host_profile},
        budget_limits={
            str(name): int(value)
            for name, value in config.budget_policy.get("global_budget_limits", {}).items()
        },
    )
    try:
        executed = service.execute(run_id=args.run_id, attempt_id=attempt_id, request=request)
    except HostExecutionNotAuthoritative as error:
        invocation = error.invocation
        result_path = config.inbox_roots[0] / f"lean-failure-{module_token}.json"
        result_path.write_text(
            json.dumps(invocation.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result_artifact = _import_product_file(
            kernel,
            config,
            args.run_id,
            result_path,
            host_cap,
            logical_name=f"lean-failure-{module_token}",
            role="COMPONENT_RESULT",
        )
        source_artifact = _import_product_file(
            kernel,
            config,
            args.run_id,
            source,
            host_cap,
            logical_name=f"lean-source-{module_token}",
            role="RESEARCH_MATERIAL",
        )
        _apply_product_command(
            kernel,
            args.run_id,
            "RecordLeanFeedback",
            {
                "claim_id": claim["claim_id"],
                "contract_version": snapshot.current_contract_version,
                "environment_profile_id": "lean-clean-product-v1",
                "toolchain": str(raw_profile["expected_toolchain"]),
                "mathlib_commit": str(raw_profile["source_commit"]),
                "source_artifact_id": source_artifact.artifact_id,
                "output_artifact_id": result_artifact.artifact_id,
                "feedback_kind": "FAILED_GOAL",
                "diagnostic": {
                    "status": invocation.status,
                    "receipt_id": error.receipt_id,
                    "block_reasons": list(error.reasons),
                    "result_hash": invocation.result_hash,
                    "repair_hint": "只修订这个 Claim 的 Lean 源文件后重试；其他 sibling 不重跑。",
                },
            },
            _managed_role_capability(config, "verifier", "RecordLeanFeedback", args.run_id),
        )
        raise RequestValidationError(
            "Lean 重放拒绝该 Claim；真实诊断已进入同一研究状态，可局部修订后重试"
        ) from error
    finally:
        latest = _run_snapshot(kernel, args.run_id)
        active = next(
            (
                item
                for item in latest.projection.get("active_attempts", ())
                if item.get("attempt_id") == attempt_id and item.get("lease_id")
            ),
            None,
        )
        if active is not None:
            terminal = "SUCCEEDED" if "executed" in locals() else "FAILED"
            _apply_product_command(
                kernel,
                args.run_id,
                "ReleaseLease",
                {
                    "lease_id": active["lease_id"],
                    "holder_id": holder,
                    "terminal_attempt_status": terminal,
                },
                host_cap,
            )
    source_artifact = _import_product_file(
        kernel,
        config,
        args.run_id,
        source,
        host_cap,
        logical_name=f"lean-source-{module_token}",
        role="RESEARCH_MATERIAL",
    )
    del source_artifact
    _import_product_file(
        kernel,
        config,
        args.run_id,
        project_output,
        host_cap,
        logical_name=f"lean-olean-{module_token}",
        role="COMPONENT_RESULT",
        media_type="application/x-lean-olean",
    )
    feedback_id = service.consume_lean_replay(receipt_id=executed.receipt_id)
    return (
        "宿主 Lean 已通过并晋级当前 Claim。\n"
        f"事实：{args.fact_label}\n声明：{args.declaration}\n"
        f"回执：{executed.receipt_id}\n反馈：{feedback_id}\n"
        f"耗时：{executed.invocation.wall_time_ms} ms",
        0,
    )


def _soft_verify_fact_product(
    args: argparse.Namespace,
    config: KernelConfig,
    kernel: ResearchKernel,
    snapshot: RunSnapshot,
) -> tuple[str, int]:
    claim = _claim_by_label(kernel, args.run_id, args.fact_label)
    normalized = claim.get("normalized_statement")
    if not isinstance(normalized, Mapping):
        raise RequestValidationError("Claim 缺少可供软 verifier 检查的规范陈述")
    statement = str(normalized.get("statement", "")).strip()
    proof = str(normalized.get("proof", "")).strip()
    if not statement or not proof:
        raise RequestValidationError("Claim 缺少陈述或证明正文")
    runtime = build_component_runtime(config, _research_environment(config))
    receipt = runtime.execute_function(
        call_id=str(uuid.uuid4()),
        function_name="verify_rethlas",
        arguments={"statement": statement, "proof": proof},
        environment={},
    )
    body = json.dumps(receipt.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
    output = config.inbox_roots[0] / f"rethlas-{receipt.receipt_id[:20]}.json"
    output.write_bytes(body)
    _import_product_file(
        kernel,
        config,
        args.run_id,
        output,
        _managed_role_capability(config, "verifier", "SubmitEvidence", args.run_id),
        logical_name=f"rethlas-{args.fact_label}-{receipt.receipt_id[:12]}",
        role="COMPONENT_RESULT",
    )
    result = receipt.result
    payload = result.get("payload") if isinstance(result, Mapping) else None
    verdict = payload.get("verdict") if isinstance(payload, Mapping) else None
    hints = payload.get("repair_hints") if isinstance(payload, Mapping) else None
    if receipt.status == "COMPLETED" and verdict == "wrong":
        _apply_product_command(
            kernel,
            args.run_id,
            "VerifyAtomicClaim",
            {
                "contract_version": snapshot.current_contract_version,
                "claim_id": claim["claim_id"],
                "backend": "SOFT_VERIFIER",
                "verdict": "REJECTED",
                "repair_feedback": str(hints or "Rethlas 报告当前证明存在缺口"),
            },
            _managed_role_capability(config, "verifier", "VerifyAtomicClaim", args.run_id),
        )
    return (
        "软 verifier 调用已进入同一研究状态。\n"
        f"组件：{receipt.component_id}\n状态：{receipt.status}\n"
        f"回执：{receipt.receipt_id}\n信任上限：SOFT_MODEL\n"
        + (
            "结论为 wrong，修复提示已保存且未污染事实图。"
            if verdict == "wrong"
            else "该结果不能独立接受或晋级 Claim。"
        ),
        0 if receipt.status == "COMPLETED" else 2,
    )


def _run_fact_product_action(args: argparse.Namespace, config: KernelConfig) -> tuple[str, int]:
    kernel = ResearchKernel.from_config(config)
    snapshot = _run_snapshot(kernel, args.run_id)
    contract_version = snapshot.current_contract_version
    if args.operation == "lean_verify_fact":
        return _lean_verify_fact_product(args, config, kernel, snapshot)
    if args.operation == "soft_verify_fact":
        return _soft_verify_fact_product(args, config, kernel, snapshot)
    if args.operation == "run_fact_queue":
        queue_value = _read_object(_product_inbox_file(args.queue_file, config.inbox_roots))
        tasks = queue_value.get("工作项")
        if not isinstance(tasks, list) or not tasks:
            raise RequestValidationError("事实队列须包含非空“工作项”数组")
        completed: list[str] = []
        for index, task in enumerate(tasks, 1):
            if not isinstance(task, Mapping):
                raise RequestValidationError(f"工作项 {index} 不是对象")
            claim_path = Path(str(task.get("事实文件", "")))
            review_path = Path(str(task.get("验证文件", "")))
            verdict = str(task.get("结论", ""))
            if not claim_path.is_absolute():
                claim_path = config.inbox_roots[0] / claim_path
            if not review_path.is_absolute():
                review_path = config.inbox_roots[0] / review_path
            _run_fact_product_action(
                argparse.Namespace(
                    operation="submit_fact",
                    run_id=args.run_id,
                    claim_file=claim_path,
                ),
                config,
            )
            label = str(_read_object(claim_path)["标签"])
            _run_fact_product_action(
                argparse.Namespace(
                    operation="verify_fact",
                    run_id=args.run_id,
                    fact_label=label,
                    review_file=review_path,
                    fact_verdict=verdict,
                ),
                config,
            )
            completed.append(f"{index}:{label}:{verdict}")
        return "事实队列已按验证栅栏顺序完成：\n- " + "\n- ".join(completed), 0
    if args.operation == "submit_fact":
        value = _read_object(_product_inbox_file(args.claim_file, config.inbox_roots))
        required = ("标签", "陈述", "证明")
        missing = [name for name in required if not str(value.get(name, "")).strip()]
        if missing:
            raise RequestValidationError("原子事实缺少：" + "、".join(missing))
        predecessors = value.get("前驱事实", [])
        if not isinstance(predecessors, list) or not all(
            isinstance(item, str) and item for item in predecessors
        ):
            raise RequestValidationError("“前驱事实”必须是事实标签数组")
        worker = _managed_role_capability(config, "worker", "SubmitEvidence", args.run_id)
        normalized = {
            "atomic": bool(value.get("原子", True)),
            "statement": str(value["陈述"]),
            "proof": str(value["证明"]),
            "claim_type": str(value.get("类型", "LEMMA")),
            "source": str(value.get("来源", "worker")),
        }
        data = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        generated = config.inbox_roots[0] / (
            ".rk-claim-" + hashlib.sha256(data).hexdigest()[:20] + ".json"
        )
        if generated.exists() and generated.read_bytes() != data:
            raise RequestValidationError("派生 Claim 工件发生摘要冲突")
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(data)
        artifact = _import_product_file(
            kernel,
            config,
            args.run_id,
            generated,
            worker,
            logical_name=f"claim-{value['标签']}",
            role="CLAIM_STATEMENT",
        )
        claim_payload = {
            "contract_version": contract_version,
            "claim_kind": str(value.get("类型", "LEMMA")),
            "stable_label": str(value["标签"]),
            "statement_artifact_id": artifact.artifact_id,
            "statement_hash": hashlib.sha256(data).hexdigest(),
            "normalized_statement": normalized,
        }
        existing = [
            item
            for item in snapshot.projection.get("claims", ())
            if item.get("stable_label") == str(value["标签"]) and item.get("lifecycle") == "ACTIVE"
        ]
        if existing:
            if existing[0].get("statement_hash") != claim_payload["statement_hash"]:
                raise RequestValidationError("事实标签已存在，但陈述内容不同")
        else:
            _apply_product_command(
                kernel,
                args.run_id,
                "RegisterClaim",
                claim_payload,
                _managed_role_capability(config, "worker", "RegisterClaim", args.run_id),
            )
        claim = _claim_by_label(kernel, args.run_id, str(value["标签"]))
        for predecessor_label in predecessors:
            predecessor = _fact_id_by_label(kernel, args.run_id, predecessor_label)
            edge_exists = any(
                item.get("from_claim_id") == predecessor
                and item.get("to_claim_id") == claim["claim_id"]
                and item.get("status") == "ACTIVE"
                for item in _run_snapshot(kernel, args.run_id).projection.get("edges", ())
            )
            if edge_exists:
                continue
            _apply_product_command(
                kernel,
                args.run_id,
                "RegisterClaimEdge",
                {
                    "contract_version": contract_version,
                    "from_claim_id": predecessor,
                    "to_claim_id": claim["claim_id"],
                    "edge_kind": "DEPENDS_ON",
                    "direction": "FORWARD",
                    "justification_kind": str(value.get("边理由", "DEFINITIONAL")),
                    "justification_ref": artifact.artifact_id,
                },
                _managed_role_capability(config, "worker", "RegisterClaimEdge", args.run_id),
            )
        return (
            f"原子 Claim 已提交：{value['标签']}。当前尚不可依赖，须由 Verifier 通过统一写门。",
            0,
        )
    if args.operation == "verify_fact":
        claim = _claim_by_label(kernel, args.run_id, args.fact_label)
        attested, resolved_review = _verify_signed_review(
            config,
            args.review_file,
            {
                "run_id": args.run_id,
                "contract_version": contract_version,
                "claim_id": claim["claim_id"],
                "statement_hash": claim["statement_hash"],
            },
        )
        imported = dict(attested["import_fields"])
        verdict = str(imported["verdict"])
        desired = "ACCEPTED" if verdict == "ACCEPT" else "REJECTED"
        prior_verifications = [
            item
            for item in snapshot.projection.get("atomic_verifications", ())
            if item.get("claim_id") == claim["claim_id"] and item.get("verdict") == desired
        ]
        if prior_verifications:
            return (
                "已复用该 Claim 的既有验证终态；未重复写入审查或事实。",
                0,
            )
        verifier = _attesting_verifier_capability(
            config, attested, "SubmitEvidence", args.run_id
        )
        review = _import_product_file(
            kernel,
            config,
            args.run_id,
            resolved_review,
            verifier,
            logical_name=f"atomic-review-{args.fact_label}",
            role="PEER_REVIEW",
        )
        if verdict != "ACCEPT":
            _apply_product_command(
                kernel,
                args.run_id,
                "VerifyAtomicClaim",
                {
                    "contract_version": contract_version,
                    "claim_id": claim["claim_id"],
                    "backend": "SOFT_VERIFIER",
                    "verdict": "REJECTED",
                    "repair_feedback": json.dumps(
                        imported.get("checks", {}), ensure_ascii=False, sort_keys=True
                    ),
                },
                _managed_role_capability(config, "verifier", "VerifyAtomicClaim", args.run_id),
            )
            return "Verifier 已拒绝该 Claim；修复意见已持久保存，事实图未被污染。", 0
        peer_payload = dict(imported)
        peer_payload["review_artifact_id"] = review.artifact_id
        peer_payload["verifier_attestation"] = {
            "artifact_sha256": review.sha256,
            "verifier_identity_id": attested["verifier_identity_id"],
            "verifier_subject_id": attested["verifier_subject_id"],
            "promotion_eligible": attested["promotion_eligible"],
            "authority": attested["authority"],
            "claim_id": claim["claim_id"],
            "contract_version": contract_version,
            "statement_hash": claim["statement_hash"],
            "verdict": verdict,
        }
        _apply_product_command(
            kernel,
            args.run_id,
            "RecordPeerReview",
            peer_payload,
            _attesting_verifier_capability(config, attested, "RecordPeerReview", args.run_id),
        )
        refreshed = _run_snapshot(kernel, args.run_id)
        review_id = refreshed.projection["peer_reviews"][-1]["review_id"]
        _apply_product_command(
            kernel,
            args.run_id,
            "VerifyAtomicClaim",
            {
                "contract_version": contract_version,
                "claim_id": claim["claim_id"],
                "backend": "MANAGED_PEER",
                "verdict": "ACCEPTED",
                "verification_ref": review_id,
            },
            _attesting_verifier_capability(config, attested, "VerifyAtomicClaim", args.run_id),
        )
        return "独立 Verifier 已通过该 Claim；它现在进入可依赖事实图。", 0
    if args.operation == "search_fact":
        if args.limit < 1 or args.limit > 100:
            raise RequestValidationError("检索条数须在 1 到 100 之间")
        found = _run_snapshot(
            kernel,
            args.run_id,
            fact_query={"operation": "search", "query": args.query, "limit": args.limit},
        ).projection["fact_graph"]
        if not found:
            return "未找到相关的已验证事实。", 0
        claims_by_id = {
            str(item["claim_id"]): item
            for item in _run_snapshot(kernel, args.run_id).projection.get("claims", ())
            if isinstance(item, Mapping) and item.get("claim_id")
        }
        lines = ["相关已验证事实："]
        for item in found:
            claim = claims_by_id.get(str(item.get("fact_id")), item)
            statement = claim.get("normalized_statement", {}).get("statement", "")
            lines.append(f"- {claim.get('stable_label')}：{statement}")
        return "\n".join(lines), 0
    if args.operation == "register_bridge":
        source = _claim_by_label(kernel, args.run_id, args.source_label)
        target = _claim_by_label(kernel, args.run_id, args.target_label)
        spec = _read_object(_product_inbox_file(args.spec_file, config.inbox_roots))
        bridge_spec = spec.get("桥接规格")
        if not isinstance(bridge_spec, Mapping):
            raise RequestValidationError("桥接 JSON 缺少“桥接规格”对象")
        verifier = _attesting_verifier_capability(
            config, attested, "SubmitEvidence", args.run_id
        )
        audit_path = _product_inbox_file(args.audit_file, config.inbox_roots)
        audit_digest = hashlib.sha256(audit_path.read_bytes()).hexdigest()
        current = _run_snapshot(kernel, args.run_id).projection
        matching_artifacts = {
            str(item["artifact_id"])
            for item in current.get("artifacts", ())
            if isinstance(item, Mapping) and item.get("sha256") == audit_digest
        }
        matching_reviews = [
            item
            for item in current.get("peer_reviews", ())
            if isinstance(item, Mapping)
            and item.get("claim_id") == target["claim_id"]
            and item.get("review_artifact_id") in matching_artifacts
            and item.get("verdict") == "ACCEPT"
        ]
        if matching_reviews:
            audit_id = matching_reviews[-1]["review_id"]
        else:
            audit = _import_product_file(
                kernel,
                config,
                args.run_id,
                args.audit_file,
                verifier,
                logical_name=f"bridge-audit-{args.target_label}",
                role="PEER_REVIEW",
            )
            _apply_product_command(
                kernel,
                args.run_id,
                "RecordPeerReview",
                {
                    "claim_id": target["claim_id"],
                    "contract_version": contract_version,
                    "statement_hash": target["statement_hash"],
                    "review_artifact_id": audit.artifact_id,
                    "verdict": "ACCEPT",
                    "checklist": {
                        "proof_checked": True,
                        "scope_checked": True,
                        "blind_review": True,
                    },
                    "source_graph": {"author_subject_ids": ["managed-worker"]},
                },
                _attesting_verifier_capability(
                    config, attested, "RecordPeerReview", args.run_id
                ),
            )
            audit_id = _run_snapshot(kernel, args.run_id).projection["peer_reviews"][-1][
                "review_id"
            ]
        main_submit = _managed_role_capability(config, "main", "SubmitEvidence", args.run_id)
        backtranslation = _import_product_file(
            kernel,
            config,
            args.run_id,
            args.backtranslation_file,
            main_submit,
            logical_name=f"bridge-backtranslation-{args.target_label}",
            role="RESEARCH_MATERIAL",
        )
        bridge_id = str(uuid.uuid4())
        directionality = str(spec.get("方向", "ONE_WAY_VALID"))
        bridge_payload = {
            "bridge_id": bridge_id,
            "contract_version": contract_version,
            "source_claim_id": source["claim_id"],
            "target_claim_id": target["claim_id"],
            "directionality": directionality,
            "term_mapping": spec.get("术语映射", {}),
            "forward_obligations": spec.get("正向义务", []),
            "reverse_obligations": spec.get("反向义务", []),
            "loss_accounting": spec.get("损失记录", {}),
            "bridge_spec": bridge_spec,
            "target_audit_review_id": audit_id,
            "backtranslation_artifact_id": backtranslation.artifact_id,
        }
        expected_bridge = {
            key: bridge_payload[key]
            for key in (
                "contract_version",
                "source_claim_id",
                "target_claim_id",
                "directionality",
                "term_mapping",
                "forward_obligations",
                "reverse_obligations",
                "loss_accounting",
                "bridge_spec",
            )
        }
        existing_bridges = [
            item
            for item in _run_snapshot(kernel, args.run_id).projection.get("bridges", ())
            if isinstance(item, Mapping)
            and all(item.get(key) == value for key, value in expected_bridge.items())
        ]
        if existing_bridges:
            bridge_id = str(existing_bridges[0]["bridge_id"])
        else:
            _apply_product_command(
                kernel,
                args.run_id,
                "RegisterBridge",
                bridge_payload,
                _managed_role_capability(config, "main", "RegisterBridge", args.run_id),
            )
        edge_payload = {
            "contract_version": contract_version,
            "from_claim_id": source["claim_id"],
            "to_claim_id": target["claim_id"],
            "edge_kind": "IMPLIES",
            "direction": "FORWARD",
            "justification_kind": "BRIDGE",
            "justification_ref": bridge_id,
        }
        existing_edge = any(
            isinstance(item, Mapping)
            and item.get("contract_version", contract_version) == contract_version
            and item.get("from_claim_id") == source["claim_id"]
            and item.get("to_claim_id") == target["claim_id"]
            and item.get("edge_kind") == "IMPLIES"
            and item.get("direction") == "FORWARD"
            and item.get("status") == "ACTIVE"
            for item in _run_snapshot(kernel, args.run_id).projection.get("edges", ())
        )
        if not existing_edge:
            _apply_product_command(
                kernel,
                args.run_id,
                "RegisterClaimEdge",
                edge_payload,
                _managed_role_capability(config, "worker", "RegisterClaimEdge", args.run_id),
            )
        return (
            "BridgeSpec 已登记：目标域独立审查、正向义务和回译工件均已绑定；"
            f"方向状态为 {directionality}。",
            0,
        )
    if args.operation == "amend_contract":
        affected = _claim_by_label(kernel, args.run_id, args.affected_label)
        topic_path = _product_inbox_file(args.topic_file, config.inbox_roots)
        replacement = _topic_request(
            _read_object(topic_path), topic_path, allowed_roots=config.inbox_roots
        )["contract"]
        main_submit = _managed_role_capability(config, "main", "SubmitEvidence", args.run_id)
        defect_path = _product_inbox_file(args.defect_file, config.inbox_roots)
        defect_data = defect_path.read_bytes()
        defect_input = ArtifactInput(
            "contract_defect" + defect_path.suffix.lower(),
            str(defect_path),
            hashlib.sha256(defect_data).hexdigest(),
            len(defect_data),
            mimetypes.guess_type(defect_path.name)[0] or "text/plain",
        )
        _apply_product_command(
            kernel,
            args.run_id,
            "SubmitEvidence",
            {
                "claim_id": affected["claim_id"],
                "contract_version": contract_version,
                "statement_hash": affected["statement_hash"],
                "evidence_type": "NATURAL_LANGUAGE_PROOF",
                "evidence_strength": "HUMAN_ATTESTED",
                "artifact_input_names": [defect_input.name],
                "scope": {
                    "claim_id": affected["claim_id"],
                    "contract_version": contract_version,
                    "statement_hash": affected["statement_hash"],
                },
                "provenance": {"actor": "managed-contract-auditor"},
                "evidence_root": {"root_kind": "HUMAN", "source_graph": {}},
            },
            main_submit,
            (defect_input,),
        )
        evidence_id = _run_snapshot(kernel, args.run_id).projection["evidence"][-1]["evidence_id"]
        approval = _import_product_file(
            kernel,
            config,
            args.run_id,
            args.approval_file,
            _managed_role_capability(config, "verifier", "SubmitEvidence", args.run_id),
            logical_name=f"contract-approval-{contract_version}",
            role="PEER_REVIEW",
        )
        _apply_product_command(
            kernel,
            args.run_id,
            "RecordPeerReview",
            {
                "claim_id": affected["claim_id"],
                "contract_version": contract_version,
                "statement_hash": affected["statement_hash"],
                "review_artifact_id": approval.artifact_id,
                "verdict": "ACCEPT",
                "checklist": {
                    "proof_checked": True,
                    "scope_checked": True,
                    "blind_review": True,
                    "amendment_approved": True,
                    "amendment_role": "CONTRACT_OWNER",
                },
                "source_graph": {"author_subject_ids": ["managed-worker"]},
            },
            _managed_role_capability(config, "verifier", "RecordPeerReview", args.run_id),
        )
        approval_id = _run_snapshot(kernel, args.run_id).projection["peer_reviews"][-1]["review_id"]
        patch_artifact = _import_product_file(
            kernel,
            config,
            args.run_id,
            args.topic_file,
            main_submit,
            logical_name=f"contract-patch-{contract_version}",
            role="RESEARCH_MATERIAL",
        )
        impact = _import_product_file(
            kernel,
            config,
            args.run_id,
            args.impact_file,
            main_submit,
            logical_name=f"contract-impact-{contract_version}",
            role="RESEARCH_MATERIAL",
        )
        _apply_product_command(
            kernel,
            args.run_id,
            "ProposeContractDefect",
            {
                "contract_version": contract_version,
                "defect_type": "BUDGET_SCOPE",
                "evidence_refs": [evidence_id],
                "affected_claim_ids": [affected["claim_id"]],
                "proposed_patch_artifact_id": patch_artifact.artifact_id,
            },
            _managed_role_capability(config, "main", "ProposeContractDefect", args.run_id),
        )
        _apply_product_command(
            kernel,
            args.run_id,
            "AmendContract",
            {
                "base_version": contract_version,
                "replacement_contract": replacement,
                "patch_artifact_id": patch_artifact.artifact_id,
                "approvals": [approval_id],
                "impact_analysis_artifact_id": impact.artifact_id,
            },
            _managed_role_capability(config, "main", "AmendContract", args.run_id),
        )
        return (
            f"合同已从 v{contract_version} 修订为 v{contract_version + 1}；"
            "受影响反向依赖闭包已失效，未受影响事实迁入新版本。",
            0,
        )
    if args.operation in {"prepare_closure_review", "close_proof"}:
        parent = _claim_by_label(kernel, args.run_id, args.parent_label)
        projection = _run_snapshot(kernel, args.run_id).projection
        active_edges = [
            item
            for item in projection.get("edges", ())
            if item.get("status") == "ACTIVE"
        ]
        direct_incoming = [
            item for item in active_edges if item.get("to_claim_id") == parent["claim_id"]
        ]
        child_ids = [str(item["from_claim_id"]) for item in direct_incoming]
        if not child_ids:
            raise RequestValidationError("组合父 Claim 至少需要一个有效前驱")
        selected_claim_ids = {str(parent["claim_id"]), *child_ids}
        selected_edges: list[Mapping[str, Any]] = []
        frontier = list(child_ids)
        while frontier:
            target_id = frontier.pop()
            for edge in active_edges:
                source_id = str(edge.get("from_claim_id"))
                if str(edge.get("to_claim_id")) != target_id:
                    continue
                if edge not in selected_edges:
                    selected_edges.append(edge)
                if source_id not in selected_claim_ids:
                    selected_claim_ids.add(source_id)
                    frontier.append(source_id)
        for edge in direct_incoming:
            if edge not in selected_edges:
                selected_edges.append(edge)
        # Register the composition shape as OPEN.  The CLI has no authority to mark any of the
        # six parts HUMAN_ATTESTED; those values can only arrive in the signed verifier artifact
        # below and are copied into the submitted witness verbatim after signature validation.
        open_part = {"ref": "等待独立 verifier", "status": "OPEN"}
        payload = {
            "contract_version": contract_version,
            "parent_claim_id": parent["claim_id"],
            "child_claim_ids": child_ids,
            "local_domain": {"scope": "selected verified dependency graph"},
            **{
                name: open_part
                for name in (
                    "coverage",
                    "compatibility",
                    "invariant",
                    "progress",
                    "boundary",
                    "simultaneous_choice",
                )
            },
            "composition_rule": "HUMAN_ARGUMENT",
            "closure_theorem_ref": "等待独立 verifier 的签名闭合结论",
            "missing_conditions": [{"condition": "INDEPENDENT_SIGNED_COMPOSITION_REVIEW"}],
            "displacement_status": "NO_DISPLACEMENT",
        }
        main = _managed_role_capability(
            config, "main", "RegisterCompositionObligation", args.run_id
        )
        matching_obligations = [
            item
            for item in projection.get("obligations", ())
            if item.get("parent_claim_id") == parent["claim_id"] and item.get("status") == "OPEN"
        ]
        if not matching_obligations:
            _apply_product_command(
                kernel, args.run_id, "RegisterCompositionObligation", payload, main
            )
            projection = _run_snapshot(kernel, args.run_id).projection
            matching_obligations = [projection["obligations"][-1]]
        claims = {str(x["claim_id"]): x for x in projection["claims"]}
        if args.operation == "prepare_closure_review":
            if args.output.exists() and not args.force:
                raise FileExistsError("闭合审查包已存在；如确定覆盖，请加 --覆盖")
            review_request = {
                "schema_version": "rk.independent-review-request.v1",
                "artifact_kind": "COMPOSITION_REVIEW",
                "binding_base": {
                    "run_id": args.run_id,
                    "contract_version": contract_version,
                    "claim_id": parent["claim_id"],
                    "statement_hash": parent["statement_hash"],
                },
                "selected_graph_skeleton": {
                    "parent": {
                        "claim_id": parent["claim_id"],
                        "statement_revision": 1,
                        "statement_hash": parent["statement_hash"],
                    },
                    "claims": [
                        {
                            "claim_id": claim_id,
                            "statement_revision": 1,
                            "statement_hash": claims[claim_id]["statement_hash"],
                            "contract_version": contract_version,
                        }
                        for claim_id in sorted(selected_claim_ids)
                    ],
                    "edges": [
                        {
                            "edge_id": item["edge_id"],
                            "from": item["from_claim_id"],
                            "to": item["to_claim_id"],
                            "edge_kind": item["edge_kind"],
                            "direction": item["direction"],
                            "justification_kind": item.get(
                                "justification_kind", "DEFINITIONAL"
                            ),
                            "justification_ref": item.get(
                                "justification_ref", "整体同行复核"
                            ),
                        }
                        for item in selected_edges
                    ],
                    "obligation_ids": [
                        item["obligation_id"] for item in matching_obligations
                    ],
                    "bridges": [],
                    "cuts": [],
                },
                "required_signed_checks": [
                    "proof_checked",
                    "scope_checked",
                    "coverage",
                    "compatibility",
                    "invariant",
                    "progress",
                    "boundary",
                    "simultaneous_choice",
                ],
                "materialization_rule": {
                    "part_status": "copy each signed check.status exactly",
                    "part_ref": "copy each signed check.conclusion exactly",
                    "closure_theorem_ref": "copy signed proof_checked.conclusion exactly",
                    "digest": "rk.cgraph.v1 selected_subgraph_digest after materialization",
                },
                "reviewer_must_supply": [
                    "verifier_identity_id",
                    "independence",
                    "verdict",
                    "all checks including passed/status/conclusion/evidence_refs",
                    "selected_subgraph_digest",
                    "signature",
                ],
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(review_request, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return (
                f"闭合审查包已生成：{args.output.resolve()}\n"
                "其中没有盲审声明、审查结论或晋级状态；须由独立 Verifier 完整填写并签名。",
                0,
            )
        raw_review = _read_object(_product_inbox_file(args.review_file, config.inbox_roots))
        raw_checks = raw_review.get("checks")
        if not isinstance(raw_checks, Mapping):
            raise RequestValidationError("签名整体验证产物缺少 checks")
        signed_parts: dict[str, Any] = {}
        for name in (
            "coverage", "compatibility", "invariant", "progress", "boundary",
            "simultaneous_choice",
        ):
            signed = raw_checks.get(name)
            if not isinstance(signed, Mapping):
                raise RequestValidationError(f"签名整体验证产物缺少 {name}")
            signed_parts[name] = {
                "ref": str(signed.get("conclusion", "")),
                "status": signed.get("status"),
            }
        proof_check = raw_checks.get("proof_checked")
        if not isinstance(proof_check, Mapping):
            raise RequestValidationError("签名整体验证产物缺少 proof_checked")
        closure_ref = str(proof_check.get("conclusion", ""))
        selected = {
            "schema": "rk.cgraph.v1",
            "run_id": args.run_id,
            "contract_version": contract_version,
            "parent": {
                "claim_id": parent["claim_id"],
                "statement_revision": 1,
                "statement_hash": parent["statement_hash"],
            },
            "claims": [
                {
                    "claim_id": cid,
                    "statement_revision": 1,
                    "statement_hash": claims[cid]["statement_hash"],
                    "contract_version": contract_version,
                }
                for cid in sorted(selected_claim_ids)
            ],
            "edges": [
                {
                    "edge_id": x["edge_id"],
                    "from": x["from_claim_id"],
                    "to": x["to_claim_id"],
                    "edge_kind": x["edge_kind"],
                    "direction": x["direction"],
                    "justification_kind": x.get("justification_kind", "DEFINITIONAL"),
                    "justification_ref": x.get("justification_ref", "整体同行复核"),
                }
                for x in selected_edges
            ],
            "obligations": [
                {
                    "obligation_id": item["obligation_id"],
                    "composition_rule": "HUMAN_ARGUMENT",
                    "closure_theorem_ref": closure_ref,
                    "parts": dict(signed_parts),
                }
                for item in matching_obligations
            ],
            "bridges": [],
            "cuts": [],
        }
        digest = selected_subgraph_digest(selected)
        attested, resolved_review = _verify_signed_review(
            config,
            args.review_file,
            {
                "run_id": args.run_id,
                "contract_version": contract_version,
                "claim_id": parent["claim_id"],
                "statement_hash": parent["statement_hash"],
                "selected_subgraph_digest": digest,
            },
        )
        if not attested.get("promotion_eligible"):
            raise RequestValidationError("整体验证结论不是可晋级的独立接受结论")
        verifier = _attesting_verifier_capability(
            config, attested, "SubmitEvidence", args.run_id
        )
        review = _import_product_file(
            kernel,
            config,
            args.run_id,
            resolved_review,
            verifier,
            logical_name=f"closure-{args.parent_label}",
            role="PEER_REVIEW",
        )
        review_payload = dict(attested["import_fields"])
        review_payload.update({
            "review_artifact_id": review.artifact_id,
            "verifier_attestation": {
                "artifact_sha256": review.sha256,
                "verifier_identity_id": attested["verifier_identity_id"],
                "verifier_subject_id": attested["verifier_subject_id"],
                "promotion_eligible": attested["promotion_eligible"],
                "authority": attested["authority"],
                "claim_id": parent["claim_id"],
                "contract_version": contract_version,
                "statement_hash": parent["statement_hash"],
                "verdict": review_payload.get("verdict"),
                "selected_subgraph_digest": digest,
            },
        })
        prior_review = [
            item
            for item in _run_snapshot(kernel, args.run_id).projection["peer_reviews"]
            if item.get("claim_id") == parent["claim_id"]
            and item.get("review_artifact_id") == review.artifact_id
            and item.get("selected_subgraph_digest") == digest
        ]
        if prior_review:
            review_id = prior_review[-1]["review_id"]
        else:
            _apply_product_command(
                kernel,
                args.run_id,
                "RecordPeerReview",
                review_payload,
                _attesting_verifier_capability(
                    config, attested, "RecordPeerReview", args.run_id
                ),
            )
            review_id = _run_snapshot(kernel, args.run_id).projection["peer_reviews"][-1][
                "review_id"
            ]
        eligible_review_ids = [
            str(item["review_id"])
            for item in _run_snapshot(kernel, args.run_id).projection["peer_reviews"]
            if item.get("claim_id") == parent["claim_id"]
            and item.get("selected_subgraph_digest") == digest
            and item.get("trust_class") == "MANAGED_PEER_REVIEW"
            and item.get("authority_effect") == "PEER_PROMOTION_ELIGIBLE"
            and item.get("promotion_eligible") is True
        ]
        witness = {
            "parent_claim_id": parent["claim_id"],
            "contract_version": contract_version,
            "selected_subgraph": selected,
            "selected_subgraph_digest": digest,
            "discharged_obligation_ids": [item["obligation_id"] for item in matching_obligations],
            "open_obligation_ids": [],
            "edge_justifications": [
                {
                    "edge_id": x["edge_id"],
                    "justification_ref": x.get("justification_ref", "整体同行复核"),
                }
                for x in selected_edges
            ],
            "bridge_dependency_ids": [],
            "composition_mode": "PEER",
            "verification_refs": [],
            "human_attestation_review_ids": eligible_review_ids,
        }
        _apply_product_command(
            kernel,
            args.run_id,
            "SubmitClosureWitness",
            witness,
            _managed_role_capability(config, "main", "SubmitClosureWitness", args.run_id),
        )
        return "ClosureWitness 已接受；最终 Claim 的有效依赖闭包已关闭。", 0
    if args.operation == "hint":
        kinds = {
            "换表示": "CHANGE_REPRESENTATION",
            "停止路线": "STOP_ROUTE",
            "优先引理": "PRIORITIZE_LEMMA",
            "修改策略": "CHANGE_STRATEGY",
            "其他": "OTHER",
        }
        cap = _managed_role_capability(config, "main", "RecordResearchHint", args.run_id)
        _apply_product_command(
            kernel,
            args.run_id,
            "RecordResearchHint",
            {
                "contract_version": contract_version,
                "hint_kind": kinds[args.hint_kind],
                "hint": args.hint,
                "checkpoint_label": f"revision-{snapshot.revision}",
            },
            cap,
        )
        return "高层指导已记录；它会影响后续编排，但不会直接写入事实图。", 0
    fact_id = _fact_id_by_label(kernel, args.run_id, args.fact_label)
    if args.operation == "revoke_fact":
        cap = _managed_role_capability(config, "main", "RevokeFact", args.run_id)
        _apply_product_command(
            kernel,
            args.run_id,
            "RevokeFact",
            {"contract_version": contract_version, "fact_id": fact_id, "reason": args.reason},
            cap,
        )
        return "事实及其全部依赖下游已撤销；未受影响的旁支事实保留。", 0
    main = _managed_role_capability(config, "main", "export", args.run_id)
    if args.operation == "candidate_paper":
        if args.output.exists() and not args.force:
            raise FileExistsError("候选论文文件已存在；如确定覆盖，请加 --覆盖")
        candidate = VerifiedPaper().build_candidate(snapshot.projection, fact_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(candidate.tex)
        digest = hashlib.sha256(candidate.tex).hexdigest()
        return (
            f"候选 TeX 已生成：{args.output.resolve()}\nSHA-256：{digest}\n"
            "它尚不是最终论文；须由独立 verifier 对这些确切字节完成整篇复核。",
            0,
        )
    if args.operation == "review_paper":
        candidate = VerifiedPaper().build_candidate(snapshot.projection, fact_id)
        paper_sha256 = hashlib.sha256(candidate.tex).hexdigest()
        attested, resolved = _verify_signed_review(
            config,
            args.review_file,
            {
                "run_id": args.run_id,
                "contract_version": contract_version,
                "final_fact_id": fact_id,
                "paper_sha256": paper_sha256,
            },
        )
        imported = dict(attested["import_fields"])
        data = resolved.read_bytes()
        verifier = _attesting_verifier_capability(
            config, attested, "SubmitEvidence", args.run_id
        )
        artifact = kernel.import_artifact(
            args.run_id,
            ArtifactInput(
                "paperreview" + resolved.suffix.lower(),
                str(resolved),
                hashlib.sha256(data).hexdigest(),
                len(data),
                mimetypes.guess_type(resolved.name)[0] or "text/plain",
            ),
            verifier,
            logical_name=f"paper-review@{snapshot.revision}",
            role="PAPER_REVIEW",
        )
        imported["review_artifact_id"] = artifact.artifact_id
        imported["verifier_attestation"] = {
            "artifact_sha256": artifact.sha256,
            "verifier_identity_id": attested["verifier_identity_id"],
            "verifier_subject_id": attested["verifier_subject_id"],
            "promotion_eligible": attested["promotion_eligible"],
            "authority": attested["authority"],
            "final_fact_id": fact_id,
            "contract_version": contract_version,
            "paper_sha256": paper_sha256,
            "status": imported["status"],
        }
        _apply_product_command(
            kernel,
            args.run_id,
            "RecordPaperReview",
            imported,
            _attesting_verifier_capability(config, attested, "RecordPaperReview", args.run_id),
        )
        return f"独立整篇论文复核已记录：{imported['status']}。复核绑定当前确切 TeX。", 0
    if args.output.exists() and not args.force:
        raise FileExistsError("论文文件已存在；如确定覆盖，请加 --覆盖")
    output_format = "LATEX" if args.paper_format == "tex" else "PDF"
    latest = _run_snapshot(kernel, args.run_id)
    ref = kernel.export(
        ExportRequest(
            str(uuid.uuid4()),
            args.run_id,
            latest.revision,
            frozen_mapping(
                {
                    "format": output_format,
                    "include_raw_artifacts": False,
                    "language": "zh-CN",
                    "final_fact_id": fact_id,
                }
            ),
        ),
        main,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(read_cas_artifact(config.cas_root, ref.sha256, ref.byte_count))
    return f"论文已生成并保存：{args.output.resolve()}", 0


def _run_snapshot(kernel: ResearchKernel, run_id: str, **kwargs: Any) -> RunSnapshot:
    snapshot = kernel.inspect(run_id, **kwargs)
    if not isinstance(snapshot, RunSnapshot):
        raise RuntimeError("当前研究状态无法用于该产品动作")
    return snapshot


def _orchestrator(config: KernelConfig, run_id: str, action: str) -> Any:
    """Late-bind the product workflow so the protocol kernel remains independently usable."""

    try:
        module = importlib.import_module("rk.orchestrator")
        orchestrator_type = module.ResearchOrchestrator
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "研究编排器尚未安装完整；请让管理员检查 RK 的 orchestrator 部署"
        ) from exc
    kernel = ResearchKernel.from_config(config)
    capability = _managed_capability(config, action, run_id)
    environment = _research_environment(config)
    factory = getattr(orchestrator_type, "from_config", None)
    if callable(factory):
        return factory(config=config, kernel=kernel, capability=capability, environment=environment)
    return orchestrator_type(
        config=config, kernel=kernel, capability=capability, environment=environment
    )


def _workflow_status(config: KernelConfig, run_id: str) -> Any:
    """Read orchestration progress when the deployed product adapter exposes it."""

    try:
        module = importlib.import_module("rk.orchestrator")
        kernel = ResearchKernel.from_config(config)
        capability = _managed_product_capability(config, run_id)
        environment = _research_environment(config)
        orchestrator = module.ResearchOrchestrator.from_config(
            config=config,
            kernel=kernel,
            capability=capability,
            environment=environment,
        )
    except (CapabilityError, RuntimeError):
        return None
    status = getattr(orchestrator, "status", None)
    if not callable(status):
        return None
    try:
        return status(run_id)
    except ValueError as exc:
        if "尚无研究编排进度" in str(exc):
            return None
        raise


def _research_environment(config: KernelConfig) -> dict[str, str]:
    names = config.product.get("research_environment_names", ["DEEPSEEK_API_KEY"])
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("product.research_environment_names 必须是环境变量名数组")
    environment = {name: os.environ[name] for name in names if name in os.environ}
    configured = config.product.get("research_environment", {})
    if not isinstance(configured, Mapping) or not all(
        isinstance(name, str) and isinstance(value, str) for name, value in configured.items()
    ):
        raise ValueError("product.research_environment 必须是字符串环境变量表")
    for name, value in configured.items():
        if name.endswith("KEY") or "TOKEN" in name or "SECRET" in name:
            raise ValueError("密钥不得写入配置文件；请使用环境变量")
        environment.setdefault(name, value)
    return environment


def _review_file(path: Path, allowed_roots: tuple[Path, ...]) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise RequestValidationError("审查材料必须是普通文件")
    if not any(resolved.is_relative_to(root.resolve()) for root in allowed_roots):
        raise RequestValidationError("审查材料须放入管理员配置的收件箱目录")
    if resolved.suffix.lower() not in {".md", ".txt", ".tex", ".pdf"}:
        raise RequestValidationError("审查材料仅支持 Markdown、纯文本、TeX 或 PDF")
    if resolved.stat().st_size > 16 * 1024 * 1024:
        raise RequestValidationError("审查材料超过 16 MiB，请拆分后再导入")
    return resolved


def _write_template(path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError("题目文件已存在；如确定覆盖，请加 --覆盖")
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "项目代号": "请填写稳定、简短的代号",
        "题目": r"请写完整题面；可使用 LaTeX，例如：证明 $\sqrt{2}$ 是无理数。",
        "精确否定": r"请写出命题的逐量词否定，例如：存在有理数 $q$ 使 $q^2=2$。",
        "研究对象": [{"名称": "例如：正整数 n", "约束": "例如：n≥1"}],
        "量词": [{"类型": "任意/存在", "变量": "n", "范围": "正整数"}],
        "来源": [],
        "允许依赖": ["标准数学定义"],
        "禁止信息": ["不得把有限搜索无命中当成一般证明"],
        "成功证书": ["严格自然语言证明", "Lean 4 内核重放"],
        "非结论": [],
        "文献范围": {"语言": ["中文", "英文"], "来源类型": ["论文", "专著"]},
        "文献截止日期": "2026-08-12",
        "附件": [],
        "附件说明": "附件路径必须位于配置文件的 inbox_roots 内，避免误读其他文件。",
    }
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _topic_request(
    value: dict[str, Any], source: Path, *, allowed_roots: tuple[Path, ...] = ()
) -> dict[str, Any]:
    required = ("项目代号", "题目", "精确否定", "研究对象", "量词")
    missing = [key for key in required if not value.get(key)]
    if missing:
        raise RequestValidationError(f"题目模板尚未填完：缺少 {', '.join(missing)}")
    attachments = value.get("附件", [])
    if not isinstance(attachments, list):
        raise RequestValidationError("“附件”必须是文件路径数组")
    artifact_inputs = [
        _artifact_input(Path(str(item)), source.parent, allowed_roots) for item in attachments
    ]
    contract = {
        "stable_project_id": str(value["项目代号"]),
        "statement": str(value["题目"]),
        "source_refs": list(value.get("来源", [])),
        "objects": list(value["研究对象"]),
        "definitions": list(value.get("定义", [])),
        "quantifiers": list(value["量词"]),
        "exact_negation": str(value["精确否定"]),
        "allowed_dependencies": list(value.get("允许依赖", [])),
        "forbidden_information": list(value.get("禁止信息", [])),
        "boundary_rules": dict(value.get("边界约定", {})),
        "randomness_rules": dict(value.get("随机性约定", {})),
        "tie_rules": dict(value.get("并列约定", {})),
        "success_certificate_types": list(value.get("成功证书", ["严格自然语言证明"])),
        "non_claims": list(value.get("非结论", [])),
        "literature_scope": dict(value.get("文献范围", {})),
        "literature_cutoff_date": str(value.get("文献截止日期", "2026-08-12")),
        "budget_policy": dict(value.get("预算", {})),
        "stop_rules": list(value.get("停止规则", [{"条件": "预算耗尽", "动作": "保留未解决"}])),
        "semantic_review_policy": dict(value.get("语义复核", {"要求": "检查对象、量词与否定"})),
        "amendment_policy": dict(value.get("修订规则", {"要求": "保留旧版本与影响分析"})),
    }
    return {
        "schema_version": "rk.command.v1",
        "operation": "create",
        "request_id": str(uuid.uuid4()),
        "contract": contract,
        "artifact_inputs": artifact_inputs,
    }


def _artifact_input(path: Path, base: Path, allowed_roots: tuple[Path, ...]) -> dict[str, Any]:
    resolved = path.expanduser()
    if not resolved.is_absolute():
        resolved = base / resolved
    resolved = resolved.resolve(strict=True)
    if not any(resolved.is_relative_to(root.resolve()) for root in allowed_roots):
        raise RequestValidationError(
            "附件不在管理员配置的收件箱目录内；请移动附件，或让管理员调整 inbox_roots"
        )
    data = resolved.read_bytes()
    suffix = resolved.suffix.lower()
    safe_suffix = suffix if suffix and suffix[1:].isalnum() and len(suffix) <= 10 else ".bin"
    digest = hashlib.sha256(data).hexdigest()
    return {
        "name": f"attachment_{digest[:12]}{safe_suffix}",
        "path": str(resolved),
        "sha256": digest,
        "byte_count": len(data),
        "media_type": "text/plain; charset=utf-8",
    }


_ERROR_HELP = {
    "CAPABILITY_DENIED": "请让管理员检查研究服务权限；普通数学家不需要处理权限文件。",
    "RUN_NOT_FOUND": "核对研究编号；可从“提交题目”成功回执中复制编号。",
    "INGEST_SCHEMA_INVALID": "运行 `rkctl 准备题目 新题目.json`，对照模板补齐字段。",
    "IDEMPOTENCY_KEY_REUSED": "不要重复使用旧请求编号；普通中文命令会自动生成新编号。",
    "TEMPORARILY_UNAVAILABLE": "稍后重试；若持续发生，请检查数据库文件权限和磁盘空间。",
    "INTERNAL_ERROR": "请把这段错误原样交给 RK 管理员排查。",
}


def main(argv: list[str] | None = None) -> int:
    audit_json = "--audit-json" in (argv or sys.argv[1:]) or "--审计JSON" in (argv or sys.argv[1:])
    try:
        args = _parser().parse_args(argv)
        value, exit_code = _run(args)
        if isinstance(value, dict):
            _write_json(value)
        else:
            _write_text(value)
        return exit_code
    except (
        RequestValidationError,
        DuplicateJsonKey,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        return _emit_error("INGEST_SCHEMA_INVALID", str(exc), audit_json, 2)
    except CapabilityError as exc:
        return _emit_error("CAPABILITY_DENIED", str(exc), audit_json, 5)
    except StorageConflict as exc:
        return _emit_error("IDEMPOTENCY_KEY_REUSED", str(exc), audit_json, 3)
    except RunNotFound:
        return _emit_error("RUN_NOT_FOUND", "没有找到这个研究编号", audit_json, 3)
    except sqlite3.OperationalError as exc:
        return _emit_error("TEMPORARILY_UNAVAILABLE", str(exc), audit_json, 6)
    except (KernelError, OSError, RuntimeError, ValueError, KeyError) as exc:
        return _emit_error("INTERNAL_ERROR", str(exc), audit_json, 7)


def _emit_error(code: str, message: str, audit_json: bool, exit_code: int) -> int:
    next_step = _ERROR_HELP[code]
    if audit_json:
        _write_json(_problem(code, message, next_step))
    else:
        _write_text(f"操作未完成：{message}\n建议：{next_step}\n错误代码：{code}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
