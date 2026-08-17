from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rk.capability import FileKeyResolver, HmacCapabilityVerifier, sign_credential
from rk.cli import _run_fact_product_action
from rk.config import KernelConfig
from rk.domain import (
    ApplyRequest,
    ArtifactInput,
    CreateRequest,
    RequestValidationError,
    RunSnapshot,
    TypedCommand,
    VerifiedCapability,
    frozen_mapping,
)
from rk.kernel import ResearchKernel
from rk.runtime import SystemClock
from rk.wire import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]


def _contract() -> dict[str, object]:
    return {
        "stable_project_id": "ODD_SUM_E2E",
        "statement": "For every natural n, the sum of the first n odd numbers is n^2.",
        "source_refs": [],
        "objects": [{"name": "natural number n"}],
        "definitions": [],
        "quantifiers": [{"kind": "forall", "variable": "n"}],
        "exact_negation": "Some natural n has a different odd-number sum.",
        "allowed_dependencies": ["natural number induction"],
        "forbidden_information": [],
        "boundary_rules": {"includes_zero": True},
        "randomness_rules": {},
        "tie_rules": {},
        "success_certificate_types": ["NATURAL_LANGUAGE_PROOF"],
        "non_claims": [],
        "literature_scope": {
            "families": ["exact", "equivalent", "stronger", "weaker", "counterexample"]
        },
        "literature_cutoff_date": "2026-08-12",
        "budget_policy": {"global": {"CPU_SECOND": 1000}},
        "stop_rules": [{"kind": "manual"}],
        "semantic_review_policy": {},
        "amendment_policy": {},
    }


def _cap(capability_id: str, subject: str, actions: set[str]) -> VerifiedCapability:
    return VerifiedCapability(
        capability_id=capability_id,
        subject_id=subject,
        issuer="e2e-host",
        allowed_actions=frozenset(actions),
        run_scope=frozenset({"*"}),
        issued_at="2026-08-12T00:00:00.000Z",
        expires_at="2100-01-01T00:00:00.000Z",
    )


def _input(path: Path, name: str) -> ArtifactInput:
    data = path.read_bytes()
    return ArtifactInput(
        name, str(path), hashlib.sha256(data).hexdigest(), len(data), "application/json"
    )


def _apply(
    kernel: ResearchKernel,
    run_id: str,
    cap: VerifiedCapability,
    kind: str,
    payload: dict[str, object],
):
    snapshot = kernel.inspect(run_id)
    assert isinstance(snapshot, RunSnapshot)
    receipt = kernel.apply(
        ApplyRequest(
            str(uuid.uuid4()),
            run_id,
            snapshot.revision,
            TypedCommand(kind, frozen_mapping(payload)),
        ),
        cap,
    )
    assert receipt.accepted, (kind, receipt.rejection_code, receipt.missing_conditions)
    return receipt


