"""Host-owned execution, attestation, and accounting behind one small interface."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from rk.adapters.base import canonical_json_sha256
from rk.domain import VerifiedCapability
from rk.runtime import format_utc, parse_utc
from rk.storage import SQLiteStorage, StorageConflict
from rk.strategy import StrategyRunner, ToolInvocation


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class HostExecution:
    invocation: ToolInvocation
    receipt_id: str
    receipt_nonce: str
    receipt: Mapping[str, Any]


class HostExecutionNotAuthoritative(StorageConflict):
    """External work was durably accounted but cannot grant mathematical authority."""

    def __init__(
        self,
        receipt_id: str,
        reasons: tuple[str, ...],
        invocation: ToolInvocation,
    ) -> None:
        self.receipt_id = receipt_id
        self.reasons = reasons
        self.invocation = invocation
        super().__init__(f"host execution is non-authoritative: {', '.join(reasons)}")


class HostExecutionReceiptService:
    """Execute a bound attempt and freeze its real scope without caller-authored context.

    The signing key is read once into this object and is never placed in policy, a request,
    an adapter, or SQLite.  The public interface deliberately accepts no claim, version,
    digest, environment profile, binding, or receipt fields: all are derived from live state.
    """

    def __init__(
        self,
        *,
        storage: SQLiteStorage,
        strategy: StrategyRunner,
        signing_key_path: Path,
        capability: VerifiedCapability,
        id_generator: Any,
        clock: Any,
        host_profiles: Mapping[str, Mapping[str, Any]],
        budget_limits: Mapping[str, int] | None = None,
        revoked_capability_ids: frozenset[str] = frozenset(),
    ) -> None:
        key_path = Path(signing_key_path).resolve()
        key = key_path.read_bytes()
        if len(key) < 32:
            raise ValueError("host receipt signing key must contain at least 32 bytes")
        if os.name == "posix" and key_path.stat().st_mode & 0o077:
            raise PermissionError("host receipt signing key must not be group/world accessible")
        if not capability.allows("HostExecute"):
            raise ValueError("host capability must allow HostExecute")
        self._storage = storage
        self._strategy = strategy
        self._key = key
        self._capability = capability
        self._ids = id_generator
        self._clock = clock
        self._profiles = {str(name): dict(value) for name, value in host_profiles.items()}
        self._budget_limits = dict(budget_limits or {})
        self._revoked_capability_ids = frozenset(revoked_capability_ids)
        self._instance_id = secrets.token_hex(32)
        # A new host instance only recovers claims whose adapter deadline has elapsed.
        # A live provider call owned by another process remains untouched.
        self._ensure_capability_temporally_valid(self._clock.now())
        self.recover_incomplete()

    def execute(
        self, *, run_id: str, attempt_id: str, request: Mapping[str, Any]
    ) -> HostExecution:
        now_value = self._clock.now()
        self._ensure_capability_valid(run_id, now_value)
        started_at = format_utc(now_value)
        scope = self._storage.host_execution_scope(
            run_id=run_id, attempt_id=attempt_id, now=started_at
        )
        adapter_name = str(scope["adapter_name"])
        profile = self._profiles.get(adapter_name)
        if profile is None:
            raise StorageConflict("binding adapter has no host-owned execution profile")
        self._validate_profile(scope, profile)
        request = self._host_owned_request(adapter_name, profile, request)
        required_budget_resources = ["WALL_SECOND"]
        if profile.get("token_meter_applicable") is True:
            required_budget_resources.extend(("INPUT_TOKEN", "OUTPUT_TOKEN"))
        self._storage.preflight_host_budget(
            run_id=run_id,
            attempt_id=attempt_id,
            required_resources=tuple(required_budget_resources),
            budget_limits=self._budget_limits,
        )
        actual = self._actual_environment(request)
        request_hash = canonical_json_sha256(request)
        claim_token = secrets.token_hex(32)
        recovery_timeout = profile.get("recovery_timeout_seconds", 900)
        if (
            not isinstance(recovery_timeout, (int, float))
            or isinstance(recovery_timeout, bool)
            or recovery_timeout <= 0
        ):
            raise StorageConflict("host recovery timeout must be positive")
        recover_after = format_utc(now_value + timedelta(seconds=float(recovery_timeout)))
        self._storage.claim_host_execution(
            scope=scope,
            capability=self._capability,
            claim_token=claim_token,
            request_hash=request_hash,
            component=str(profile.get("component", adapter_name)),
            service_instance_id=self._instance_id,
            now=started_at,
            recover_after=recover_after,
        )
        heartbeat_stop = threading.Event()
        heartbeat_period = max(0.05, min(float(recovery_timeout) / 3.0, 30.0))
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(claim_token, float(recovery_timeout), heartbeat_period, heartbeat_stop),
            name=f"rk-host-heartbeat-{str(scope['attempt_id'])[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            return self._execute_claimed(
                scope=scope,
                profile=profile,
                adapter_name=adapter_name,
                request=request,
                request_hash=request_hash,
                actual=actual,
                claim_token=claim_token,
            )
        except BaseException:
            # If Python still has control, close the ambiguous crash window immediately.
            # A hard process death is recovered on the next service start after its deadline.
            with suppress(Exception):
                self._storage.recover_incomplete_host_claims(
                    capability=self._capability,
                    now=format_utc(self._clock.now()), claim_token=claim_token, force=True
                )
            raise
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=max(1.0, heartbeat_period * 2.0))

    def _execute_claimed(
        self,
        *,
        scope: Mapping[str, Any],
        profile: Mapping[str, Any],
        adapter_name: str,
        request: Mapping[str, Any],
        request_hash: str,
        actual: Mapping[str, Any],
        claim_token: str,
    ) -> HostExecution:
        input_snapshot = {
            "run_id": scope["run_id"],
            "route_id": scope["route_id"],
            "attempt_id": scope["attempt_id"],
            "binding_id": scope["binding_id"],
            "claim_id": scope["claim_id"],
            "contract_version": scope["contract_version"],
            "statement_hash": scope["statement_hash"],
            "registered_input_snapshot_digest": scope["input_snapshot_digest"],
            "invocation_artifact_sha256": scope["invocation_artifact_sha256"],
            "request_hash": request_hash,
        }
        environment_digest = canonical_json_sha256(actual)
        mount_digest = canonical_json_sha256(self._actual_mounts(profile))
        dependency_digest = self._dependency_closure_digest(profile)
        if adapter_name == "lean-replay" and dependency_digest is None:
            raise StorageConflict("Lean replay requires a pinned dependency closure digest")
        process_digest = canonical_json_sha256(
            {
                "host_pid": os.getpid(),
                "python_executable": str(Path(sys.executable).resolve()),
                "python_sha256": _sha256_file(Path(sys.executable).resolve()),
                "adapter_name": adapter_name,
                "adapter_version": scope["adapter_version"],
                "registered_process": profile.get("process", {}),
            }
        )
        binary_sha = self._actual_binary_sha(profile)
        tool_digest = canonical_json_sha256(
            {
                "toolchain": profile.get("toolchain"),
                "source_commit": scope.get("source_commit"),
                "binary_sha256": binary_sha,
            }
        )
        invocation = self._strategy.invoke(adapter_name, request)
        post_call_blocks: list[str] = []
        if dependency_digest is not None:
            try:
                if self._dependency_closure_digest(profile) != dependency_digest:
                    raise StorageConflict("dependency closure changed during invocation")
            except StorageConflict as error:
                post_call_blocks.append(f"DEPENDENCY_VALIDATION_FAILED:{error}")
        try:
            source_sha256 = self._artifact_digest(
                profile, invocation.result, "source_sha256", "source_result_path"
            )
            output_sha256 = self._artifact_digest(
                profile, invocation.result, "output_sha256", "output_result_path"
            )
        except StorageConflict as error:
            post_call_blocks.append(f"ARTIFACT_VALIDATION_FAILED:{error}")
            source_sha256 = None
            output_sha256 = None
        # Re-read inside the recording transaction.  Any claim/version/route/attempt drift
        # during the external call makes the result non-authoritative and leaves no receipt.
        receipt_id = self._ids.new()
        nonce = secrets.token_hex(32)
        usage = dict(invocation.usage)
        usage["component"] = str(profile.get("component", adapter_name))
        usage["token_meter_applicable"] = profile.get("token_meter_applicable") is True
        usage["currency_meter_applicable"] = (
            profile.get("currency_meter_applicable") is True
        )
        payload = {
            "schema_version": "rk.host_execution_receipt.v1",
            "receipt_id": receipt_id,
            "receipt_nonce": nonce,
            "service_instance_id": self._instance_id,
            **{key: scope[key] for key in (
                "run_id", "route_id", "attempt_id", "binding_id", "claim_id",
                "contract_version", "statement_hash", "environment_profile_id",
                "adapter_name", "adapter_version", "source_commit",
            )},
            "toolchain": profile.get("toolchain"),
            "binary_sha256": binary_sha,
            "request_hash": request_hash,
            "result_hash": invocation.result_hash,
            "source_sha256": source_sha256,
            "output_sha256": output_sha256,
            "input_snapshot_digest": canonical_json_sha256(input_snapshot),
            "environment_digest": environment_digest,
            "mount_digest": mount_digest,
            "dependency_closure_digest": dependency_digest,
            "process_digest": process_digest,
            "tool_digest": tool_digest,
            "status": invocation.status,
            "exit_code": invocation.result.get("exit_code"),
            "wall_time_ms": invocation.wall_time_ms,
            "provider_usage": usage,
        }
        now = format_utc(self._clock.now())
        authority_block_reasons: tuple[str, ...] = tuple(post_call_blocks)
        exit_code = invocation.result.get("exit_code")
        if invocation.status != "COMPLETED" or (
            exit_code is not None and exit_code != 0
        ):
            authority_block_reasons = (
                *authority_block_reasons,
                f"EXECUTION_NOT_SUCCESSFUL:{invocation.status}:{exit_code}",
            )
        authority_result: Mapping[str, Any] = {}
        try:
            authority_result = self._authority_result(
                adapter_name, profile, invocation.result
            )
        except StorageConflict as error:
            authority_block_reasons = (
                *authority_block_reasons,
                f"AUTHORITY_RESULT_INVALID:{error}",
            )
        payload["kernel_verdict"] = authority_result.get("kernel_verdict")
        payload["axiom_dependencies"] = authority_result.get("axiom_dependencies")
        payload["declaration_audit"] = authority_result.get("declaration_audit")
        payload["declaration_module"] = authority_result.get("declaration_module")
        payload["declaration_type_digest"] = authority_result.get(
            "declaration_type_digest"
        )
        signature = hmac.new(self._key, _canonical_bytes(payload), hashlib.sha256).hexdigest()
        receipt = {"payload": payload, "signature": signature}
        try:
            self._ensure_capability_valid(str(scope["run_id"]), self._clock.now())
        except StorageConflict:
            authority_block_reasons = (
                *authority_block_reasons,
                "HOST_CAPABILITY_INVALID_AFTER_CALL",
            )
        block_reasons = self._storage.record_host_execution_atomic(
            scope=scope,
            expected_scope=scope,
            receipt=receipt,
            capability=self._capability,
            command_id=self._ids.new(),
            event_id=self._ids.new(),
            trace_id=self._ids.new(),
            now=now,
            budget_limits=self._budget_limits,
            claim_token=claim_token,
            authority_block_reasons=authority_block_reasons,
        )
        if block_reasons:
            # The receipt and every actual/unknown usage row are already committed.  Raising
            # only stops downstream authority; it never erases money or tokens already spent.
            raise HostExecutionNotAuthoritative(receipt_id, block_reasons, invocation)
        return HostExecution(invocation, receipt_id, nonce, receipt)

    def verify_receipt(self, receipt_id: str) -> Mapping[str, Any]:
        """Verify the persisted HMAC and every duplicated signed column before trust use."""

        profile = self._storage.host_receipt_profile(receipt_id)
        payload = profile.get("payload")
        signature = profile.get("signature")
        if not isinstance(payload, Mapping) or not isinstance(signature, str):
            raise StorageConflict("persisted host receipt is malformed")
        expected_signature = hmac.new(
            self._key, _canonical_bytes(payload), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise StorageConflict("persisted host receipt signature is invalid")
        signed_columns = (
            "receipt_id", "receipt_nonce", "service_instance_id", "run_id", "route_id",
            "attempt_id", "binding_id", "claim_id", "contract_version", "statement_hash",
            "environment_profile_id", "adapter_name", "adapter_version", "source_commit",
            "toolchain", "binary_sha256", "request_hash", "result_hash", "source_sha256",
            "output_sha256", "input_snapshot_digest", "environment_digest", "mount_digest",
            "process_digest", "tool_digest", "status", "exit_code", "wall_time_ms",
            "dependency_closure_digest",
        )
        if any(profile.get(name) != payload.get(name) for name in signed_columns):
            raise StorageConflict("persisted host receipt columns do not match signed payload")
        if profile.get("provider_usage") != payload.get("provider_usage"):
            raise StorageConflict("persisted host usage does not match signed payload")
        return profile

    @staticmethod
    def _authority_result(
        adapter_name: str,
        profile: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if adapter_name != "lean-replay":
            if profile.get("authority_mode") != "CHECKER_CERTIFICATE":
                return {}
            if profile.get("capability_kind") not in {"SMT", "EXACT_ENUMERATION"}:
                raise StorageConflict("only SMT or exact enumeration may issue certificates")
            if result.get("status") != "COMPLETED" or result.get("exit_code") != 0:
                raise StorageConflict("deterministic checker did not complete successfully")
            if result.get("payload") != profile.get("expected_result"):
                raise StorageConflict("deterministic checker result is outside the host profile")
            if result.get("trust_limit") != "HOST_CHECKED_CERTIFICATE":
                raise StorageConflict("deterministic checker did not return a certificate result")
            return {
                "kernel_verdict": "CERTIFICATE_PASS",
                "axiom_dependencies": [],
            }
        if result.get("status") != "COMPLETED" or result.get("exit_code") != 0:
            raise StorageConflict("Lean replay did not complete successfully")
        if result.get("kernel_verdict") != "REPLAY_PASS":
            raise StorageConflict("Lean replay did not return a kernel pass verdict")
        axioms = result.get("axiom_dependencies")
        allowed = profile.get("allowed_axioms")
        if (
            not isinstance(axioms, list)
            or not all(isinstance(item, str) for item in axioms)
            or not isinstance(allowed, list)
            or not all(isinstance(item, str) for item in allowed)
            or set(axioms) - set(allowed)
        ):
            raise StorageConflict("Lean replay returned untrusted axiom dependencies")
        if not isinstance(result.get("source_sha256"), str) or not isinstance(
            result.get("output_sha256"), str
        ):
            raise StorageConflict("Lean replay omitted source or output digest")
        declarations = result.get("declaration_audit")
        declaration_module = result.get("declaration_module")
        type_digest = result.get("declaration_type_digest")
        expected_types = profile.get("expected_declaration_types")
        expected_module = profile.get("expected_declaration_module")
        if (
            not isinstance(declarations, Mapping)
            or not declarations
            or not all(
                isinstance(item, Mapping)
                and item.get("owner") == "target_module"
                and isinstance(item.get("type"), str)
                and bool(item.get("type"))
                for item in declarations.values()
            )
            or not isinstance(type_digest, str)
            or len(type_digest) != 64
            or not isinstance(expected_types, Mapping)
            or not expected_types
            or not all(
                isinstance(name, str) and isinstance(rendered_type, str)
                for name, rendered_type in expected_types.items()
            )
            or declaration_module != expected_module
        ):
            raise StorageConflict("Lean replay did not bind local declaration types")
        actual_types = {
            str(name): str(item["type"])
            for name, item in declarations.items()
            if isinstance(item, Mapping)
        }
        if actual_types != dict(expected_types):
            raise StorageConflict("Lean declaration types do not match the host profile")
        if canonical_json_sha256(declarations) != type_digest:
            raise StorageConflict("Lean declaration type digest is inconsistent")
        return {
            "kernel_verdict": "REPLAY_PASS",
            "axiom_dependencies": sorted(axioms),
            "declaration_audit": declarations,
            "declaration_module": declaration_module,
            "declaration_type_digest": type_digest,
        }

    def recover_incomplete(self) -> tuple[str, ...]:
        """Mark crash-window calls UNKNOWN/FUSED; callers must register a new attempt."""

        self._ensure_capability_temporally_valid(self._clock.now())
        return self._storage.recover_incomplete_host_claims(
            capability=self._capability, now=format_utc(self._clock.now())
        )

    def consume_lean_replay(self, *, receipt_id: str) -> str:
        """Consume one successful Lean receipt after its source/output entered the run CAS."""

        profile = self.verify_receipt(receipt_id)
        payload = profile["payload"]
        self._ensure_capability_valid(str(payload["run_id"]), self._clock.now())
        signature = str(profile["signature"])
        if profile.get("adapter_name") != "lean-replay":
            raise StorageConflict("receipt is not a Lean replay")
        verifier = self._profiles.get("lean-replay")
        if verifier is None:
            raise StorageConflict("Lean host profile is unavailable")
        if profile.get("dependency_closure_digest") != verifier.get(
            "dependency_closure_sha256"
        ):
            raise StorageConflict("Lean dependency closure is not the registered closure")
        current_closure_digest = self._dependency_closure_digest(verifier)
        if current_closure_digest != profile.get("dependency_closure_digest"):
            raise StorageConflict("Lean dependency closure drifted before receipt consumption")
        self._authority_result("lean-replay", verifier, payload)
        feedback_id = str(self._ids.new())
        now = format_utc(self._clock.now())
        self._storage.consume_host_lean_receipt_atomic(
            receipt_id=receipt_id,
            feedback_id=feedback_id,
            capability=self._capability,
            command_id=self._ids.new(),
            event_id=self._ids.new(),
            trace_id=self._ids.new(),
            now=now,
            toolchain=str(verifier.get("toolchain", "")),
            expected_signature=signature,
            expected_payload_json=_canonical_bytes(payload).decode("utf-8"),
        )
        return feedback_id

    def consume_checker_result(self, *, receipt_id: str) -> str:
        """Consume one host-pinned SMT/enumeration certificate into the Claim graph."""

        profile = self.verify_receipt(receipt_id)
        payload = profile["payload"]
        self._ensure_capability_valid(str(payload["run_id"]), self._clock.now())
        adapter_name = str(profile.get("adapter_name"))
        verifier = self._profiles.get(adapter_name)
        if verifier is None or verifier.get("authority_mode") != "CHECKER_CERTIFICATE":
            raise StorageConflict("host checker profile is unavailable")
        if payload.get("kernel_verdict") != "CERTIFICATE_PASS":
            raise StorageConflict("host checker receipt has no certificate verdict")
        verification_id = str(self._ids.new())
        self._storage.consume_host_checker_receipt_atomic(
            receipt_id=receipt_id,
            verification_id=verification_id,
            capability=self._capability,
            command_id=self._ids.new(),
            event_id=self._ids.new(),
            trace_id=self._ids.new(),
            now=format_utc(self._clock.now()),
            expected_signature=str(profile["signature"]),
            expected_payload_json=_canonical_bytes(payload).decode("utf-8"),
            root_kind=(
                "ENUMERATION"
                if verifier.get("capability_kind") == "EXACT_ENUMERATION"
                else "CHECKER"
            ),
        )
        return verification_id

    def _ensure_capability_valid(self, run_id: str, now_value: Any) -> None:
        if (
            not self._capability.allows("HostExecute", run_id)
        ):
            raise StorageConflict("host capability is outside this run scope")
        self._ensure_capability_temporally_valid(now_value)

    def _ensure_capability_temporally_valid(self, now_value: Any) -> None:
        if (
            self._capability.capability_id in self._revoked_capability_ids
            or not parse_utc(self._capability.issued_at)
            <= now_value
            < parse_utc(self._capability.expires_at)
        ):
            raise StorageConflict("host capability is outside this run scope")

    def _heartbeat_loop(
        self,
        claim_token: str,
        recovery_timeout: float,
        heartbeat_period: float,
        stop: threading.Event,
    ) -> None:
        while not stop.wait(heartbeat_period):
            now_value = self._clock.now()
            if not self._storage.heartbeat_host_execution(
                claim_token=claim_token,
                service_instance_id=self._instance_id,
                now=format_utc(now_value),
                recover_after=format_utc(
                    now_value + timedelta(seconds=recovery_timeout)
                ),
            ):
                return

    @staticmethod
    def _actual_environment(request: Mapping[str, Any]) -> Mapping[str, Any]:
        supplied = request.get("environment")
        if not isinstance(supplied, Mapping):
            return {"names": [], "values_sha256": canonical_json_sha256({})}
        rendered = {str(key): str(value) for key, value in supplied.items()}
        return {
            "names": sorted(rendered),
            "values_sha256": canonical_json_sha256(rendered),
        }

    @staticmethod
    def _actual_binary_sha(profile: Mapping[str, Any]) -> str | None:
        raw = profile.get("binary_path")
        if raw is None:
            return None
        path = Path(str(raw)).resolve()
        if not path.is_file():
            raise StorageConflict("host profile binary is missing")
        digest = _sha256_file(path)
        expected = profile.get("binary_sha256")
        if expected is not None and digest != expected:
            raise StorageConflict("host profile binary digest drifted")
        return str(digest)

    @staticmethod
    def _actual_mounts(profile: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = profile.get("mounts", {})
        if not isinstance(raw, Mapping):
            raise StorageConflict("host profile mounts must be an object")
        result: dict[str, Any] = {}
        for name, value in raw.items():
            path = Path(str(value)).resolve()
            if not path.exists():
                raise StorageConflict(f"host profile mount is missing: {name}")
            stat = path.stat()
            result[str(name)] = {
                "resolved_path": str(path),
                "device": int(stat.st_dev),
                "inode": int(stat.st_ino),
                "mode": int(stat.st_mode),
            }
        return result

    @staticmethod
    def _dependency_closure_digest(profile: Mapping[str, Any]) -> str | None:
        raw = profile.get("dependency_closure_root")
        expected = profile.get("dependency_closure_sha256")
        if raw is None:
            if expected is not None:
                raise StorageConflict("dependency closure root is missing")
            return None
        root = Path(str(raw)).resolve()
        if not root.is_dir():
            raise StorageConflict("dependency closure root is missing")
        manifest_raw = profile.get("dependency_closure_manifest_path")
        if manifest_raw is None:
            raise StorageConflict("dependency closure manifest is missing")
        manifest_path = Path(str(manifest_raw)).resolve()
        manifest_sha256 = profile.get("dependency_closure_manifest_sha256")
        try:
            actual_manifest_sha256 = _sha256_file(manifest_path)
        except OSError as error:
            raise StorageConflict("dependency closure manifest is unreadable") from error
        if (
            not isinstance(manifest_sha256, str)
            or actual_manifest_sha256 != manifest_sha256
        ):
            raise StorageConflict("dependency closure manifest digest drifted")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StorageConflict("dependency closure manifest is unreadable") from error
        if manifest.get("schema_version") != "rk.mathlib_closure_anchor.v1":
            raise StorageConflict("dependency closure manifest schema is not trusted")
        if manifest.get("dependency_root_relpath") != ".lake":
            raise StorageConflict("dependency closure manifest root is not trusted")
        if manifest.get("dependency_closure_sha256") != expected:
            raise StorageConflict("dependency closure manifest does not match the host profile")
        if manifest.get("mathlib_commit") != profile.get("source_commit"):
            raise StorageConflict("dependency closure manifest commit drifted")
        if manifest.get("toolchain") != profile.get("toolchain"):
            raise StorageConflict("dependency closure manifest toolchain drifted")
        anchored_raw = manifest.get("olean_files")
        if not isinstance(anchored_raw, Mapping) or not anchored_raw:
            raise StorageConflict("dependency closure manifest has no anchored files")
        anchored = {str(key): str(value) for key, value in anchored_raw.items()}
        digest = hashlib.sha256()
        files = sorted(
            (path for path in root.rglob("*.olean") if path.is_file()),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        if not files:
            raise StorageConflict("dependency closure contains no olean files")
        actual_paths: set[str] = set()
        for path in files:
            relative_text = path.relative_to(root).as_posix()
            actual_paths.add(relative_text)
            if relative_text not in anchored:
                continue
            file_digest = _sha256_file(path)
            if file_digest != anchored[relative_text]:
                raise StorageConflict("anchored dependency object digest drifted")
            relative = relative_text.encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(bytes.fromhex(file_digest))
        if set(anchored) - actual_paths:
            raise StorageConflict("anchored dependency object is missing")
        if actual_paths - set(anchored):
            raise StorageConflict("untrusted dependency object is present")
        actual = digest.hexdigest()
        if not isinstance(expected, str) or actual != expected:
            raise StorageConflict("dependency closure digest drifted")
        return actual

    @staticmethod
    def _host_owned_request(
        adapter_name: str,
        profile: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Pin authority-sensitive subprocess environment to the host profile."""

        if adapter_name not in {"lean-replay", "jixia"}:
            return request
        expected = profile.get("execution_environment")
        supplied = request.get("environment")
        if not isinstance(expected, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in expected.items()
        ):
            raise StorageConflict("host execution environment is not pinned")
        if not isinstance(supplied, Mapping) or dict(supplied) != dict(expected):
            raise StorageConflict("request environment does not match the host profile")
        return {**dict(request), "environment": dict(expected)}

    @staticmethod
    def _artifact_digest(
        profile: Mapping[str, Any],
        result: Mapping[str, Any],
        digest_name: str,
        profile_path_name: str,
    ) -> str | None:
        raw_path = profile.get(profile_path_name)
        if raw_path is None:
            value = result.get(digest_name)
            return str(value) if isinstance(value, str) else None
        path = Path(str(raw_path)).resolve()
        if not path.is_file():
            raise StorageConflict(f"host receipt artifact is missing: {profile_path_name}")
        actual = _sha256_file(path)
        reported = result.get(digest_name)
        if reported is not None and reported != actual:
            raise StorageConflict(f"adapter reported false {digest_name}")
        return actual

    @staticmethod
    def _validate_profile(scope: Mapping[str, Any], profile: Mapping[str, Any]) -> None:
        checks = {
            "adapter_version": scope["adapter_version"],
            "environment_profile_id": scope["environment_profile_id"],
            "source_commit": scope.get("source_commit"),
        }
        if any(profile.get(key) != value for key, value in checks.items()):
            raise StorageConflict("host execution profile does not match the bound attempt")
        if (
            scope.get("adapter_name") == "lean-replay"
            or profile.get("authority_mode") == "CHECKER_CERTIFICATE"
        ) and profile.get("expected_statement_hash") != scope.get("statement_hash"):
            raise StorageConflict("formal/checker statement is not pinned to this claim")


__all__ = ["HostExecution", "HostExecutionNotAuthoritative", "HostExecutionReceiptService"]
