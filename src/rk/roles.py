# ruff: noqa: RUF001
"""Versioned, soft-authority role protocols for mathematical sub-agents.

The kernel owns mathematical state.  This module only prepares deterministic prompts and
declares what an agent may read and emit; no role output is promoted by this module.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

SOFT_AUTHORITY = "SOFT_CANDIDATE_ONLY"


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


class MathRole(StrEnum):
    CONTRACT_CLARIFIER = "CONTRACT_CLARIFIER"
    ROUTE_SCOUT = "ROUTE_SCOUT"
    PROOF_COUNTEREXAMPLE = "PROOF_COUNTEREXAMPLE"
    LEAN_FORMALIZER = "LEAN_FORMALIZER"
    ANONYMOUS_GAP_REVIEWER = "ANONYMOUS_GAP_REVIEWER"
    TARGETED_REVISER = "TARGETED_REVISER"
    SEMANTIC_FIDELITY_AUDITOR = "SEMANTIC_FIDELITY_AUDITOR"
    LITERATURE_NOVELTY_AUDITOR = "LITERATURE_NOVELTY_AUDITOR"
    FINAL_SYNTHESIZER = "FINAL_SYNTHESIZER"


@dataclass(frozen=True, slots=True)
class RoleBudget:
    max_work_units: int
    max_output_tokens: int
    max_wall_seconds: int

    def __post_init__(self) -> None:
        if min(self.max_work_units, self.max_output_tokens, self.max_wall_seconds) <= 0:
            raise ValueError("role budgets must be positive")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_work_units": self.max_work_units,
            "max_output_tokens": self.max_output_tokens,
            "max_wall_seconds": self.max_wall_seconds,
        }


@dataclass(frozen=True, slots=True)
class RoleSpec:
    role: MathRole
    role_id: str
    version: int
    title_zh: str
    purpose: str
    required_input_types: tuple[str, ...]
    allowed_input_types: tuple[str, ...]
    payload_required_fields: tuple[str, ...]
    role_rules: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    ambiguity_policy: str
    budget: RoleBudget
    system_prompt: str
    prompt_sha256: str
    authority_ceiling: str = SOFT_AUTHORITY

    def to_manifest(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "role_id": self.role_id,
            "version": self.version,
            "title_zh": self.title_zh,
            "purpose": self.purpose,
            "required_input_types": list(self.required_input_types),
            "allowed_input_types": list(self.allowed_input_types),
            "output_schema": _output_schema(self),
            "stop_conditions": list(self.stop_conditions),
            "ambiguity_policy": self.ambiguity_policy,
            "budget": self.budget.to_dict(),
            "authority_ceiling": self.authority_ceiling,
            "prompt_sha256": self.prompt_sha256,
        }


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    contract_id: str
    contract_version: int
    contract_hash: str
    claim_id: str
    statement_hash: str
    input_artifacts: Mapping[str, tuple[str, ...]]
    task: str
    budget: RoleBudget | None = None

    def __post_init__(self) -> None:
        if not self.contract_id or not self.claim_id or not self.task.strip():
            raise ValueError("contract_id, claim_id and task are required")
        if self.contract_version <= 0:
            raise ValueError("contract_version must be positive")
        for label, digest in (
            ("contract_hash", self.contract_hash),
            ("statement_hash", self.statement_hash),
        ):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        normalized = {
            str(kind): tuple(str(artifact_id) for artifact_id in artifact_ids)
            for kind, artifact_ids in self.input_artifacts.items()
        }
        object.__setattr__(self, "input_artifacts", MappingProxyType(normalized))


@dataclass(frozen=True, slots=True)
class BoundRolePrompt:
    role_id: str
    prompt_sha256: str
    assignment_digest: str
    authority_ceiling: str
    system_prompt: str
    assignment_prompt: str
    output_schema: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "prompt_sha256": self.prompt_sha256,
            "assignment_digest": self.assignment_digest,
            "authority_ceiling": self.authority_ceiling,
            "system_prompt": self.system_prompt,
            "assignment_prompt": self.assignment_prompt,
            "output_schema": dict(self.output_schema),
        }


_UNIVERSAL_RULES = (
    "冻结合同、claim、定义、前提、量词、方向和边界均不可修改、弱化或重新解释。",
    "发现歧义时输出 AMBIGUOUS 与精确选项并停止；不得自行选择最容易证明的解释。",
    "只能读取任务列出的输入工件，不得把记忆、模型自信或其他角色投票当作证据。",
    "必须区分命题为假、证明失败、环境失败、未找到和有限无命中。",
    "所有输出仅为 SOFT_CANDIDATE_ONLY；不得宣称改变 RK 的机器、同行或结案状态。",
    "达到预算、命中停止条件或连续一个工作单元没有新可检查对象时立即停止并留账。",
    "不得伪造引用、Lean/工具执行、哈希、token、计时、独立性或审查身份。",
)


def _build_system_prompt(
    *,
    role_id: str,
    title: str,
    purpose: str,
    role_rules: tuple[str, ...],
    stop_conditions: tuple[str, ...],
    ambiguity_policy: str,
    budget: RoleBudget,
) -> str:
    rules = "\n".join(f"{index}. {rule}" for index, rule in enumerate(_UNIVERSAL_RULES, 1))
    offset = len(_UNIVERSAL_RULES) + 1
    specific = "\n".join(f"{index}. {rule}" for index, rule in enumerate(role_rules, offset))
    stops = "\n".join(f"- {condition}" for condition in stop_conditions)
    return (
        f"你是 RK 数学子智能体：{title}。\n"
        f"ROLE_ID: {role_id}\n"
        f"AUTHORITY_CEILING: {SOFT_AUTHORITY}\n"
        f"职责：{purpose}\n\n"
        "强制规则：\n"
        f"{rules}\n{specific}\n\n"
        f"歧义处理：{ambiguity_policy}\n"
        "默认预算："
        f"{budget.max_work_units} 个工作单元，最多 {budget.max_output_tokens} 输出 token，"
        f"最多 {budget.max_wall_seconds} 秒。\n"
        "停止条件：\n"
        f"{stops}\n\n"
        "输出必须是调用消息给定 schema 的单个 JSON 对象。若调用消息包含已绑定的"
        "contract/claim scope、PROMPT_SHA256 和 assignment_digest，则必须原样回显；"
        "未提供时不得自行猜测或生成这些字段。"
    )


def _make_spec(
    role: MathRole,
    title: str,
    purpose: str,
    required_inputs: tuple[str, ...],
    allowed_inputs: tuple[str, ...],
    payload_fields: tuple[str, ...],
    role_rules: tuple[str, ...],
    stop_conditions: tuple[str, ...],
    ambiguity_policy: str,
    budget: RoleBudget,
) -> RoleSpec:
    role_id = f"rk.math.{role.value.lower()}.v1"
    prompt = _build_system_prompt(
        role_id=role_id,
        title=title,
        purpose=purpose,
        role_rules=role_rules,
        stop_conditions=stop_conditions,
        ambiguity_policy=ambiguity_policy,
        budget=budget,
    )
    return RoleSpec(
        role=role,
        role_id=role_id,
        version=1,
        title_zh=title,
        purpose=purpose,
        required_input_types=required_inputs,
        allowed_input_types=allowed_inputs,
        payload_required_fields=payload_fields,
        role_rules=role_rules,
        stop_conditions=stop_conditions,
        ambiguity_policy=ambiguity_policy,
        budget=budget,
        system_prompt=prompt,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    )


_SPECS = (
    _make_spec(
        MathRole.CONTRACT_CLARIFIER,
        "契约澄清者",
        "只把用户题意变成可证伪的冻结候选，不承担证明或冻结权限。",
        ("PROBLEM_STATEMENT",),
        ("PROBLEM_STATEMENT", "SOURCE_CONTEXT", "DEFINITION_LEDGER"),
        ("normalized_statement", "quantifiers", "ambiguities", "contract_proposal"),
        ("逐项列对象、定义域、量词、边界和精确否定。", "不得证明，不得自行冻结合同。"),
        ("题意有会改变真值的歧义", "合同候选字段完整", "预算耗尽"),
        "列出互斥解释及其数学影响，返回 AMBIGUOUS，交还合同所有者。",
        RoleBudget(1, 4_000, 600),
    ),
    _make_spec(
        MathRole.ROUTE_SCOUT,
        "路线侦察者",
        "提出少量结构不同、能快速证伪的路线，不把路线当证明。",
        ("FROZEN_CONTRACT", "CLAIM_STATEMENT"),
        ("FROZEN_CONTRACT", "CLAIM_STATEMENT", "LOCAL_TOOLBOX", "HAZARD_LEDGER"),
        ("routes", "shared_blockers", "fast_falsifiers"),
        ("路线按数学结构去重。", "每条路线写关键引理、闭合链和最快证伪测试。"),
        ("产生三至五条高差异路线", "路线塌缩到同一等价难题", "预算耗尽"),
        "若缺少定义或工具排除表会改变路线空间，返回 AMBIGUOUS，不补猜。",
        RoleBudget(1, 5_000, 900),
    ),
    _make_spec(
        MathRole.PROOF_COUNTEREXAMPLE,
        "证明与反例工作者",
        "在指定路线内先攻击边界，再给完整证明、显式反例或最小断点。",
        ("FROZEN_CONTRACT", "CLAIM_STATEMENT", "ROUTE_CARD"),
        (
            "FROZEN_CONTRACT",
            "CLAIM_STATEMENT",
            "ROUTE_CARD",
            "DEFINITION_LEDGER",
            "KNOWN_LEMMA_LEDGER",
        ),
        ("outcome", "derivation", "assumption_audit", "open_obligations"),
        ("先执行路线的最快证伪测试。", "外部或新引理标明已知、已证、开放、等价或更强。"),
        ("得到完整证明", "得到满足全部前提的反例", "定位最小未闭合义务", "预算耗尽"),
        "缺少前提只报告缺口；不得把缺失前提写回冻结命题。",
        RoleBudget(2, 12_000, 3_600),
    ),
    _make_spec(
        MathRole.LEAN_FORMALIZER,
        "Lean 形式化者",
        "把固定 claim 编码为 Lean 候选并报告编译反馈，不给自己机器认证。",
        ("FROZEN_CONTRACT", "CLAIM_STATEMENT"),
        (
            "FROZEN_CONTRACT",
            "CLAIM_STATEMENT",
            "NATURAL_LANGUAGE_PROOF",
            "LEAN_CONTEXT",
            "PREMISE_CANDIDATES",
        ),
        ("lean_statement", "lean_candidate", "translation_map", "verification_requests"),
        (
            "逐字段对齐自然语言与 Lean 陈述。",
            "不得使用 sorry、admit、新 axiom、unsafe、native_decide 或自报 replay。",
        ),
        ("候选已提交独立 replay", "发现语义漂移", "工具或环境失败", "预算耗尽"),
        "无法唯一编码时输出候选编码差异并请求语义审计，不擅自挑选。",
        RoleBudget(2, 10_000, 3_600),
    ),
    _make_spec(
        MathRole.ANONYMOUS_GAP_REVIEWER,
        "匿名找漏洞者",
        "在不知道作者与期望答案的情况下，只裁检查到的关键缺口。",
        ("FROZEN_CONTRACT", "CLAIM_STATEMENT", "ANONYMOUS_PROOF"),
        (
            "FROZEN_CONTRACT",
            "CLAIM_STATEMENT",
            "ANONYMOUS_PROOF",
            "HAZARD_LEDGER",
            "KNOWN_LEMMA_LEDGER",
        ),
        ("review_status", "critical_gaps", "scope_audit"),
        ("不得搜索作者或出处。", "不得修证明；接受必须说明承重链如何逐项闭合。"),
        ("确认无关键缺口", "定位首个关键缺口或反例", "预算耗尽"),
        "证明中的歧义按未证明处理并定位原文，不替作者作有利解释。",
        RoleBudget(1, 6_000, 1_800),
    ),
    _make_spec(
        MathRole.TARGETED_REVISER,
        "定向修订者",
        "只处理已定位缺口，产出自包含新稿、反例或未完成报告。",
        ("FROZEN_CONTRACT", "CLAIM_STATEMENT", "PROOF_DRAFT", "GAP_REPORT"),
        (
            "FROZEN_CONTRACT",
            "CLAIM_STATEMENT",
            "PROOF_DRAFT",
            "GAP_REPORT",
            "KNOWN_LEMMA_LEDGER",
        ),
        ("revision_outcome", "revised_proof", "gap_resolutions", "remaining_gaps"),
        ("逐条对应缺口。", "不得用新增前提修补冻结命题。"),
        ("全部缺口闭合", "发现反例", "同一缺口第二次仍未闭合", "预算耗尽"),
        "若修补要求新前提，返回 INCOMPLETE 并交还合同所有者。",
        RoleBudget(1, 10_000, 2_400),
    ),
    _make_spec(
        MathRole.SEMANTIC_FIDELITY_AUDITOR,
        "语义忠实审计者",
        "比较原题、冻结合同、形式陈述和回译，定位首个语义漂移。",
        ("PROBLEM_STATEMENT", "FROZEN_CONTRACT", "FORMAL_STATEMENT"),
        (
            "PROBLEM_STATEMENT",
            "SOURCE_CONTEXT",
            "FROZEN_CONTRACT",
            "FORMAL_STATEMENT",
            "BACK_TRANSLATION",
            "MUTATION_TEST_RESULTS",
        ),
        ("fidelity_status", "field_alignment", "semantic_drifts", "mutation_tests"),
        ("逐字段检查量词、方向、域、非退化条件和隐藏类型类。", "内核通过不等于语义忠实。"),
        ("完成逐字段双向映射", "定位首个语义漂移", "预算耗尽"),
        "任何会改变真假或适用域的不唯一映射都返回 AMBIGUOUS。",
        RoleBudget(1, 6_000, 1_800),
    ),
    _make_spec(
        MathRole.LITERATURE_NOVELTY_AUDITOR,
        "文献与新颖性审计者",
        "核验原始来源、开放状态、相邻定理和贡献占位，不负责证明。",
        ("FROZEN_CONTRACT", "CLAIM_STATEMENT"),
        (
            "FROZEN_CONTRACT",
            "CLAIM_STATEMENT",
            "SOURCE_CONTEXT",
            "LITERATURE_CANDIDATES",
            "PROOF_SUMMARY",
        ),
        ("status", "sources", "implication_map", "novelty_assessment"),
        ("优先原论文、作者页面和正式数据库。", "找不到写未确认，不得写没有先例。"),
        ("状态与占位已由原始来源支持", "状态仍不确定", "发现更强已知结果", "预算耗尽"),
        "来源冲突时保留双方与日期，状态置 STATUS_UNCERTAIN。",
        RoleBudget(1, 6_000, 1_800),
    ),
    _make_spec(
        MathRole.FINAL_SYNTHESIZER,
        "最终综合者",
        "把已冻结证据投影为可读结论，分开真假、证明完整性和新颖性。",
        ("FROZEN_CONTRACT", "CLAIM_STATEMENT", "EVIDENCE_LEDGER"),
        (
            "FROZEN_CONTRACT",
            "CLAIM_STATEMENT",
            "EVIDENCE_LEDGER",
            "GAP_REPORT",
            "SEMANTIC_AUDIT",
            "NOVELTY_AUDIT",
            "MACHINE_REPLAY_REPORT",
        ),
        ("truth_status", "proof_status", "novelty_status", "evidence_map", "remaining_risks"),
        ("只综合已列工件，不补写新证明。", "冲突证据并列展示，不以多数票消解。"),
        ("三个结论维度及证据路径齐全", "发现承重冲突或缺失", "预算耗尽"),
        "任何承重证据冲突都输出 UNRESOLVED 并列明需要谁裁决。",
        RoleBudget(1, 6_000, 1_200),
    ),
)

_SPEC_BY_ROLE = {spec.role: spec for spec in _SPECS}


def role_catalog() -> tuple[RoleSpec, ...]:
    """Return the immutable v1 role catalog in workflow order."""

    return _SPECS


def get_role_spec(role: MathRole | str) -> RoleSpec:
    try:
        normalized = role if isinstance(role, MathRole) else MathRole(role)
        return _SPEC_BY_ROLE[normalized]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unknown mathematical role: {role}") from exc


def _output_schema(spec: RoleSpec) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "role_id",
            "prompt_sha256",
            "assignment_digest",
            "authority",
            "status",
            "contract_scope",
            "claim_scope",
            "artifacts_used",
            "open_obligations",
            "payload",
        ],
        "properties": {
            "schema_version": {"const": "rk.role-output.v1"},
            "role_id": {"const": spec.role_id},
            "prompt_sha256": {"const": spec.prompt_sha256},
            "assignment_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "authority": {"const": SOFT_AUTHORITY},
            "status": {
                "enum": [
                    "CANDIDATE",
                    "PROVED_CANDIDATE",
                    "DISPROVED_CANDIDATE",
                    "INCOMPLETE",
                    "AMBIGUOUS",
                    "ENVIRONMENT_FAILURE",
                ]
            },
            "contract_scope": {
                "type": "object",
                "required": ["contract_id", "contract_version", "contract_hash"],
                "additionalProperties": False,
                "properties": {
                    "contract_id": {"type": "string", "minLength": 1},
                    "contract_version": {"type": "integer", "minimum": 1},
                    "contract_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
            },
            "claim_scope": {
                "type": "object",
                "required": ["claim_id", "statement_hash"],
                "additionalProperties": False,
                "properties": {
                    "claim_id": {"type": "string", "minLength": 1},
                    "statement_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
            },
            "artifacts_used": {"type": "array", "items": {"type": "string"}},
            "open_obligations": {"type": "array", "items": {"type": "string"}},
            "payload": {
                "type": "object",
                "required": list(spec.payload_required_fields),
                "additionalProperties": True,
            },
        },
    }


def bind_role_prompt(role: MathRole | str, assignment: RoleAssignment) -> BoundRolePrompt:
    """Bind one exact contract/claim scope and reject undeclared input channels."""

    spec = get_role_spec(role)
    supplied = set(assignment.input_artifacts)
    missing = set(spec.required_input_types) - supplied
    forbidden = supplied - set(spec.allowed_input_types)
    if missing:
        raise ValueError(f"missing required input artifact types: {sorted(missing)}")
    if forbidden:
        raise ValueError(f"input artifact types are not allowed for role: {sorted(forbidden)}")
    if any(not ids for ids in assignment.input_artifacts.values()):
        raise ValueError("every declared input artifact type must contain at least one artifact id")

    budget = assignment.budget or spec.budget
    if (
        budget.max_work_units > spec.budget.max_work_units
        or budget.max_output_tokens > spec.budget.max_output_tokens
        or budget.max_wall_seconds > spec.budget.max_wall_seconds
    ):
        raise ValueError(
            "assignment budget cannot exceed the role default without a new prompt version"
        )
    scope = {
        "contract_id": assignment.contract_id,
        "contract_version": assignment.contract_version,
        "contract_hash": assignment.contract_hash,
        "claim_id": assignment.claim_id,
        "statement_hash": assignment.statement_hash,
        "input_artifacts": {
            kind: list(ids) for kind, ids in sorted(assignment.input_artifacts.items())
        },
        "task": assignment.task,
        "budget": budget.to_dict(),
    }
    assignment_digest = _canonical_sha256(scope)
    output_schema = _output_schema(spec)
    properties = output_schema["properties"]
    properties["assignment_digest"] = {"const": assignment_digest}
    properties["contract_scope"]["properties"] = {
        "contract_id": {"const": assignment.contract_id},
        "contract_version": {"const": assignment.contract_version},
        "contract_hash": {"const": assignment.contract_hash},
    }
    properties["claim_scope"]["properties"] = {
        "claim_id": {"const": assignment.claim_id},
        "statement_hash": {"const": assignment.statement_hash},
    }
    allowed_artifact_ids = sorted(
        artifact_id
        for artifact_ids in assignment.input_artifacts.values()
        for artifact_id in artifact_ids
    )
    properties["artifacts_used"]["items"] = {"enum": allowed_artifact_ids}
    assignment_prompt = (
        "本次分配（这些值是不可变作用域，不是建议）：\n"
        f"{json.dumps(scope, ensure_ascii=False, sort_keys=True, indent=2)}\n\n"
        f"ASSIGNMENT_DIGEST: {assignment_digest}\n"
        f"PROMPT_SHA256: {spec.prompt_sha256}\n"
        "输出 JSON Schema：\n"
        f"{json.dumps(output_schema, ensure_ascii=False, sort_keys=True, indent=2)}"
    )
    return BoundRolePrompt(
        role_id=spec.role_id,
        prompt_sha256=spec.prompt_sha256,
        assignment_digest=assignment_digest,
        authority_ceiling=spec.authority_ceiling,
        system_prompt=spec.system_prompt,
        assignment_prompt=assignment_prompt,
        output_schema=output_schema,
    )


class CapabilityLayer(StrEnum):
    KERNEL = "RK_KERNEL"
    ADAPTER = "REGISTERED_ADAPTER"
    SCRIPT_BYPASS = "EVALUATION_SCRIPT_BYPASS"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class MathCapability:
    capability_id: str
    layer: CapabilityLayer
    maturity: str
    authority_ceiling: str
    function: str
    evidence_path: str


_CAPABILITIES = (
    MathCapability(
        "contract-and-claim-state",
        CapabilityLayer.KERNEL,
        "IMPLEMENTED",
        "STATE_ONLY",
        "冻结合同、claim DAG、路线、义务与失效传播",
        "src/rk/guard.py",
    ),
    MathCapability(
        "evidence-independence-budget",
        CapabilityLayer.KERNEL,
        "IMPLEMENTED_FAIL_CLOSED",
        "STATE_ONLY",
        "证据作用域、独立性、预算、租约和组合闭合",
        "src/rk/kernel.py",
    ),
    MathCapability(
        "leansearch",
        CapabilityLayer.ADAPTER,
        "REMOTE_E2E_UNATTESTED",
        SOFT_AUTHORITY,
        "Mathlib 前提候选检索",
        "src/rk/adapters/leansearch.py",
    ),
    MathCapability(
        "jixia",
        CapabilityLayer.ADAPTER,
        "REMOTE_E2E",
        SOFT_AUTHORITY,
        "Lean 声明、符号、局部状态与依赖抽取",
        "src/rk/adapters/jixia.py",
    ),
    MathCapability(
        "lean-replay",
        CapabilityLayer.ADAPTER,
        "INTEGRATED_AUTHORITY_FAIL_CLOSED",
        "HOST_VERIFIED_ONLY",
        "固定工具链的独立编译与公理审计",
        "src/rk/adapters/lean.py",
    ),
    MathCapability(
        "smt-cas-enumeration",
        CapabilityLayer.ADAPTER,
        "SMOKE_PASS",
        "HEURISTIC_UNTIL_CERTIFICATE_REPLAY",
        "SMT、CAS、精确有限枚举和注册代码",
        "src/rk/adapters/base.py",
    ),
    MathCapability(
        "literature-search",
        CapabilityLayer.ADAPTER,
        "SMOKE_PASS",
        SOFT_AUTHORITY,
        "书目候选检索；NO_HIT 不是无先例",
        "src/rk/adapters/literature.py",
    ),
    MathCapability(
        "opencode-worker",
        CapabilityLayer.ADAPTER,
        "REMOTE_E2E",
        SOFT_AUTHORITY,
        "当前远程模型执行外壳与候选生成",
        "src/rk/adapters/opencode.py",
    ),
    MathCapability(
        "rethlas",
        CapabilityLayer.ADAPTER,
        "ADAPTER_TESTED_FULL_LOOP_FAIL",
        SOFT_AUTHORITY,
        "生成—批评—修订候选循环",
        "src/rk/adapters/rethlas.py",
    ),
    MathCapability(
        "local-proof-model",
        CapabilityLayer.ADAPTER,
        "BENCHMARKED_NOT_DEFAULT",
        SOFT_AUTHORITY,
        "QED-Nano 自然语言候选与 DeepSeek-Prover Lean 候选",
        "src/rk/adapters/local_proof_model.py",
    ),
    MathCapability(
        "model-benchmarks",
        CapabilityLayer.SCRIPT_BYPASS,
        "EVALUATION_ONLY",
        "NONE",
        "上游默认配置的 QED-Nano/DeepSeek-Prover 评测",
        "scripts/rkmodelbench.py",
    ),
    MathCapability(
        "role-prompt-router",
        CapabilityLayer.KERNEL,
        "IMPLEMENTED_SOFT_ONLY",
        SOFT_AUTHORITY,
        "版本化角色、输入边界、输出 schema 与停止条件",
        "src/rk/roles.py",
    ),
    MathCapability(
        "independent-verifier-artifact-import",
        CapabilityLayer.ADAPTER,
        "ADAPTER_TESTED_DEPLOYMENT_IDENTITY_REQUIRED",
        "HUMAN_ATTESTED_IF_SIGNED_AND_ELIGIBLE",
        "验签独立 verifier 的原子 Claim、六部分组合与整篇论文结构化审查产物",
        "src/rk/adapters/attestation.py",
    ),
    MathCapability(
        "human-signature-service",
        CapabilityLayer.MISSING,
        "NOT_IMPLEMENTED",
        "NONE",
        "真实人类身份、签名与独立性证明",
        "docs/implementation-status.md",
    ),
    MathCapability(
        "host-execution-receipt-service",
        CapabilityLayer.KERNEL,
        "IMPLEMENTED_LEAN_ROOT_ONLY",
        "KERNEL_VERIFIED",
        "从当前 canonical ROOT、实际进程与工件重算回执；一次消费后授予 Lean 机器轴",
        "src/rk/host_execution.py",
    ),
)


def math_capability_matrix() -> tuple[MathCapability, ...]:
    """Return a truthful inventory; adapters and benchmark bypasses remain distinguishable."""

    return _CAPABILITIES
