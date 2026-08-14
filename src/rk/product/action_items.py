"""Cross-run action-item aggregation from authoritative available_actions."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RunActions:
    run_id: str
    research_revision: int
    contract_version: int
    actions: tuple[dict[str, Any], ...]


def aggregate_action_items(
    runs: Iterable[RunActions], principal_subject_id: str
) -> tuple[dict[str, Any], ...]:
    result = []
    for run in runs:
        for action in run.actions:
            if action.get("principal_subject_id") != principal_subject_id:
                continue
            command = str(action["command_type"])
            targets = tuple(sorted(map(str, action["target_ids"])))
            stable = hashlib.sha256(
                (run.run_id + "\0" + command + "\0" + "\0".join(targets)).encode()
            ).hexdigest()
            result.append(
                {
                    "stable_entity_id": "action:" + stable,
                    "run_id": run.run_id,
                    "command_type": command,
                    "target_ids": list(targets),
                    "required_inputs": list(action.get("required_inputs", [])),
                    "blocked_by": list(action.get("blocked_by", [])),
                    "research_revision": run.research_revision,
                    "contract_version": run.contract_version,
                }
            )
    return tuple(
        sorted(result, key=lambda x: (x["run_id"], x["command_type"], x["stable_entity_id"]))
    )
