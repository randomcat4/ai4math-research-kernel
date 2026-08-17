from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

import pytest

from rk.scheduler import (
    CandidateRequirement,
    ComponentIdentity,
    ComponentKind,
    HardwareInventory,
    HardwareMode,
    IdentityStatus,
    RetrievalRequirement,
    ScheduleRequest,
    UnschedulablePlan,
    classify_hardware,
    current_server48_inventory,
    detect_local_inventory,
    schedule_research,
)

HASH_A = "a" * 64


def test_local_hardware_detection_feeds_the_real_scheduler() -> None:
    inventory = detect_local_inventory(
        api_candidate_available=True,
        public_retrieval_available=True,
    )
    decision = schedule_research(ScheduleRequest(require_jixia=False), inventory, ())
    assert inventory.system_memory_gb > 0
    assert decision.executed_mode == classify_hardware(inventory)
    assert decision.plan_digest


def test_rocm_inventory_detects_real_json_vram_when_nvidia_is_unavailable() -> None:
    calls: list[Sequence[str]] = []

    def run(argv: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] == "nvidia-smi":
            raise FileNotFoundError("nvidia-smi")
        return subprocess.CompletedProcess(
            list(argv), 0,
            json.dumps({
                "card0": {
                    "VRAM Total Memory (B)": "51522830336",
                    "Card Series": "AMD Radeon Graphics",
                }
            }), "",
        )

    inventory = detect_local_inventory(
        api_candidate_available=True,
        public_retrieval_available=True,
        local_assets=frozenset({"qed-nano", "deepseek-prover-v2-7b"}),
        command_runner=run,
    )

    assert inventory.gpu_vram_gb == (48,)
    assert classify_hardware(inventory) is HardwareMode.SERVER48_BATCHED
    assert [call[0] for call in calls if call[0].endswith("-smi")] == [
        "nvidia-smi", "rocm-smi"
    ]


