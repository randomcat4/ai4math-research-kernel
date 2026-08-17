"""Mathematician-facing one-run research workflow.

This is product orchestration, not a mathematical authority path. It turns a submitted problem
into a running research record, premise search, and a readable model candidate.
"""
# ruff: noqa: RUF001 -- Chinese product text intentionally uses Chinese punctuation.

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rk.adapters import (
    AdapterProfile,
    CurlHttpClient,
    LeanSearchAdapter,
    OpenAICompatibleAdapter,
)
from rk.config import KernelConfig
from rk.domain import ApplyRequest, ArtifactInput, RunSnapshot, TypedCommand, VerifiedCapability
from rk.kernel import ResearchKernel


class ResearchWorkflowError(RuntimeError):
    """The product workflow cannot make useful progress with the current deployment."""


def run_research(
    *,
    kernel: ResearchKernel,
    config: KernelConfig,
    run_id: str,
    capability: VerifiedCapability,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Start one general research route and persist its candidate report."""

    snapshot = _snapshot(kernel, run_id)
    contract_record = snapshot.projection.get("contract")
    if not isinstance(contract_record, Mapping):
        raise ResearchWorkflowError("研究题目不存在")
    contract = contract_record.get("contract")
    if not isinstance(contract, Mapping):
        raise ResearchWorkflowError("研究题目内容无法读取")
    contract_artifact_id = str(contract_record.get("contract_artifact_id"))
    statement_hash = str(contract_record.get("statement_hash"))

    if not snapshot.projection.get("root_claim_id"):
        _apply(
            kernel,
            capability,
            run_id,
            "RegisterClaim",
            {
                "contract_version": snapshot.current_contract_version,
                "claim_kind": "ROOT",
                "stable_label": "root",
                "statement_artifact_id": contract_artifact_id,
                "statement_hash": statement_hash,
                "normalized_statement": dict(contract),
            },
        )
        snapshot = _snapshot(kernel, run_id)

    if str(contract_record.get("status")) == "DRAFT":
        _apply(
            kernel,
            capability,
            run_id,
            "FreezeContract",
            {
                "contract_version": snapshot.current_contract_version,
                "completeness_check_artifact_id": contract_artifact_id,
            },
        )
        snapshot = _snapshot(kernel, run_id)

    if snapshot.status == "OPEN":
        configured_budget = contract.get("budget_policy")
        if not isinstance(configured_budget, Mapping) or not configured_budget.get("global"):
            configured_budget = {
                "global": {"INPUT_TOKEN": 100_000, "OUTPUT_TOKEN": 20_000}
            }
        _apply(
            kernel,
            capability,
            run_id,
            "StartRun",
            {
                "contract_version": snapshot.current_contract_version,
                "literature_plan_artifact_id": contract_artifact_id,
                "budget_policy": dict(configured_budget),
            },
        )
        snapshot = _snapshot(kernel, run_id)
    if snapshot.status != "RUNNING":
        raise ResearchWorkflowError(f"研究当前状态为 {snapshot.status}，不能开始新路线")

    root_claim_id = str(snapshot.projection.get("root_claim_id"))
    routes = snapshot.projection.get("routes", [])
    route = next(
        (
            item
            for item in routes
            if isinstance(item, Mapping) and item.get("status") in {"SCOUT", "ACTIVE"}
        ),
        None,
    )
    if route is None:
        _apply(
            kernel,
            capability,
            run_id,
            "RegisterRoute",
            {
                "contract_version": snapshot.current_contract_version,
                "target_claim_id": root_claim_id,
                "label": "general-research",
                "representation": "natural-language mathematical argument",
                "tool_family": "premise-search-and-model",
                "approach_root": {"method": "search, attempt, criticize, report gaps"},
                "budget_policy": {"attempts": 3},
            },
        )

    product = config.product
    search_profile_raw = config.adapter_profiles.get("research-search")
    search_summary: dict[str, Any]
    search_text = "未配置前提检索。"
    if isinstance(search_profile_raw, Mapping):
        started = time.monotonic()
        search = LeanSearchAdapter(
            AdapterProfile.from_mapping(search_profile_raw),
            client=CurlHttpClient() if product.get("http_client") == "curl" else None,
        ).run(
            {
                "query": [str(contract.get("statement", ""))],
                "num_results": int(product.get("search_results", 8)),
                "rerank": bool(product.get("search_rerank", True)),
                "retrieve_k": int(product.get("search_retrieve_k", 50)),
            }
        )
        elapsed = int((time.monotonic() - started) * 1000)
        payload = search.get("payload")
        search_summary = {"status": search.get("status"), "wall_time_ms": elapsed}
        search_text = json.dumps(payload, ensure_ascii=False)[:20_000]
    else:
        search_summary = {"status": "NOT_CONFIGURED", "wall_time_ms": 0}

    model_profile_raw = config.adapter_profiles.get("research-model")
    if not isinstance(model_profile_raw, Mapping):
        raise ResearchWorkflowError(
            "管理员尚未配置研究模型；请在 adapter_profiles.research-model 中设置模型入口"
        )
    model_name = product.get("model")
    if not isinstance(model_name, str) or not model_name:
        raise ResearchWorkflowError("管理员尚未在 product.model 中设置研究模型")
    prompt = _research_prompt(contract, search_text)
    started = time.monotonic()
    model = OpenAICompatibleAdapter(
        AdapterProfile.from_mapping(model_profile_raw),
        client=CurlHttpClient() if product.get("http_client") == "curl" else None,
    ).run(
        {
            "prompt": prompt,
            "model": model_name,
            "max_tokens": int(product.get("model_max_tokens", 8192)),
            "environment": {
                "DEEPSEEK_API_KEY": environment.get("DEEPSEEK_API_KEY", "")
            },
        }
    )
    model_elapsed = int((time.monotonic() - started) * 1000)
    if model.get("status") != "COMPLETED":
        raise ResearchWorkflowError(f"研究模型调用失败：{model.get('status')}")
    payload = model.get("payload")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("text"), str):
        raise ResearchWorkflowError("研究模型没有返回可读研究稿")
    candidate = str(payload["text"])
    raw_usage = model.get("usage")
    usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}

    output_dir = config.inbox_roots[0] / "research" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = output_dir / "candidate.md"
    progress_path = output_dir / "progress.json"
    candidate_path.write_text(candidate + "\n", encoding="utf-8")
    progress = {
        "schema_version": "rk.research_progress.v1",
        "stage": "CANDIDATE_READY",
        "conclusion": "待独立验证",
        "search": search_summary,
        "model": {
            "name": model_name,
            "status": "COMPLETED",
            "wall_time_ms": model_elapsed,
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "reasoning_tokens": int(usage.get("reasoning_tokens", 0)),
        },
        "candidate_file": str(candidate_path),
        "next_step": "审阅研究稿；需要机器证明时补充 Lean 形式化后运行验证。",
    }
    progress_path.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _apply(
        kernel,
        capability,
        run_id,
        "SubmitEvidence",
        {
            "claim_id": root_claim_id,
            "contract_version": snapshot.current_contract_version,
            "statement_hash": statement_hash,
            "evidence_type": "NATURAL_LANGUAGE_PROOF",
            "evidence_strength": "SOFT_MODEL",
            "artifact_input_names": ["candidate.md", "progress.json"],
            "scope": {
                "claim_id": root_claim_id,
                "contract_version": snapshot.current_contract_version,
                "statement_hash": statement_hash,
            },
            "provenance": {"actor": "research-model", "model": model_name},
            "evidence_root": {
                "root_kind": "MODEL",
                "origin_artifact_input_name": "candidate.md",
                "source_graph": {"premise_search": search_summary["status"]},
            },
        },
        (_artifact(candidate_path, "candidate.md", "text/markdown"),
         _artifact(progress_path, "progress.json", "application/json")),
    )
    latest = _snapshot(kernel, run_id)
    return {**progress, "run_id": run_id, "revision": latest.revision}


def _research_prompt(contract: Mapping[str, Any], search_text: str) -> str:
    return f"""你是数学研究助手。请针对下列冻结题目产出一份可审查的研究稿。

题目：{contract.get('statement')}
精确否定：{contract.get('exact_negation')}
对象：{json.dumps(contract.get('objects'), ensure_ascii=False)}
量词：{json.dumps(contract.get('quantifiers'), ensure_ascii=False)}
允许依赖：{json.dumps(contract.get('allowed_dependencies'), ensure_ascii=False)}

检索候选：
{search_text}

要求：先尝试找反例，再给2至3条不同路线；选择最好的一条写出逐步论证；明确指出仍未证明的
承重步骤、隐藏假设和下一项最有价值的验证。不得把检索命中、有限计算或自己的自信当成证明。
使用中文和必要的 LaTeX。"""


def _apply(
    kernel: ResearchKernel,
    capability: VerifiedCapability,
    run_id: str,
    command_type: str,
    payload: Mapping[str, Any],
    artifacts: tuple[ArtifactInput, ...] = (),
) -> None:
    snapshot = _snapshot(kernel, run_id)
    receipt = kernel.apply(
        ApplyRequest(
            request_id=str(uuid.uuid4()),
            run_id=run_id,
            expected_revision=snapshot.revision,
            command=TypedCommand(command_type, dict(payload)),
            artifact_inputs=artifacts,
        ),
        capability,
    )
    if not receipt.accepted:
        raise ResearchWorkflowError(
            f"{command_type} 未完成：{receipt.rejection_code or '未知原因'}"
        )


def _snapshot(kernel: ResearchKernel, run_id: str) -> RunSnapshot:
    snapshot = kernel.inspect(run_id)
    if not isinstance(snapshot, RunSnapshot):
        raise ResearchWorkflowError("无法读取当前研究状态")
    return snapshot


def _artifact(path: Path, name: str, media_type: str) -> ArtifactInput:
    data = path.read_bytes()
    return ArtifactInput(name, str(path), hashlib.sha256(data).hexdigest(), len(data), media_type)
