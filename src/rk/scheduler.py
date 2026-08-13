# ruff: noqa: RUF001
"""Loss-aware hardware scheduling for RK candidate generation and verification.

Scheduling records intent and placement.  It cannot turn an unavailable quality requirement into
an allegedly lossless fallback, and it never grants mathematical authority.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import platform
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

UNKNOWN_DIGEST = "UNKNOWN"

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class HardwareMode(StrEnum):
    AUTO = "AUTO"
    PORTABLE_CPU_API = "PORTABLE_CPU_API"
    GAMING_SERIAL_8_12 = "GAMING_SERIAL_8_12"
    PROSUMER_SERIAL_16_24 = "PROSUMER_SERIAL_16_24"
    SERVER48_BATCHED = "SERVER48_BATCHED"
    MULTI_GPU_SPLIT = "MULTI_GPU_SPLIT"


class RetrievalRequirement(StrEnum):
    BASELINE_RETRIEVER = "BASELINE_RETRIEVER"
    RERANK_REQUIRED = "RERANK_REQUIRED"


class CandidateRequirement(StrEnum):
    ANY = "ANY"
    LOCAL_REQUIRED = "LOCAL_REQUIRED"


class IdentityStatus(StrEnum):
    VERIFIED = "VERIFIED"
    DECLARED = "DECLARED"
    UNKNOWN = "UNKNOWN"


class ComponentKind(StrEnum):
    MODEL = "MODEL"
    RETRIEVAL = "RETRIEVAL"
    VERIFIER = "VERIFIER"


@dataclass(frozen=True, slots=True)
class ComponentIdentity:
    kind: ComponentKind
    component_id: str
    digest: str
    status: IdentityStatus
    source: str

    def __post_init__(self) -> None:
        if not self.component_id or not self.source:
            raise ValueError("component identity and source are required")
        if self.digest != UNKNOWN_DIGEST and (
            len(self.digest) != 64
            or any(character not in "0123456789abcdef" for character in self.digest)
        ):
            raise ValueError("component digest must be UNKNOWN or a lowercase SHA-256")
        if self.status is IdentityStatus.VERIFIED and self.digest == UNKNOWN_DIGEST:
            raise ValueError("a verified component must have a digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "component_id": self.component_id,
            "digest": self.digest,
            "status": self.status.value,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class HardwareInventory:
    os_family: str
    system_memory_gb: int
    gpu_vram_gb: tuple[int, ...] = ()
    apple_unified_memory: bool = False
    api_candidate_available: bool = False
    public_retrieval_available: bool = False
    local_assets: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.system_memory_gb <= 0 or any(value <= 0 for value in self.gpu_vram_gb):
            raise ValueError("memory values must be positive")
        if self.apple_unified_memory and self.os_family.lower() != "macos":
            raise ValueError("apple_unified_memory is only valid on macOS")


@dataclass(frozen=True, slots=True)
class ScheduleRequest:
    requested_mode: HardwareMode = HardwareMode.AUTO
    retrieval_requirement: RetrievalRequirement = RetrievalRequirement.BASELINE_RETRIEVER
    candidate_requirement: CandidateRequirement = CandidateRequirement.ANY
    retrieval_top_k: int = 8
    require_jixia: bool = True
    require_final_replay: bool = True
    allow_explicit_mode_fallback: bool = False

    def __post_init__(self) -> None:
        if self.retrieval_top_k <= 0:
            raise ValueError("retrieval_top_k must be positive")
        if not self.require_final_replay:
            raise ValueError("RK scheduling cannot disable final independent replay")


@dataclass(frozen=True, slots=True)
class ScheduledStep:
    component: str
    placement: str
    concurrency_group: str
    quality_contract: str

    def to_dict(self) -> dict[str, str]:
        return {
            "component": self.component,
            "placement": self.placement,
            "concurrency_group": self.concurrency_group,
            "quality_contract": self.quality_contract,
        }


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    requested_mode: HardwareMode
    executed_mode: HardwareMode
    hardware_summary: str
    fallback_reasons: tuple[str, ...]
    steps: tuple[ScheduledStep, ...]
    identities: tuple[ComponentIdentity, ...]
    identity_gaps: tuple[str, ...]
    retrieval_top_k: int
    final_replay_required: bool
    lossless_conditions: tuple[str, ...]
    forbidden_shortcuts: tuple[str, ...]
    plan_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rk.schedule.v1",
            "requested_mode": self.requested_mode.value,
            "executed_mode": self.executed_mode.value,
            "hardware_summary": self.hardware_summary,
            "fallback_reasons": list(self.fallback_reasons),
            "steps": [step.to_dict() for step in self.steps],
            "identities": [identity.to_dict() for identity in self.identities],
            "identity_gaps": list(self.identity_gaps),
            "retrieval_top_k": self.retrieval_top_k,
            "final_replay_required": self.final_replay_required,
            "lossless_conditions": list(self.lossless_conditions),
            "forbidden_shortcuts": list(self.forbidden_shortcuts),
            "plan_digest": self.plan_digest,
        }


class UnschedulablePlan(ValueError):
    """Raised when requested quality cannot be preserved by available hardware and services."""


_MODE_MINIMUMS = {
    HardwareMode.PORTABLE_CPU_API: (0, 0),
    HardwareMode.GAMING_SERIAL_8_12: (1, 8),
    HardwareMode.PROSUMER_SERIAL_16_24: (1, 16),
    HardwareMode.SERVER48_BATCHED: (1, 40),
    HardwareMode.MULTI_GPU_SPLIT: (2, 16),
}


def classify_hardware(inventory: HardwareInventory) -> HardwareMode:
    """Classify conservatively; macOS uses the portable path even with unified memory."""

    if inventory.os_family.lower() == "macos" or not inventory.gpu_vram_gb:
        return HardwareMode.PORTABLE_CPU_API
    if len(inventory.gpu_vram_gb) >= 2 and min(inventory.gpu_vram_gb[:2]) >= 16:
        return HardwareMode.MULTI_GPU_SPLIT
    largest = max(inventory.gpu_vram_gb)
    if largest >= 40:
        return HardwareMode.SERVER48_BATCHED
    if largest >= 16:
        return HardwareMode.PROSUMER_SERIAL_16_24
    if largest >= 8:
        return HardwareMode.GAMING_SERIAL_8_12
    return HardwareMode.PORTABLE_CPU_API


def _supports(mode: HardwareMode, inventory: HardwareInventory) -> bool:
    if mode is HardwareMode.AUTO:
        return True
    gpu_count, minimum_vram = _MODE_MINIMUMS[mode]
    if gpu_count == 0:
        return True
    eligible = [value for value in inventory.gpu_vram_gb if value >= minimum_vram]
    return len(eligible) >= gpu_count


def _candidate_step(
    mode: HardwareMode, inventory: HardwareInventory, requirement: CandidateRequirement
) -> ScheduledStep:
    has_qed = "qed-nano" in inventory.local_assets
    has_dsp = "deepseek-prover-v2-7b" in inventory.local_assets
    if mode is HardwareMode.GAMING_SERIAL_8_12 and has_qed:
        return ScheduledStep(
            "QED-Nano candidate",
            "GPU 0, one job at a time",
            "gpu-serial",
            "SOFT candidate; exact token cap",
        )
    if mode in {HardwareMode.PROSUMER_SERIAL_16_24, HardwareMode.SERVER48_BATCHED}:
        if has_dsp:
            return ScheduledStep(
                "DeepSeek-Prover candidate",
                "GPU 0, serialized with retrieval",
                "gpu-serial",
                "SOFT Lean candidate; generation-limit is failure",
            )
        if has_qed:
            return ScheduledStep(
                "QED-Nano candidate",
                "GPU 0, serialized with retrieval",
                "gpu-serial",
                "SOFT natural-language candidate; exact token cap",
            )
    if mode is HardwareMode.MULTI_GPU_SPLIT and has_dsp:
        return ScheduledStep(
            "DeepSeek-Prover candidate",
            "GPU 0 after embedding batch",
            "gpu0-queued",
            "SOFT Lean candidate; generation-limit is failure",
        )
    if requirement is CandidateRequirement.LOCAL_REQUIRED:
        raise UnschedulablePlan(
            "a local candidate model was required but no fitting registered asset exists"
        )
    if inventory.api_candidate_available:
        return ScheduledStep(
            "API candidate model",
            "remote API",
            "network",
            "SOFT candidate; provider identity recorded",
        )
    raise UnschedulablePlan("no local candidate model or candidate API is available")


def _retrieval_steps(
    mode: HardwareMode,
    inventory: HardwareInventory,
    requirement: RetrievalRequirement,
    top_k: int,
) -> tuple[ScheduledStep, ...]:
    has_embedding = "qwen3-embedding-8b" in inventory.local_assets
    has_reranker = "qwen3-reranker-8b" in inventory.local_assets
    local_rerank_capable = mode in {
        HardwareMode.PROSUMER_SERIAL_16_24,
        HardwareMode.SERVER48_BATCHED,
        HardwareMode.MULTI_GPU_SPLIT,
    }
    if requirement is RetrievalRequirement.RERANK_REQUIRED:
        if not (has_embedding and has_reranker and local_rerank_capable):
            raise UnschedulablePlan(
                "reranking was required; retriever-only fallback would be lossy and is refused"
            )
        if mode is HardwareMode.MULTI_GPU_SPLIT:
            return (
                ScheduledStep(
                    "LeanSearch embedding/retrieval",
                    "GPU 0",
                    "retrieval-parallel",
                    f"preserve top_k={top_k}",
                ),
                ScheduledStep(
                    "LeanSearch reranker",
                    "GPU 1",
                    "retrieval-parallel",
                    f"rerank all requested top_k={top_k}",
                ),
            )
        return (
            ScheduledStep(
                "LeanSearch embedding/retrieval",
                "GPU 0 batch, then unload",
                "gpu-serial",
                f"preserve top_k={top_k}",
            ),
            ScheduledStep(
                "LeanSearch reranker",
                "GPU 0 after memory-reclaim barrier",
                "gpu-serial",
                f"rerank all requested top_k={top_k}",
            ),
        )
    if inventory.public_retrieval_available:
        return (
            ScheduledStep(
                "public LeanSearch retriever",
                "remote service",
                "network",
                f"baseline retriever, preserve top_k={top_k}",
            ),
        )
    if has_embedding and mode is not HardwareMode.PORTABLE_CPU_API:
        return (
            ScheduledStep(
                "local LeanSearch retriever",
                "GPU 0 serialized",
                "gpu-serial",
                f"baseline retriever, preserve top_k={top_k}",
            ),
        )
    raise UnschedulablePlan("no baseline premise retrieval path is available")


def schedule_research(
    request: ScheduleRequest,
    inventory: HardwareInventory,
    identities: tuple[ComponentIdentity, ...],
) -> ScheduleDecision:
    """Create a deterministic placement plan and expose every mode fallback and identity gap."""

    native_mode = classify_hardware(inventory)
    requested = request.requested_mode
    fallback: list[str] = []
    if requested is HardwareMode.AUTO:
        executed = native_mode
    elif _supports(requested, inventory):
        executed = requested
    elif request.allow_explicit_mode_fallback:
        executed = native_mode
        fallback.append(
            f"requested {requested.value} is unsupported; explicitly executed {native_mode.value}"
        )
    else:
        raise UnschedulablePlan(
            f"requested mode {requested.value} is unsupported and explicit fallback is disabled"
        )

    steps = list(
        _retrieval_steps(
            executed, inventory, request.retrieval_requirement, request.retrieval_top_k
        )
    )
    steps.append(_candidate_step(executed, inventory, request.candidate_requirement))
    if request.require_jixia:
        steps.append(
            ScheduledStep(
                "jixia structural analysis",
                "CPU; full analysis only for selected weight-bearing nodes",
                "cpu-analysis",
                "selection is recorded; omitted nodes receive no jixia claim",
            )
        )
    if executed is HardwareMode.PORTABLE_CPU_API:
        steps.append(
            ScheduledStep(
                "registered CAS/SMT probes",
                "CPU, certificate-aware registered adapters",
                "cpu-probes",
                "heuristic unless a trusted checker replays a certificate",
            )
        )
    steps.append(
        ScheduledStep(
            "Lean clean replay and axiom audit",
            "CPU, fresh verifier process",
            "verifier-exclusive",
            "mandatory pinned final replay; never shared with generator",
        )
    )

    required_identities: dict[ComponentKind, set[str]] = {
        ComponentKind.MODEL: set(),
        ComponentKind.RETRIEVAL: set(),
        ComponentKind.VERIFIER: {"lean-mathlib-profile"},
    }
    for step in steps:
        if step.component == "DeepSeek-Prover candidate":
            required_identities[ComponentKind.MODEL].add("deepseek-prover-v2-7b")
        elif step.component == "QED-Nano candidate":
            required_identities[ComponentKind.MODEL].add("qed-nano")
        elif step.component == "API candidate model":
            # The selected API model is the single declared MODEL identity for this plan.
            model_ids = {
                item.component_id for item in identities if item.kind is ComponentKind.MODEL
            }
            if len(model_ids) == 1:
                required_identities[ComponentKind.MODEL].update(model_ids)
            else:
                required_identities[ComponentKind.MODEL].add("configured-api-model")
        elif step.component == "public LeanSearch retriever":
            required_identities[ComponentKind.RETRIEVAL].add("public-leansearch")
        elif step.component in {"LeanSearch embedding/retrieval", "local LeanSearch retriever"}:
            required_identities[ComponentKind.RETRIEVAL].add("qwen3-embedding-8b")
        elif step.component == "LeanSearch reranker":
            required_identities[ComponentKind.RETRIEVAL].add("qwen3-reranker-8b")
    identities_by_key = {(item.kind, item.component_id): item for item in identities}
    identity_gaps = tuple(
        identity.component_id
        for identity in identities
        if identity.status is IdentityStatus.UNKNOWN or identity.digest == UNKNOWN_DIGEST
    ) + tuple(
        f"MISSING_{kind.value}_IDENTITY:{component_id}"
        for kind in ComponentKind
        for component_id in sorted(required_identities[kind])
        if (kind, component_id) not in identities_by_key
    )
    summary = (
        f"os={inventory.os_family}; ram={inventory.system_memory_gb}GB; "
        f"gpu_vram={list(inventory.gpu_vram_gb)}; apple_unified={inventory.apple_unified_memory}"
    )
    lossless_conditions = (
        "精确 imports 只在依赖闭包等价检查通过时启用，否则恢复原 imports",
        "缓存 olean 必须同 attempt、同源码/工具链/Mathlib digest；最终仍做新进程 clean replay",
        "批处理只改变排队与装载，不改变查询、top_k、rerank 输入或候选 token 上限",
        "jixia 可只调度到承重节点，但未分析节点不得伪装成已分析",
    )
    forbidden_shortcuts = (
        "把关闭 reranker 称为无损回退",
        "降低 retrieval_top_k 或召回深度",
        "跳过最终 Lean clean replay 或公理审计",
        "复用跨 attempt、跨源码或跨 Mathlib 的 olean",
        "模型撞 token 上限后仍把截断结果记为成功",
        "单 GPU 同时常驻两个 8B 检索模型导致静默 OOM 回退",
    )
    payload = {
        "requested_mode": requested.value,
        "executed_mode": executed.value,
        "hardware_summary": summary,
        "fallback_reasons": fallback,
        "steps": [step.to_dict() for step in steps],
        "identities": [identity.to_dict() for identity in identities],
        "retrieval_top_k": request.retrieval_top_k,
        "final_replay_required": True,
        "lossless_conditions": lossless_conditions,
        "forbidden_shortcuts": forbidden_shortcuts,
    }
    plan_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ScheduleDecision(
        requested_mode=requested,
        executed_mode=executed,
        hardware_summary=summary,
        fallback_reasons=tuple(fallback),
        steps=tuple(steps),
        identities=identities,
        identity_gaps=identity_gaps,
        retrieval_top_k=request.retrieval_top_k,
        final_replay_required=True,
        lossless_conditions=lossless_conditions,
        forbidden_shortcuts=forbidden_shortcuts,
        plan_digest=plan_digest,
    )


def current_server48_inventory() -> HardwareInventory:
    """The current 48 GB server baseline; assets are present, not thereby authority-bearing."""

    return HardwareInventory(
        os_family="linux",
        system_memory_gb=1_007,
        gpu_vram_gb=(48,),
        api_candidate_available=True,
        public_retrieval_available=True,
        local_assets=frozenset(
            {
                "qed-nano",
                "deepseek-prover-v2-7b",
                "qwen3-embedding-8b",
                "qwen3-reranker-8b",
            }
        ),
    )


def _nvidia_vram(run: CommandRunner) -> tuple[int, ...]:
    completed = run(
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if completed.returncode != 0:
        return ()
    return tuple(
        max(1, round(int(line.strip()) / 1024))
        for line in completed.stdout.splitlines()
        if line.strip().isdigit()
    )


def _rocm_vram(run: CommandRunner) -> tuple[int, ...]:
    completed = run(
        ["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if completed.returncode != 0:
        return ()
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        return ()
    memory: list[int] = []
    for card in value.values():
        if not isinstance(card, dict):
            continue
        raw = card.get("VRAM Total Memory (B)")
        if isinstance(raw, str) and raw.isdigit():
            memory.append(max(1, round(int(raw) / 1024**3)))
        elif isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            memory.append(max(1, round(raw / 1024**3)))
    return tuple(memory)


def detect_local_inventory(
    *,
    api_candidate_available: bool = False,
    public_retrieval_available: bool = False,
    local_assets: frozenset[str] = frozenset(),
    command_runner: CommandRunner = subprocess.run,
) -> HardwareInventory:
    """Discover the host actually running RK without trusting caller-declared VRAM."""

    memory_bytes = 0
    try:
        psutil = importlib.import_module("psutil")
        memory_bytes = int(psutil.virtual_memory().total)
    except (ImportError, OSError, ValueError):
        if os.name == "posix":
            sysconf = os.sysconf
            page_size = int(sysconf("SC_PAGE_SIZE"))
            memory_bytes = page_size * int(sysconf("SC_PHYS_PAGES"))
    if memory_bytes == 0 and os.name == "nt":
        try:
            completed = command_runner(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode == 0:
                memory_bytes = int(completed.stdout.strip())
        except (OSError, subprocess.SubprocessError, ValueError):
            memory_bytes = 0
    memory_gb = max(1, round(memory_bytes / 1024**3))
    gpu_vram: tuple[int, ...] = ()
    try:
        gpu_vram = _nvidia_vram(command_runner)
    except (OSError, subprocess.SubprocessError, ValueError):
        gpu_vram = ()
    if not gpu_vram:
        try:
            gpu_vram = _rocm_vram(command_runner)
        except (OSError, subprocess.SubprocessError, TypeError, ValueError, json.JSONDecodeError):
            gpu_vram = ()
    os_family = platform.system().lower() or "unknown"
    return HardwareInventory(
        os_family=os_family,
        system_memory_gb=memory_gb,
        gpu_vram_gb=gpu_vram,
        apple_unified_memory=os_family == "darwin",
        api_candidate_available=api_candidate_available,
        public_retrieval_available=public_retrieval_available,
        local_assets=local_assets,
    )
