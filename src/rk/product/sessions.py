"""Single-organization login, identity switching, and session capability derivation."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from rk.domain import CapabilityError, VerifiedCapability
from rk.product.api import ProductSession
from rk.product.identity import IdentityStore, ProductIdentity, ProductRole
from rk.runtime import parse_utc
from rk.sqlite import open_sqlite


class SessionAuthenticationError(PermissionError):
    """A session is absent, expired, revoked, stale, or not linked to an identity."""


@dataclass(frozen=True, slots=True)
class SessionView:
    session_id: str
    organization_id: str
    principal_subject_id: str
    identity_id: str
    display_name: str
    role: ProductRole
    linked_identity_ids: tuple[str, ...]
    session_version: int
    issued_at: str
    expires_at: str


class SessionStore:
    """One browser session may switch among explicitly authenticated identities."""

    def __init__(
        self,
        db_path: Path,
        identities: IdentityStore,
        id_generator: Callable[[], str],
        organization_id: str,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if not organization_id:
            raise ValueError("organization_id must be non-empty")
        self._db_path = Path(db_path)
        self._identities = identities
        self._ids = id_generator
        self._organization_id = organization_id
        self._busy_timeout_ms = busy_timeout_ms

    def login(
        self,
        *,
        identity_id: str,
        login_secret: str,
        now: str,
        expires_at: str,
        session_id: str | None = None,
    ) -> SessionView:
        identity = self._identities.authenticate(identity_id, login_secret)
        # A shared cookie is a read-only admission token, not an authenticated
        # session container.  Successful login rotates it into a fresh managed
        # session so the read-only marker can never survive authentication.
        if session_id is not None and session_id.startswith("shared."):
            session_id = None
        return self._enter_identity(
            identity,
            now=now,
            expires_at=expires_at,
            session_id=session_id,
        )

    def enter_shared(
        self,
        *,
        now: str,
        expires_at: str,
        session_id: str | None = None,
    ) -> SessionView:
        """Enter the shared read-only product view without browser credentials."""

        if session_id is not None:
            raise SessionAuthenticationError("SHARED_SESSION_REQUIRES_ROTATION")
        identity = self._identities.first_enabled_by_role(ProductRole.MAIN)
        return self._enter_identity(
            identity,
            now=now,
            expires_at=expires_at,
            session_id=session_id,
            shared_read_only=True,
        )

    def _enter_identity(
        self,
        identity: ProductIdentity,
        *,
        now: str,
        expires_at: str,
        session_id: str | None,
        shared_read_only: bool = False,
    ) -> SessionView:
        if parse_utc(expires_at) <= parse_utc(now):
            raise ValueError("session expiry must be after login time")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if session_id is None:
                generated = self._ids()
                session_id = f"shared.{generated}" if shared_read_only else generated
                connection.execute(
                    "INSERT INTO product_sessions("
                    "session_id,organization_id,active_identity_id,session_version,issued_at,"
                    "expires_at) VALUES(?,?,?,1,?,?)",
                    (session_id, self._organization_id, identity.identity_id, now, expires_at),
                )
            else:
                self._active_row(connection, session_id, now)
                connection.execute(
                    "UPDATE product_sessions SET active_identity_id=?,session_version="
                    "session_version+1 WHERE session_id=?",
                    (identity.identity_id, session_id),
                )
            connection.execute(
                "INSERT INTO product_session_identities(session_id,identity_id,authenticated_at) "
                "VALUES(?,?,?) ON CONFLICT(session_id,identity_id) DO UPDATE SET "
                "authenticated_at=excluded.authenticated_at",
                (session_id, identity.identity_id, now),
            )
            connection.commit()
        return self.view(session_id, now=now)

    def switch(self, session_id: str, identity_id: str, *, now: str) -> SessionView:
        identity = self._identities.get(identity_id)
        if not identity.enabled:
            raise SessionAuthenticationError("SESSION_IDENTITY_DISABLED")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._active_row(connection, session_id, now)
            linked = connection.execute(
                "SELECT 1 FROM product_session_identities WHERE session_id=? AND identity_id=?",
                (session_id, identity_id),
            ).fetchone()
            if linked is None:
                raise SessionAuthenticationError("SESSION_IDENTITY_NOT_AUTHENTICATED")
            connection.execute(
                "UPDATE product_sessions SET active_identity_id=?,session_version="
                "session_version+1 WHERE session_id=?",
                (identity_id, session_id),
            )
            connection.commit()
        return self.view(session_id, now=now)

    def logout(self, session_id: str, *, now: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._active_row(connection, session_id, now)
            changed = connection.execute(
                "UPDATE product_sessions SET revoked_at=?,session_version=session_version+1 "
                "WHERE session_id=? AND revoked_at IS NULL",
                (now, session_id),
            ).rowcount
            if changed != 1:
                raise SessionAuthenticationError("SESSION_NOT_ACTIVE")
            connection.commit()

    def derive(self, session_id: str, *, now: str) -> ProductSession:
        _row, identity = self._resolved(session_id, now)
        return ProductSession(
            session_id=session_id,
            principal_subject_id=identity.subject_id,
            capability_ids=(identity.capability_id,),
        )

    def view(self, session_id: str, *, now: str) -> SessionView:
        row, identity = self._resolved(session_id, now)
        with self._connect() as connection:
            linked = tuple(
                str(item[0])
                for item in connection.execute(
                    "SELECT identity_id FROM product_session_identities WHERE session_id=? "
                    "ORDER BY authenticated_at,identity_id",
                    (session_id,),
                ).fetchall()
            )
        return SessionView(
            session_id=session_id,
            organization_id=str(row[1]),
            principal_subject_id=identity.subject_id,
            identity_id=identity.identity_id,
            display_name=identity.display_name,
            role=identity.role,
            linked_identity_ids=linked,
            session_version=int(str(row[3])),
            issued_at=str(row[4]),
            expires_at=str(row[5]),
        )

    def verified_capability(
        self,
        session: ProductSession,
        *,
        action: str,
        run_id: str | None,
        now: str,
    ) -> VerifiedCapability:
        if session.session_id.startswith("shared."):
            raise CapabilityError("CAPABILITY_DENIED")
        current = self.derive(session.session_id, now=now)
        if current != session:
            raise CapabilityError("CAPABILITY_DENIED")
        row, identity = self._resolved(session.session_id, now)
        if action not in identity.allowed_actions:
            raise CapabilityError("CAPABILITY_DENIED")
        return VerifiedCapability(
            capability_id=identity.capability_id,
            subject_id=identity.subject_id,
            issuer=f"rk-product:{self._organization_id}",
            allowed_actions=identity.allowed_actions,
            run_scope=frozenset({run_id}) if run_id is not None else frozenset({"*"}),
            issued_at=str(row[4]),
            expires_at=str(row[5]),
            subject_role=identity.role,
        )

    def _resolved(self, session_id: str, now: str) -> tuple[tuple[object, ...], ProductIdentity]:
        with self._connect() as connection:
            row = self._active_row(connection, session_id, now)
            linked = connection.execute(
                "SELECT 1 FROM product_session_identities WHERE session_id=? AND identity_id=?",
                (session_id, str(row[2])),
            ).fetchone()
        if linked is None:
            raise SessionAuthenticationError("SESSION_IDENTITY_NOT_AUTHENTICATED")
        try:
            identity = self._identities.get(str(row[2]))
        except KeyError as error:
            raise SessionAuthenticationError("SESSION_IDENTITY_MISSING") from error
        if not identity.enabled:
            raise SessionAuthenticationError("SESSION_IDENTITY_DISABLED")
        return row, identity

    def _active_row(
        self, connection: sqlite3.Connection, session_id: str, now: str
    ) -> tuple[object, ...]:
        row = cast(
            tuple[object, ...] | None,
            connection.execute(
                "SELECT session_id,organization_id,active_identity_id,session_version,issued_at,"
                "expires_at,revoked_at FROM product_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone(),
        )
        if (
            row is None
            or str(row[1]) != self._organization_id
            or row[6] is not None
            or parse_utc(str(row[5])) <= parse_utc(now)
        ):
            raise SessionAuthenticationError("SESSION_NOT_ACTIVE")
        return row

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection


class SessionCapabilitySource:
    """Resolve only the current session principal to its frozen role capability."""

    def __init__(self, sessions: SessionStore, clock: Callable[[], str]) -> None:
        self._sessions = sessions
        self._clock = clock

    def resolve(
        self,
        session: ProductSession,
        *,
        action: str,
        run_id: str | None,
    ) -> VerifiedCapability:
        return self._sessions.verified_capability(
            session,
            action=action,
            run_id=run_id,
            now=self._clock(),
        )


__all__ = [
    "SessionAuthenticationError",
    "SessionCapabilitySource",
    "SessionStore",
    "SessionView",
]
