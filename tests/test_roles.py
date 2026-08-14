from __future__ import annotations

import jsonschema
import pytest

from rk.roles import (
    CapabilityLayer,
    MathRole,
    RoleAssignment,
    RoleBudget,
    bind_role_prompt,
    math_capability_matrix,
    role_catalog,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def assignment(**overrides: object) -> RoleAssignment:
    values: dict[str, object] = {
        "contract_id": "contract-1",
        "contract_version": 1,
        "contract_hash": HASH_A,
        "claim_id": "claim-1",
        "statement_hash": HASH_B,
        "input_artifacts": {
            "FROZEN_CONTRACT": ("artifact-contract",),
            "CLAIM_STATEMENT": ("artifact-claim",),
        },
        "task": "寻找三条结构不同的路线。",
    }
    values.update(overrides)
    return RoleAssignment(**values)  # type: ignore[arg-type]


def test_catalog_has_nine_versioned_soft_roles_with_required_controls() -> None:
    catalog = role_catalog()

    assert {spec.role for spec in catalog} == set(MathRole)
    assert len({spec.role_id for spec in catalog}) == 9
    assert len({spec.prompt_sha256 for spec in catalog}) == 9
    for spec in catalog:
        assert spec.role_id.endswith(".v1")
        assert len(spec.prompt_sha256) == 64
        assert spec.authority_ceiling == "SOFT_CANDIDATE_ONLY"
        assert "量词" in spec.system_prompt
        assert "歧义" in spec.system_prompt
        assert "预算" in spec.system_prompt
        assert "停止条件" in spec.system_prompt
        assert "不得伪造" in spec.system_prompt


def test_binding_freezes_scope_and_is_deterministic() -> None:
    bound = bind_role_prompt(MathRole.ROUTE_SCOUT, assignment())
    repeated = bind_role_prompt(MathRole.ROUTE_SCOUT, assignment())

    assert bound.assignment_digest == repeated.assignment_digest
    assert bound.prompt_sha256 == repeated.prompt_sha256
    assert HASH_A in bound.assignment_prompt
    assert HASH_B in bound.assignment_prompt
    assert bound.authority_ceiling == "SOFT_CANDIDATE_ONLY"


def test_binding_rejects_missing_and_forbidden_artifacts() -> None:
    with pytest.raises(ValueError, match="missing required"):
        bind_role_prompt(
            MathRole.ROUTE_SCOUT,
            assignment(input_artifacts={"FROZEN_CONTRACT": ("contract",)}),
        )
    with pytest.raises(ValueError, match="not allowed"):
        bind_role_prompt(
            MathRole.ROUTE_SCOUT,
            assignment(
                input_artifacts={
                    "FROZEN_CONTRACT": ("contract",),
                    "CLAIM_STATEMENT": ("claim",),
                    "OTHER_AGENT_PRIVATE_MEMORY": ("memory",),
                }
            ),
        )


def test_assignment_cannot_silently_expand_budget() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        bind_role_prompt(
            MathRole.ROUTE_SCOUT,
            assignment(budget=RoleBudget(2, 20_000, 5_000)),
        )


def test_bound_output_schema_rejects_authority_and_role_spoofing() -> None:
    bound = bind_role_prompt(MathRole.ROUTE_SCOUT, assignment())
    output = {
        "schema_version": "rk.role-output.v1",
        "role_id": bound.role_id,
        "prompt_sha256": bound.prompt_sha256,
        "assignment_digest": bound.assignment_digest,
        "authority": "SOFT_CANDIDATE_ONLY",
        "status": "CANDIDATE",
        "contract_scope": {
            "contract_id": "contract-1",
            "contract_version": 1,
            "contract_hash": HASH_A,
        },
        "claim_scope": {"claim_id": "claim-1", "statement_hash": HASH_B},
        "artifacts_used": ["artifact-contract", "artifact-claim"],
        "open_obligations": [],
        "payload": {"routes": [], "shared_blockers": [], "fast_falsifiers": []},
    }

    jsonschema.validate(output, bound.output_schema)
    output["authority"] = "HARD_MACHINE"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(output, bound.output_schema)


def test_bound_output_schema_rejects_cross_claim_and_undeclared_artifact() -> None:
    bound = bind_role_prompt(MathRole.ROUTE_SCOUT, assignment())
    output = {
        "schema_version": "rk.role-output.v1",
        "role_id": bound.role_id,
        "prompt_sha256": bound.prompt_sha256,
        "assignment_digest": bound.assignment_digest,
        "authority": "SOFT_CANDIDATE_ONLY",
        "status": "CANDIDATE",
        "contract_scope": {
            "contract_id": "contract-1",
            "contract_version": 1,
            "contract_hash": HASH_A,
        },
        "claim_scope": {"claim_id": "other-claim", "statement_hash": HASH_B},
        "artifacts_used": ["artifact-contract", "artifact-claim"],
        "open_obligations": [],
        "payload": {"routes": [], "shared_blockers": [], "fast_falsifiers": []},
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(output, bound.output_schema)

    output["claim_scope"] = {"claim_id": "claim-1", "statement_hash": HASH_B}
    output["artifacts_used"] = ["undeclared-private-memory"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(output, bound.output_schema)


def test_capability_matrix_separates_kernel_adapter_bypass_and_missing() -> None:
    matrix = math_capability_matrix()
    layers = {entry.layer for entry in matrix}

    assert layers == set(CapabilityLayer)
    assert next(entry for entry in matrix if entry.capability_id == "model-benchmarks").layer == (
        CapabilityLayer.SCRIPT_BYPASS
    )
    host = next(
        entry for entry in matrix if entry.capability_id == "host-execution-receipt-service"
    )
    assert host.layer is CapabilityLayer.KERNEL
    assert host.maturity == "IMPLEMENTED_LEAN_ROOT_ONLY"
    assert host.authority_ceiling == "KERNEL_VERIFIED"
    attestation = next(
        entry
        for entry in matrix
        if entry.capability_id == "independent-verifier-artifact-import"
    )
    assert attestation.layer is CapabilityLayer.ADAPTER
    assert attestation.maturity == "ADAPTER_TESTED_DEPLOYMENT_IDENTITY_REQUIRED"
    signature_service = next(
        entry for entry in matrix if entry.capability_id == "human-signature-service"
    )
    assert signature_service.layer is CapabilityLayer.MISSING