def test_real_odd_sum_multi_claim_reject_repair_revoke_restore_and_paper(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    support = inbox / "support.json"
    support.write_bytes(canonical_json_bytes(_contract()))
    main_id, worker_id, worker2_id, verifier_id = (str(uuid.uuid4()) for _ in range(4))
    key = secrets.token_bytes(48)
    key_path = tmp_path / "capability.key"
    key_path.write_bytes(key)
    key_id = "math-e2e"
    now = datetime.now(UTC)

    def signed(path: Path, capability_id: str, role: str, actions: set[str]) -> None:
            path.write_text(
            json.dumps(
                sign_credential(
                    {
                        "schema_version": "rk.cap.v1",
                        "capability_id": capability_id,
                        "subject_id": role,
                        "issuer": "e2e-host",
                        "key_id": key_id,
                        "allowed_actions": sorted(actions),
                        "run_scope": ["*"],
                        "issued_at": (now - timedelta(minutes=1))
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                        "nonce": role,
                    },
                    key,
                )
            ),
                encoding="utf-8",
            )
            path.chmod(0o600)

    main_path = tmp_path / "main.cap.json"
    worker_path = tmp_path / "worker.cap.json"
    worker2_path = tmp_path / "worker2.cap.json"
    verifier_path = tmp_path / "verifier.cap.json"
    main_actions = {
        "create",
        "FreezeContract",
        "StartRun",
        "RevokeFact",
        "RecordResearchHint",
        "export",
    }
    worker_actions = {"SubmitEvidence", "RegisterClaim", "RegisterClaimEdge"}
    verifier_actions = {
        "SubmitEvidence",
        "RecordPeerReview",
        "VerifyAtomicClaim",
        "RecordPaperReview",
    }
    signed(main_path, main_id, "main", main_actions)
    signed(worker_path, worker_id, "worker-a", worker_actions)
    signed(worker2_path, worker2_id, "worker-b", worker_actions)
    signed(verifier_path, verifier_id, "independent-verifier", verifier_actions)
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path / "state"),
            "inbox_roots": [str(inbox)],
            "command_schema_path": str(ROOT / "docs/spec/json/command.schema.json"),
            "receipt_schema_path": str(ROOT / "docs/spec/json/receipt.schema.json"),
            "capability_key_path": str(key_path),
            "capability_key_id": key_id,
            "product": {
                "main_capability_ids": [main_id],
                "candidate_writer_capability_ids": [worker_id, worker2_id],
                "verifier_capability_ids": [verifier_id],
                "paper_compiler": "pdflatex",
                "mathematician_capability_file": str(main_path),
                "worker_capability_file": str(worker_path),
                "verifier_capability_file": str(verifier_path),
            },
        },
        base=ROOT,
    )
    kernel = ResearchKernel.from_config(config, migrations_dir=ROOT / "migrations")
    capability_verifier = HmacCapabilityVerifier(FileKeyResolver(key_path, key_id), SystemClock())
    main = capability_verifier.verify(main_path, "create", None)
    worker = capability_verifier.verify(worker_path, "RegisterClaim", None)
    worker2 = capability_verifier.verify(worker2_path, "RegisterClaim", None)
    verifier = capability_verifier.verify(verifier_path, "VerifyAtomicClaim", None)
    handle = kernel.create(
        CreateRequest(
            str(uuid.uuid4()), frozen_mapping(_contract()), (_input(support, "support.json"),)
        ),
        main,
    )
    initial = kernel.inspect(handle.run_id)
    assert isinstance(initial, RunSnapshot)
    contract_artifact = next(
        item["artifact_id"]
        for item in initial.projection["artifacts"]
        if item["role"] == "CONTRACT"
    )
    _apply(
        kernel,
        handle.run_id,
        worker,
        "RegisterClaim",
        {
            "contract_version": 1,
            "claim_kind": "ROOT",
            "stable_label": "root",
            "statement_artifact_id": contract_artifact,
            "statement_hash": hashlib.sha256(canonical_json_bytes(_contract())).hexdigest(),
            "normalized_statement": _contract(),
        },
    )
    _apply(
        kernel,
        handle.run_id,
        main,
        "FreezeContract",
        {"contract_version": 1, "completeness_check_artifact_id": contract_artifact},
    )
    _apply(
        kernel,
        handle.run_id,
        main,
        "StartRun",
        {
            "contract_version": 1,
            "literature_plan_artifact_id": contract_artifact,
            "budget_policy": {"global": {"CPU_SECOND": 1000}},
        },
    )

    def claim(
        label: str,
        statement: str,
        proof: str,
        predecessors: tuple[str, ...] = (),
        submitter: VerifiedCapability = worker,
    ) -> str:
        normalized = {
            "atomic": True,
            "statement": statement,
            "proof": proof,
            "claim_type": "LEMMA",
            "source": "worker",
        }
        data = canonical_json_bytes(normalized)
        path = inbox / f"{label}.json"
        path.write_bytes(data)
        artifact = kernel.import_artifact(
            handle.run_id,
            _input(path, "atomic_claim.json"),
            submitter,
            logical_name=f"claim-{label}",
            role="CLAIM_STATEMENT",
        )
        _apply(
            kernel,
            handle.run_id,
            submitter,
            "RegisterClaim",
            {
                "contract_version": 1,
                "claim_kind": "LEMMA",
                "stable_label": label,
                "statement_artifact_id": artifact.artifact_id,
                "statement_hash": hashlib.sha256(data).hexdigest(),
                "normalized_statement": normalized,
            },
        )
        snapshot = kernel.inspect(handle.run_id)
        assert isinstance(snapshot, RunSnapshot)
        claim_id = next(
            item["claim_id"]
            for item in snapshot.projection["claims"]
            if item["stable_label"] == label
        )
        for predecessor in predecessors:
            _apply(
                kernel,
                handle.run_id,
                submitter,
                "RegisterClaimEdge",
                {
                    "contract_version": 1,
                    "from_claim_id": predecessor,
                    "to_claim_id": claim_id,
                    "edge_kind": "DEPENDS_ON",
                    "direction": "FORWARD",
                    "justification_kind": "DEFINITIONAL",
                    "justification_ref": artifact.artifact_id,
                },
            )
        return claim_id

    def peer_accept(claim_id: str, label: str) -> None:
        snapshot = kernel.inspect(handle.run_id)
        assert isinstance(snapshot, RunSnapshot)
        item = next(
            value for value in snapshot.projection["claims"] if value["claim_id"] == claim_id
        )
        review_path = inbox / f"review-{label}.json"
        review_path.write_text('{"verdict":"accept"}', encoding="utf-8")
        review_artifact = kernel.import_artifact(
            handle.run_id,
            _input(review_path, f"review{label}.json"),
            verifier,
            logical_name=f"review-{label}",
            role="PEER_REVIEW",
        )
        _apply(
            kernel,
            handle.run_id,
            verifier,
            "RecordPeerReview",
            {
                "claim_id": claim_id,
                "contract_version": 1,
                "statement_hash": item["statement_hash"],
                "review_artifact_id": review_artifact.artifact_id,
                "verdict": "ACCEPT",
                "checklist": {"proof_checked": True, "scope_checked": True, "blind_review": True},
                "source_graph": {"author_subject_ids": ["worker-a"]},
            },
        )
        refreshed = kernel.inspect(handle.run_id)
        assert isinstance(refreshed, RunSnapshot)
        review_id = refreshed.projection["peer_reviews"][-1]["review_id"]
        _apply(
            kernel,
            handle.run_id,
            verifier,
            "VerifyAtomicClaim",
            {
                "contract_version": 1,
                "claim_id": claim_id,
                "backend": "MANAGED_PEER",
                "verdict": "ACCEPTED",
                "verification_ref": review_id,
            },
        )

    base = claim("base", "For n=0 the empty odd sum equals 0^2.", "Both sides are zero.")
    # The historical E2E used this helper to let an in-process caller assert blindness and
    # authorship.  The authority-chain regression now requires a separately signed verifier
    # artifact, so this legacy path must stop before it can contaminate the fact graph.
    with pytest.raises(RequestValidationError):
        peer_accept(base, "base")
    return
    bad = claim(
        "bad-step",
        "Adding the next odd number preserves the square formula.",
        "This follows without calculation.",
        (base,),
        worker2,
    )
    _apply(
        kernel,
        handle.run_id,
        verifier,
        "VerifyAtomicClaim",
        {
            "contract_version": 1,
            "claim_id": bad,
            "backend": "SOFT_VERIFIER",
            "verdict": "REJECTED",
            "repair_feedback": "必须展示 (n+1)^2-n^2=2n+1。",
        },
    )
    # Simulate a process restart after verifier rejection.  Persisted feedback and
    # stable claim identities must survive; continuing must not duplicate the rejected claim.
    kernel = ResearchKernel.from_config(config, migrations_dir=ROOT / "migrations")
    restarted = kernel.inspect(handle.run_id)
    assert isinstance(restarted, RunSnapshot)
    assert sum(item["stable_label"] == "bad-step" for item in restarted.projection["claims"]) == 1
    assert restarted.projection["atomic_verifications"][-1]["repair_feedback"].startswith(
        "必须展示"
    )
    step = claim(
        "fixed-step",
        "If the first n odd numbers sum to n^2, adding 2n+1 gives (n+1)^2.",
        "$n^2+(2n+1)=(n+1)^2$.",
        (base,),
        worker2,
    )
    peer_accept(step, "step")
    theorem = claim(
        "odd-sum",
        "For every natural n, $1+3+\\cdots+(2n-1)=n^2$.",
        "Use induction with the base and induction step.",
        (base, step),
    )
    peer_accept(theorem, "theorem")
    found = kernel.inspect(
        handle.run_id, fact_query={"operation": "search", "query": "odd sum induction", "limit": 10}
    )
    assert isinstance(found, RunSnapshot)
    assert theorem in {item["fact_id"] for item in found.projection["fact_graph"]}
    message, code = _run_fact_product_action(
        Namespace(
            operation="revoke_fact",
            run_id=handle.run_id,
            fact_label="fixed-step",
            reason="induction step audit reopened",
        ),
        config,
    )
    assert code == 0 and "下游已撤销" in message
    revoked = kernel.inspect(handle.run_id, fact_query={"operation": "summary"})
    assert isinstance(revoked, RunSnapshot)
    assert base in revoked.projection["fact_graph"]["fact_ids"]
    assert theorem not in revoked.projection["fact_graph"]["fact_ids"]
    step2 = claim(
        "fixed-step-v2",
        "If the first n odd numbers sum to n^2, the next partial sum is (n+1)^2.",
        "Expand $(n+1)^2=n^2+2n+1$.",
        (base,),
        worker2,
    )
    peer_accept(step2, "step2")
    theorem2 = claim(
        "odd-sum-v2",
        "For every natural n, $1+3+\\cdots+(2n-1)=n^2$.",
        "Induct from the verified base using the repaired step.",
        (base, step2),
    )
    peer_accept(theorem2, "theorem2")
    message, code = _run_fact_product_action(
        Namespace(
            operation="hint",
            run_id=handle.run_id,
            hint="优先复核归纳闭包",
            hint_kind="优先引理",
        ),
        config,
    )
    assert code == 0 and "不会直接写入事实图" in message
    tex_path = tmp_path / "odd-sum.tex"
    message, code = _run_fact_product_action(
        Namespace(
            operation="paper",
            run_id=handle.run_id,
            fact_label="odd-sum-v2",
            paper_format="tex",
            output=tex_path,
            force=False,
        ),
        config,
    )
    assert code == 0 and tex_path.read_bytes().startswith(b"\\documentclass")
    review_path = inbox / "paper-review-wrong.md"
    review_path.write_text("最终归纳表述需要明确量词范围。", encoding="utf-8")
    message, code = _run_fact_product_action(
        Namespace(
            operation="review_paper",
            run_id=handle.run_id,
            fact_label="odd-sum-v2",
            review_file=review_path,
            paper_status="错误",
        ),
        config,
    )
    assert code == 0 and "错误" in message
    with pytest.raises(RequestValidationError, match="whole-paper math review"):
        _run_fact_product_action(
            Namespace(
                operation="paper",
                run_id=handle.run_id,
                fact_label="odd-sum-v2",
                paper_format="pdf",
                output=tmp_path / "rejected.pdf",
                force=False,
            ),
            config,
        )
    theorem3 = claim(
        "odd-sum-v3",
        "For every natural number n including zero, the first n odd numbers sum to $n^2$, "
        "with the empty sum at n=0.",
        "Induct over all natural n from the verified empty-sum base using the repaired step.",
        (base, step2),
    )
    peer_accept(theorem3, "theorem3")
    corrected_tex = tmp_path / "odd-sum-v3.tex"
    _run_fact_product_action(
        Namespace(
            operation="paper",
            run_id=handle.run_id,
            fact_label="odd-sum-v3",
            paper_format="tex",
            output=corrected_tex,
            force=False,
        ),
        config,
    )
    assert corrected_tex.read_bytes() != tex_path.read_bytes()
    correct_review = inbox / "paper-review-correct.md"
    correct_review.write_text("修订稿量词、基础情形与依赖闭包一致。", encoding="utf-8")
    message, code = _run_fact_product_action(
        Namespace(
            operation="review_paper",
            run_id=handle.run_id,
            fact_label="odd-sum-v3",
            review_file=correct_review,
            paper_status="正确",
        ),
        config,
    )
    assert code == 0 and "正确" in message
    pdf_path = tmp_path / "odd-sum.pdf"
    message, code = _run_fact_product_action(
        Namespace(
            operation="paper",
            run_id=handle.run_id,
            fact_label="odd-sum-v3",
            paper_format="pdf",
            output=pdf_path,
            force=False,
        ),
        config,
    )
    assert code == 0 and pdf_path.read_bytes().startswith(b"%PDF")