def test_malformed_rocm_inventory_honestly_degrades_to_no_gpu() -> None:
    def run(argv: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        if argv[0] == "nvidia-smi":
            return subprocess.CompletedProcess(list(argv), 1, "", "missing")
        return subprocess.CompletedProcess(list(argv), 0, "not-json", "")

    inventory = detect_local_inventory(command_runner=run)
    assert inventory.gpu_vram_gb == ()


def identities() -> tuple[ComponentIdentity, ...]:
    return (
        ComponentIdentity(
            ComponentKind.MODEL,
            "deepseek-prover-v2-7b",
            HASH_A,
            IdentityStatus.VERIFIED,
            "local download receipt",
        ),
        ComponentIdentity(
            ComponentKind.RETRIEVAL,
            "public-leansearch",
            "UNKNOWN",
            IdentityStatus.UNKNOWN,
            "public endpoint",
        ),
        ComponentIdentity(
            ComponentKind.VERIFIER,
            "lean-mathlib-profile",
            HASH_A,
            IdentityStatus.VERIFIED,
            "host registry",
        ),
    )


@pytest.mark.parametrize(
    ("inventory", "expected"),
    [
        (HardwareInventory("macos", 32, (), True, True, True), HardwareMode.PORTABLE_CPU_API),
        (
            HardwareInventory("windows", 32, (8,), False, True, True),
            HardwareMode.GAMING_SERIAL_8_12,
        ),
        (
            HardwareInventory("linux", 64, (24,), False, True, True),
            HardwareMode.PROSUMER_SERIAL_16_24,
        ),
        (HardwareInventory("linux", 128, (48,), False, True, True), HardwareMode.SERVER48_BATCHED),
        (
            HardwareInventory("linux", 128, (24, 24), False, True, True),
            HardwareMode.MULTI_GPU_SPLIT,
        ),
    ],
)
def test_hardware_classification(inventory: HardwareInventory, expected: HardwareMode) -> None:
    assert classify_hardware(inventory) is expected


def test_current_server_plan_preserves_rerank_and_clean_replay() -> None:
    decision = schedule_research(
        ScheduleRequest(
            retrieval_requirement=RetrievalRequirement.RERANK_REQUIRED,
            retrieval_top_k=8,
        ),
        current_server48_inventory(),
        identities(),
    )

    assert decision.requested_mode is HardwareMode.AUTO
    assert decision.executed_mode is HardwareMode.SERVER48_BATCHED
    assert decision.fallback_reasons == ()
    assert decision.retrieval_top_k == 8
    assert decision.final_replay_required is True
    components = [step.component for step in decision.steps]
    assert components[:2] == ["LeanSearch embedding/retrieval", "LeanSearch reranker"]
    assert components[-1] == "Lean clean replay and axiom audit"
    assert "public-leansearch" in decision.identity_gaps
    assert "把关闭 reranker 称为无损回退" in decision.forbidden_shortcuts


def test_portable_mac_uses_cpu_verifiers_and_api_candidate() -> None:
    inventory = HardwareInventory(
        "macos",
        32,
        apple_unified_memory=True,
        api_candidate_available=True,
        public_retrieval_available=True,
    )
    decision = schedule_research(ScheduleRequest(), inventory, identities())

    assert decision.executed_mode is HardwareMode.PORTABLE_CPU_API
    components = [step.component for step in decision.steps]
    assert "API candidate model" in components
    assert "registered CAS/SMT probes" in components
    assert "Lean clean replay and axiom audit" in components


def test_prosumer_card_can_run_qed_serially_when_dsp_is_absent() -> None:
    inventory = HardwareInventory(
        "linux",
        64,
        (20,),
        api_candidate_available=True,
        public_retrieval_available=True,
        local_assets=frozenset({"qed-nano"}),
    )
    decision = schedule_research(ScheduleRequest(), inventory, identities())
    assert decision.executed_mode is HardwareMode.PROSUMER_SERIAL_16_24
    assert "QED-Nano candidate" in [step.component for step in decision.steps]


def test_rerank_requirement_refuses_retriever_only_fallback() -> None:
    inventory = HardwareInventory(
        "windows", 32, (8,), api_candidate_available=True, public_retrieval_available=True
    )
    with pytest.raises(UnschedulablePlan, match="retriever-only fallback would be lossy"):
        schedule_research(
            ScheduleRequest(retrieval_requirement=RetrievalRequirement.RERANK_REQUIRED),
            inventory,
            identities(),
        )


def test_explicit_mode_fallback_is_opt_in_and_recorded() -> None:
    inventory = HardwareInventory(
        "windows",
        32,
        (8,),
        api_candidate_available=True,
        public_retrieval_available=True,
        local_assets=frozenset({"qed-nano"}),
    )
    request = ScheduleRequest(requested_mode=HardwareMode.SERVER48_BATCHED)
    with pytest.raises(UnschedulablePlan, match="explicit fallback is disabled"):
        schedule_research(request, inventory, identities())

    fallback = schedule_research(
        ScheduleRequest(
            requested_mode=HardwareMode.SERVER48_BATCHED,
            allow_explicit_mode_fallback=True,
        ),
        inventory,
        identities(),
    )
    assert fallback.executed_mode is HardwareMode.GAMING_SERIAL_8_12
    assert fallback.fallback_reasons


def test_final_replay_cannot_be_disabled() -> None:
    with pytest.raises(ValueError, match="cannot disable final independent replay"):
        ScheduleRequest(require_final_replay=False)


def test_local_candidate_requirement_does_not_silently_use_api() -> None:
    inventory = HardwareInventory(
        "macos",
        16,
        apple_unified_memory=True,
        api_candidate_available=True,
        public_retrieval_available=True,
    )
    with pytest.raises(UnschedulablePlan, match="local candidate model was required"):
        schedule_research(
            ScheduleRequest(candidate_requirement=CandidateRequirement.LOCAL_REQUIRED),
            inventory,
            identities(),
        )


def test_missing_identity_classes_are_explicit() -> None:
    only_model = (
        ComponentIdentity(
            ComponentKind.MODEL,
            "api-model",
            "UNKNOWN",
            IdentityStatus.UNKNOWN,
            "provider",
        ),
    )
    decision = schedule_research(ScheduleRequest(), current_server48_inventory(), only_model)
    assert "MISSING_RETRIEVAL_IDENTITY:public-leansearch" in decision.identity_gaps
    assert "MISSING_VERIFIER_IDENTITY:lean-mathlib-profile" in decision.identity_gaps


def test_unrelated_verified_identity_does_not_cover_selected_component() -> None:
    unrelated = (
        ComponentIdentity(
            ComponentKind.MODEL,
            "unrelated-model",
            HASH_A,
            IdentityStatus.VERIFIED,
            "registry",
        ),
        *identities()[1:],
    )
    decision = schedule_research(
        ScheduleRequest(), current_server48_inventory(), unrelated
    )
    assert (
        "MISSING_MODEL_IDENTITY:deepseek-prover-v2-7b" in decision.identity_gaps
    )
