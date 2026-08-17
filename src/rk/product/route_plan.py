"""Durable route proposals and the formal ``APPLY_ROUTE_PLAN`` control plane."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rk.extensions import ProductActivity
from rk.product.activity_store import ActivityStore
from rk.sqlite import open_sqlite

_STATES = frozenset({"PROPOSED", "APPROVED", "ACTIVE", "PAUSED", "STOPPED"})
_ACTIONS = frozenset({"APPROVE", "START", "PAUSE", "STOP", "SET_PRIORITY", "SET_BUDGET"})
_TRANSITIONS: Mapping[str, Mapping[str, str]] = {
    "APPROVE": {"PROPOSED": "APPROVED"},
    "START": {"APPROVED": "ACTIVE", "PAUSED": "ACTIVE"},
    "PAUSE": {"ACTIVE": "PAUSED"},
    "STOP": {"APPROVED": "STOPPED", "ACTIVE": "STOPPED", "PAUSED": "STOPPED"},
}


class RoutePlanError(RuntimeError):
    """A route proposal or formal control transition violated its contract."""


class RoutePlanConflict(RoutePlanError):
    """An immutable identity or request ID was reused with different content."""


class RoutePlanCASMismatch(RoutePlanError):
    """The command was not bound to the current research and contract versions."""


class RouteDerivationStopped(RoutePlanError):
    """A route that is not active attempted to derive new work."""


@dataclass(frozen=True, slots=True)
class RunFence:
    research_revision: int
    contract_version: int


@dataclass(frozen=True, slots=True)
class RouteProposal:
    route_id: str
    method: str
    target: str
    expected_verifier: str
    milestones: tuple[str, ...]
    termination_condition: str
    dependencies: tuple[str, ...]
    priority: int
    budget: Mapping[str, Any]

    def __post_init__(self) -> None:
        strings = (
            self.route_id,
            self.method,
            self.target,
            self.expected_verifier,
            self.termination_condition,
        )
        if any(not value or value != value.strip() for value in strings):
            raise ValueError("route proposal strings must be non-empty and trimmed")
        if not self.milestones or any(not value for value in self.milestones):
            raise ValueError("route proposal requires explicit milestones")
        if any(not value for value in self.dependencies):
            raise ValueError("route dependencies must be non-empty identities")
        if self.priority <= 0:
            raise ValueError("route priority must be positive")
        _budget(self.budget)


@dataclass(frozen=True, slots=True)
class RoutePlanProposal:
    route_plan_id: str
    run_id: str
    research_revision: int
    contract_version: int
    routes: tuple[RouteProposal, ...]

    def __post_init__(self) -> None:
        if not self.route_plan_id or not self.run_id:
            raise ValueError("route plan identities are required")
        if self.research_revision < 0 or self.contract_version <= 0:
            raise ValueError("route plan fence is invalid")
        if not self.routes:
            raise ValueError("route plan requires at least one route")
        route_ids = [route.route_id for route in self.routes]
        if len(set(route_ids)) != len(route_ids):
            raise ValueError("route identities must be unique within a plan")

    @property
    def digest(self) -> str:
        return hashlib.sha256(_json(_proposal_value(self)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PlannedRoute:
    route_id: str
    method: str
    target: str
    expected_verifier: str
    milestones: tuple[str, ...]
    termination_condition: str
    dependencies: tuple[str, ...]
    state: str
    priority: int
    budget: Mapping[str, Any]
    stop_reason: str | None


@dataclass(frozen=True, slots=True)
class RoutePlan:
    route_plan_id: str
    run_id: str
    research_revision: int
    contract_version: int
    plan_digest: str
    state: str
    state_reason: str | None
    created_at: str
    updated_at: str
    routes: tuple[PlannedRoute, ...]


@dataclass(frozen=True, slots=True)
class RoutePlanCommandResult:
    request_digest: str
    replayed: bool
    plan: RoutePlan


RunFenceReader = Callable[[sqlite3.Connection, str], RunFence]


class RoutePlanStore:
    """Own route state and activity in one SQLite transaction per formal action."""

    def __init__(
        self,
        *,
        db_path: Path,
        activities: ActivityStore,
        id_generator: Callable[[], str],
        clock: Callable[[], str],
        run_fence_reader: RunFenceReader | None = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._activities = activities
        self._ids = id_generator
        self._clock = clock
        self._run_fence_reader = run_fence_reader or sqlite_run_fence
        self._busy_timeout_ms = busy_timeout_ms

    def register_proposal(self, proposal: RoutePlanProposal) -> RoutePlan:
        now = self._clock()
        value = _proposal_value(proposal)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_fence(
                connection,
                proposal.run_id,
                proposal.research_revision,
                proposal.contract_version,
            )
            existing = connection.execute(
                "SELECT run_id,plan_digest,proposal_json FROM product_route_plans "
                "WHERE route_plan_id=?",
                (proposal.route_plan_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing[0]) != proposal.run_id
                    or str(existing[1]) != proposal.digest
                    or _object(existing[2]) != value
                ):
                    raise RoutePlanConflict("route plan identity was reused")
                connection.commit()
                return self.get(proposal.route_plan_id)
            try:
                connection.execute(
                    "INSERT INTO product_route_plans("
                    "route_plan_id,run_id,research_revision,contract_version,plan_digest,"
                    "proposal_json,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,'PROPOSED',?,?)",
                    (
                        proposal.route_plan_id,
                        proposal.run_id,
                        proposal.research_revision,
                        proposal.contract_version,
                        proposal.digest,
                        _json(value),
                        now,
                        now,
                    ),
                )
                connection.executemany(
                    "INSERT INTO product_planned_routes("
                    "route_id,route_plan_id,method,target,expected_verifier,milestones_json,"
                    "termination_condition,dependencies_json,state,priority,budget_json) "
                    "VALUES(?,?,?,?,?,?,?,?,'PROPOSED',?,?)",
                    [
                        (
                            route.route_id,
                            proposal.route_plan_id,
                            route.method,
                            route.target,
                            route.expected_verifier,
                            _json(route.milestones),
                            route.termination_condition,
                            _json(route.dependencies),
                            route.priority,
                            _json(dict(route.budget)),
                        )
                        for route in proposal.routes
                    ],
                )
            except sqlite3.IntegrityError as error:
                raise RoutePlanConflict("route or plan digest is already owned") from error
            self._append_activity(
                connection,
                run_id=proposal.run_id,
                revision=proposal.research_revision,
                event_type="ROUTE_PLAN_PROPOSED",
                route_plan_id=proposal.route_plan_id,
                route_ids=tuple(route.route_id for route in proposal.routes),
                payload={"plan_digest": proposal.digest, "route_count": len(proposal.routes)},
                now=now,
            )
            connection.commit()
        return self.get(proposal.route_plan_id)

    def apply(
        self,
        *,
        run_id: str,
        request_id: str,
        expected_revision: int,
        contract_version: int,
        action: str,
        route_plan_id: str,
        plan_digest: str | None = None,
        reason: str | None = None,
        priority: int | None = None,
        budget: Mapping[str, Any] | None = None,
    ) -> RoutePlanCommandResult:
        request = _command_value(
            run_id=run_id,
            request_id=request_id,
            expected_revision=expected_revision,
            contract_version=contract_version,
            action=action,
            route_plan_id=route_plan_id,
            plan_digest=plan_digest,
            reason=reason,
            priority=priority,
            budget=budget,
        )
        request_digest = hashlib.sha256(_json(request).encode("utf-8")).hexdigest()
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                "SELECT request_digest,result_json FROM product_route_plan_commands "
                "WHERE run_id=? AND request_id=?",
                (run_id, request_id),
            ).fetchone()
            if replay is not None:
                if str(replay[0]) != request_digest:
                    raise RoutePlanConflict("request id was reused with different content")
                result = _object(replay[1])
                connection.commit()
                return RoutePlanCommandResult(
                    request_digest=request_digest,
                    replayed=True,
                    plan=_plan_from_value(result["plan"]),
                )
            self._assert_fence(connection, run_id, expected_revision, contract_version)
            row = connection.execute(
                "SELECT run_id,research_revision,contract_version,plan_digest,state "
                "FROM product_route_plans WHERE route_plan_id=?",
                (route_plan_id,),
            ).fetchone()
            if row is None:
                raise KeyError(route_plan_id)
            if str(row[0]) != run_id:
                raise RoutePlanError("route plan belongs to another research run")
            before = str(row[4])
            if int(row[2]) != contract_version:
                raise RoutePlanCASMismatch("route plan contract binding is stale")
            if before == "PROPOSED" and int(row[1]) != expected_revision:
                raise RoutePlanCASMismatch("unapproved route proposal revision is stale")
            after = self._apply_action(
                connection,
                action=action,
                route_plan_id=route_plan_id,
                stored_digest=str(row[3]),
                before=before,
                plan_digest=plan_digest,
                reason=reason,
                priority=priority,
                budget=budget,
                now=now,
            )
            connection.execute(
                "UPDATE product_route_plans SET research_revision=? WHERE route_plan_id=?",
                (expected_revision, route_plan_id),
            )
            plan_after = self._get_in_connection(connection, route_plan_id)
            route_ids = tuple(
                str(item[0])
                for item in connection.execute(
                    "SELECT route_id FROM product_planned_routes WHERE route_plan_id=? "
                    "ORDER BY route_id",
                    (route_plan_id,),
                )
            )
            result = {
                "action": action,
                "state_before": before,
                "state_after": after,
                "route_ids": list(route_ids),
                "plan": _plan_value(plan_after),
            }
            connection.execute(
                "INSERT INTO product_route_plan_commands("
                "run_id,request_id,request_digest,route_plan_id,action,expected_revision,"
                "contract_version,result_json,applied_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    request_id,
                    request_digest,
                    route_plan_id,
                    action,
                    expected_revision,
                    contract_version,
                    _json(result),
                    now,
                ),
            )
            event_payload: dict[str, Any] = {
                "action": action,
                "state_before": before,
                "state_after": after,
            }
            if reason is not None:
                event_payload["reason"] = reason
            if priority is not None:
                event_payload["priority"] = priority
            if budget is not None:
                event_payload["budget"] = dict(budget)
            self._append_activity(
                connection,
                run_id=run_id,
                revision=expected_revision,
                event_type=f"ROUTE_PLAN_{_event_suffix(action)}",
                route_plan_id=route_plan_id,
                route_ids=route_ids,
                payload=event_payload,
                now=now,
            )
            connection.commit()
        return RoutePlanCommandResult(request_digest, False, plan_after)

    def record_hint(
        self,
        *,
        hint_id: str,
        run_id: str,
        content_artifact_id: str,
        research_revision: int,
        contract_version: int,
        route_plan_id: str | None = None,
    ) -> None:
        if not hint_id or not content_artifact_id:
            raise ValueError("hint and content artifact identities are required")
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_fence(connection, run_id, research_revision, contract_version)
            if route_plan_id is not None:
                owner = connection.execute(
                    "SELECT run_id FROM product_route_plans WHERE route_plan_id=?",
                    (route_plan_id,),
                ).fetchone()
                if owner is None or str(owner[0]) != run_id:
                    raise RoutePlanError("hint route plan is outside the research run")
            connection.execute(
                "INSERT INTO product_route_hints("
                "hint_id,run_id,route_plan_id,content_artifact_id,created_at) VALUES(?,?,?,?,?)",
                (hint_id, run_id, route_plan_id, content_artifact_id, now),
            )
            self._append_activity(
                connection,
                run_id=run_id,
                revision=research_revision,
                event_type="ROUTE_HINT_RECORDED",
                route_plan_id=route_plan_id,
                route_ids=(),
                payload={"content_artifact_id": content_artifact_id},
                now=now,
            )
            connection.commit()

    def assert_route_active(
        self, connection: sqlite3.Connection, *, run_id: str, route_id: str
    ) -> None:
        row = connection.execute(
            "SELECT rp.state,pr.state FROM product_planned_routes pr "
            "JOIN product_route_plans rp ON rp.route_plan_id=pr.route_plan_id "
            "WHERE pr.route_id=? AND rp.run_id=?",
            (route_id, run_id),
        ).fetchone()
        if row is None:
            raise KeyError(route_id)
        if tuple(str(value) for value in row) != ("ACTIVE", "ACTIVE"):
            raise RouteDerivationStopped("route is not active; no new work may be derived")

    def get(self, route_plan_id: str) -> RoutePlan:
        with self._connect() as connection:
            return self._get_in_connection(connection, route_plan_id)

    @staticmethod
    def _get_in_connection(connection: sqlite3.Connection, route_plan_id: str) -> RoutePlan:
        row = connection.execute(
            "SELECT route_plan_id,run_id,research_revision,contract_version,plan_digest,"
            "state,state_reason,created_at,updated_at FROM product_route_plans "
            "WHERE route_plan_id=?",
            (route_plan_id,),
        ).fetchone()
        if row is None:
            raise KeyError(route_plan_id)
        routes = connection.execute(
            "SELECT route_id,method,target,expected_verifier,milestones_json,"
            "termination_condition,dependencies_json,state,priority,budget_json,stop_reason "
            "FROM product_planned_routes WHERE route_plan_id=? ORDER BY priority,route_id",
            (route_plan_id,),
        ).fetchall()
        return RoutePlan(
            route_plan_id=str(row[0]),
            run_id=str(row[1]),
            research_revision=int(row[2]),
            contract_version=int(row[3]),
            plan_digest=str(row[4]),
            state=str(row[5]),
            state_reason=str(row[6]) if row[6] is not None else None,
            created_at=str(row[7]),
            updated_at=str(row[8]),
            routes=tuple(_route(item) for item in routes),
        )

    def _apply_action(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        route_plan_id: str,
        stored_digest: str,
        before: str,
        plan_digest: str | None,
        reason: str | None,
        priority: int | None,
        budget: Mapping[str, Any] | None,
        now: str,
    ) -> str:
        if action in _TRANSITIONS:
            after = _TRANSITIONS[action].get(before)
            if after is None:
                raise RoutePlanError(f"route plan transition {before}->{action} is not allowed")
            if action == "APPROVE" and plan_digest != stored_digest:
                raise RoutePlanConflict("approval digest does not match the frozen proposal")
            state_reason = reason if action in {"PAUSE", "STOP"} else None
            stop_reason = reason if action == "STOP" else None
            connection.execute(
                "UPDATE product_route_plans SET state=?,state_reason=?,updated_at=? "
                "WHERE route_plan_id=? AND state=?",
                (after, state_reason, now, route_plan_id, before),
            )
            connection.execute(
                "UPDATE product_planned_routes SET state=?,stop_reason=? WHERE route_plan_id=?",
                (after, stop_reason, route_plan_id),
            )
            return after
        if before == "STOPPED":
            raise RoutePlanError("stopped route plans are immutable")
        if action == "SET_PRIORITY":
            if priority is None or priority <= 0:
                raise ValueError("SET_PRIORITY requires a positive priority")
            connection.execute(
                "UPDATE product_planned_routes SET priority=? WHERE route_plan_id=?",
                (priority, route_plan_id),
            )
        elif action == "SET_BUDGET":
            if budget is None:
                raise ValueError("SET_BUDGET requires an explicit budget")
            connection.execute(
                "UPDATE product_planned_routes SET budget_json=? WHERE route_plan_id=?",
                (_json(_budget(budget)), route_plan_id),
            )
        else:
            raise ValueError(f"unknown route plan action: {action}")
        connection.execute(
            "UPDATE product_route_plans SET updated_at=? WHERE route_plan_id=?",
            (now, route_plan_id),
        )
        return before

    def _assert_fence(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        expected_revision: int,
        contract_version: int,
    ) -> None:
        actual = self._run_fence_reader(connection, run_id)
        if actual != RunFence(expected_revision, contract_version):
            raise RoutePlanCASMismatch(
                f"research fence is revision {actual.research_revision}, "
                f"contract {actual.contract_version}"
            )

    def _append_activity(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        revision: int,
        event_type: str,
        route_plan_id: str | None,
        route_ids: Sequence[str],
        payload: Mapping[str, Any],
        now: str,
    ) -> None:
        refs: dict[str, Any] = {"route_ids": list(route_ids)}
        if route_plan_id is not None:
            refs["route_plan_id"] = route_plan_id
        self._activities.append_in_transaction(
            connection,
            ProductActivity(
                event_id=self._ids(),
                scope_kind="RUN",
                run_id=run_id,
                source="ORCHESTRATOR",
                research_revision=revision,
                entity_refs=refs,
                payload={"type": event_type, **dict(payload)},
                recorded_at=now,
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(
            self._db_path,
            timeout=self._busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection


def sqlite_run_fence(connection: sqlite3.Connection, run_id: str) -> RunFence:
    row = connection.execute(
        "SELECT revision,current_contract_version FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise KeyError(run_id)
    return RunFence(int(row[0]), int(row[1]))


def _command_value(**values: Any) -> dict[str, Any]:
    action = values["action"]
    if action not in _ACTIONS:
        raise ValueError(f"unknown route plan action: {action}")
    required = {
        "APPROVE": ("plan_digest",),
        "PAUSE": ("reason",),
        "STOP": ("reason",),
        "SET_PRIORITY": ("priority",),
        "SET_BUDGET": ("budget",),
    }.get(action, ())
    if any(values[name] is None or values[name] == "" for name in required):
        raise ValueError(f"{action} is missing required action data")
    allowed = {
        "APPROVE": {"plan_digest"},
        "START": set(),
        "PAUSE": {"reason"},
        "STOP": {"reason"},
        "SET_PRIORITY": {"priority"},
        "SET_BUDGET": {"budget"},
    }[action]
    optional = {"plan_digest", "reason", "priority", "budget"}
    if any(values[name] is not None for name in optional - allowed):
        raise ValueError(f"{action} received fields owned by another action")
    if not values["run_id"] or not values["request_id"] or not values["route_plan_id"]:
        raise ValueError("route command identities are required")
    if values["expected_revision"] < 0 or values["contract_version"] <= 0:
        raise ValueError("route command fence is invalid")
    if values["priority"] is not None and values["priority"] <= 0:
        raise ValueError("priority must be positive")
    if values["budget"] is not None:
        values["budget"] = _budget(values["budget"])
    return {key: value for key, value in values.items() if value is not None}


def _proposal_value(proposal: RoutePlanProposal) -> dict[str, Any]:
    return {
        "route_plan_id": proposal.route_plan_id,
        "run_id": proposal.run_id,
        "research_revision": proposal.research_revision,
        "contract_version": proposal.contract_version,
        "routes": [
            {
                "route_id": route.route_id,
                "method": route.method,
                "target": route.target,
                "expected_verifier": route.expected_verifier,
                "milestones": list(route.milestones),
                "termination_condition": route.termination_condition,
                "dependencies": list(route.dependencies),
                "priority": route.priority,
                "budget": _budget(route.budget),
            }
            for route in proposal.routes
        ],
    }


def _plan_value(plan: RoutePlan) -> dict[str, Any]:
    return {
        "route_plan_id": plan.route_plan_id,
        "run_id": plan.run_id,
        "research_revision": plan.research_revision,
        "contract_version": plan.contract_version,
        "plan_digest": plan.plan_digest,
        "state": plan.state,
        "state_reason": plan.state_reason,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
        "routes": [
            {
                "route_id": route.route_id,
                "method": route.method,
                "target": route.target,
                "expected_verifier": route.expected_verifier,
                "milestones": list(route.milestones),
                "termination_condition": route.termination_condition,
                "dependencies": list(route.dependencies),
                "state": route.state,
                "priority": route.priority,
                "budget": dict(route.budget),
                "stop_reason": route.stop_reason,
            }
            for route in plan.routes
        ],
    }


def _plan_from_value(value: Any) -> RoutePlan:
    item = dict(value)
    return RoutePlan(
        route_plan_id=str(item["route_plan_id"]),
        run_id=str(item["run_id"]),
        research_revision=int(item["research_revision"]),
        contract_version=int(item["contract_version"]),
        plan_digest=str(item["plan_digest"]),
        state=str(item["state"]),
        state_reason=str(item["state_reason"]) if item["state_reason"] is not None else None,
        created_at=str(item["created_at"]),
        updated_at=str(item["updated_at"]),
        routes=tuple(_route_from_value(route) for route in item["routes"]),
    )


def _route(row: Sequence[Any]) -> PlannedRoute:
    return PlannedRoute(
        route_id=str(row[0]),
        method=str(row[1]),
        target=str(row[2]),
        expected_verifier=str(row[3]),
        milestones=_strings(row[4]),
        termination_condition=str(row[5]),
        dependencies=_strings(row[6]),
        state=str(row[7]),
        priority=int(row[8]),
        budget=_object(row[9]),
        stop_reason=str(row[10]) if row[10] is not None else None,
    )


def _route_from_value(value: Any) -> PlannedRoute:
    item = dict(value)
    return PlannedRoute(
        route_id=str(item["route_id"]),
        method=str(item["method"]),
        target=str(item["target"]),
        expected_verifier=str(item["expected_verifier"]),
        milestones=tuple(str(entry) for entry in item["milestones"]),
        termination_condition=str(item["termination_condition"]),
        dependencies=tuple(str(entry) for entry in item["dependencies"]),
        state=str(item["state"]),
        priority=int(item["priority"]),
        budget=dict(item["budget"]),
        stop_reason=str(item["stop_reason"]) if item["stop_reason"] is not None else None,
    )


def _budget(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    if not result:
        raise ValueError("route budget must be explicit")
    if any(
        not isinstance(key, str)
        or not key
        or isinstance(item, bool)
        or not isinstance(item, (int, float))
        or item < 0
        for key, item in result.items()
    ):
        raise ValueError("route budget values must be non-negative numbers")
    return result


def _strings(value: Any) -> tuple[str, ...]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise RoutePlanError("stored route identity list is invalid")
    return tuple(parsed)


def _object(value: Any) -> dict[str, Any]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise RoutePlanError("stored route JSON is not an object")
    return parsed


def _event_suffix(action: str) -> str:
    return {
        "APPROVE": "APPROVED",
        "START": "STARTED",
        "PAUSE": "PAUSED",
        "STOP": "STOPPED",
        "SET_PRIORITY": "PRIORITY_SET",
        "SET_BUDGET": "BUDGET_SET",
    }[action]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "PlannedRoute",
    "RouteDerivationStopped",
    "RoutePlan",
    "RoutePlanCASMismatch",
    "RoutePlanCommandResult",
    "RoutePlanConflict",
    "RoutePlanError",
    "RoutePlanProposal",
    "RoutePlanStore",
    "RouteProposal",
    "RunFence",
    "sqlite_run_fence",
]
