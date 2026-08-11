from rk.domain import RunSnapshot, frozen_mapping
from rk.dossier import DossierBuilder


def test_dossier_is_byte_stable_and_sorts_claims() -> None:
    snapshot = RunSnapshot(
        run_id="run",
        status="CLOSED",
        revision=7,
        current_contract_version=2,
        last_cursor=10,
        projection=frozen_mapping(
            {
                "claims": [
                    {"claim_id": "b", "machine_verdict": "UNVERIFIED"},
                    {"claim_id": "a", "machine_verdict": "KERNEL_VERIFIED"},
                ],
                "open_obligation_ids": [],
            }
        ),
    )
    spec = {"format": "JSON", "language": "zh-CN", "include_raw_artifacts": False}
    builder = DossierBuilder()

    first, media = builder.build(snapshot, spec)
    second, _ = builder.build(snapshot, spec)

    assert first == second
    assert first.index(b'"claim_id":"a"') < first.index(b'"claim_id":"b"')
    assert media == "application/json"
