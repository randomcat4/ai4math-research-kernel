"""Single-organization product identities with frozen narrow role profiles."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType


class ProductRole(StrEnum):
    MAIN = "MAIN"
    WORKER = "WORKER"
    REVIEWER = "REVIEWER"
    ADMIN = "ADMIN"


_ROLE_ACTIONS: Mapping[ProductRole, frozenset[str]] = MappingProxyType(
    {
        ProductRole.MAIN: frozenset(
            {
                "create",
                "FreezeContract",
                "AmendContract",
                "StartRun",
                "Interrupt",
                "Resume",
                "CancelRun",
                "ApplyRoutePlan",
                "SubmitGuidance",
                "WithdrawGuidance",
                "ConfirmRevoke",
                "RegisterBridgeSpec",
                "SubmitClosureWitness",
                "CreateReviewTask",
                "GenerateCandidateTex",
                "Finalize",
                "ExportDossier",
            }
        ),
        ProductRole.WORKER: frozenset(
            {
                "RegisterClaim",
                "ReviseClaim",
                "CreateComputeTask",
                "RunTool",
                "CancelJob",
                "SubmitWorkerCheckpoint",
            }
        ),
        ProductRole.REVIEWER: frozenset(
            {
                "ClaimReviewTask",
                "SubmitAtomicReview",
                "SubmitCompositionReview",
                "SubmitPaperReview",
                "ReviewTheoremApplicability",
                "ReviewMaterialExtraction",
                "ReviewProblemCandidate",
            }
        ),
        ProductRole.ADMIN: frozenset(
            {
                "DeploymentOperation",
                "ConfigureDeployment",
                "ProbeCapability",
                "BackupDeployment",
                "RestoreDeployment",
                "UpgradePreflight",
                "ManageIdentity",
                "ReadDiagnostics",
            }
        ),
    }
)


class IdentityAuthenticationError(PermissionError):
    """Identity credentials are invalid or the identity is disabled."""


class IdentityConflict(RuntimeError):
    """An immutable identity field was reused with different content."""


@dataclass(frozen=True, slots=True)
class ProductIdentity:
    identity_id: str
    subject_id: str
    display_name: str
    role: ProductRole
    capability_id: str
    enabled: bool
    created_at: str
    disabled_at: str | None

    @property
    def allowed_actions(self) -> frozenset[str]:
        return _ROLE_ACTIONS[self.role]


class IdentityStore:
    """Host-managed identities for one configured organization."""

    def __init__(
        self,
        db_path: Path,
        salt_generator: Callable[[], bytes],
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._salts = salt_generator
        self._busy_timeout_ms = busy_timeout_ms

    def register(
        self,
        *,
        identity_id: str,
        subject_id: str,
        display_name: str,
        role: ProductRole,
        capability_id: str,
        login_secret: str,
        now: str,
    ) -> ProductIdentity:
        _nonempty(identity_id, subject_id, display_name, capability_id)
        if len(login_secret) < 16:
            raise ValueError("login secret must contain at least 16 characters")
        salt = self._salts()
        if len(salt) != 16:
            raise ValueError("identity salt generator must return exactly 16 bytes")
        digest = _credential_digest(login_secret, salt)
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO product_identities("
                    "identity_id,subject_id,display_name,role,capability_id,credential_salt,"
                    "credential_digest,enabled,created_at) VALUES(?,?,?,?,?,?,?,1,?)",
                    (
                        identity_id,
                        subject_id,
                        display_name,
                        role.value,
                        capability_id,
                        salt,
                        digest,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise IdentityConflict("identity, subject, or capability already exists") from error
        return self.get(identity_id)

    def get(self, identity_id: str) -> ProductIdentity:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT identity_id,subject_id,display_name,role,capability_id,enabled,"
                "created_at,disabled_at FROM product_identities WHERE identity_id=?",
                (identity_id,),
            ).fetchone()
        if row is None:
            raise KeyError(identity_id)
        return _identity(row)

    def authenticate(self, identity_id: str, login_secret: str) -> ProductIdentity:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT identity_id,subject_id,display_name,role,capability_id,enabled,"
                "created_at,disabled_at,credential_salt,credential_digest "
                "FROM product_identities WHERE identity_id=?",
                (identity_id,),
            ).fetchone()
        if row is None or not bool(row[5]):
            raise IdentityAuthenticationError("IDENTITY_AUTHENTICATION_FAILED")
        actual = _credential_digest(login_secret, bytes(row[8]))
        if not hmac.compare_digest(actual, bytes(row[9])):
            raise IdentityAuthenticationError("IDENTITY_AUTHENTICATION_FAILED")
        return _identity(row[:8])

    def disable(self, identity_id: str, *, now: str) -> ProductIdentity:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE product_identities SET enabled=0,disabled_at=? "
                "WHERE identity_id=? AND enabled=1",
                (now, identity_id),
            ).rowcount
        if changed != 1:
            raise KeyError(identity_id)
        return self.get(identity_id)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection


def role_actions(role: ProductRole) -> frozenset[str]:
    return _ROLE_ACTIONS[role]


def _credential_digest(secret: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 200_000, dklen=32)


def _nonempty(*values: str) -> None:
    if any(not value for value in values):
        raise ValueError("identity strings must be non-empty")


def _identity(row: tuple[object, ...]) -> ProductIdentity:
    return ProductIdentity(
        identity_id=str(row[0]),
        subject_id=str(row[1]),
        display_name=str(row[2]),
        role=ProductRole(str(row[3])),
        capability_id=str(row[4]),
        enabled=bool(row[5]),
        created_at=str(row[6]),
        disabled_at=str(row[7]) if row[7] is not None else None,
    )


__all__ = [
    "IdentityAuthenticationError",
    "IdentityConflict",
    "IdentityStore",
    "ProductIdentity",
    "ProductRole",
    "role_actions",
]