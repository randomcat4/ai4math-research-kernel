"""Registry and transactional assembler for product schema fragments.

Business packages own unnumbered fragments.  A release assembler may later assign a
linear migration number; this module deliberately exposes no way for a fragment to do so.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_PACKAGE_RE = re.compile(r"^[A-Z][0-9]{2}[a-z]?$")
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_OBJECT_RE = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?(?P<kind>TABLE|INDEX|TRIGGER|VIEW)\s+"
    r"(?P<conditional>IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_FORBIDDEN_VERSIONING_RE = re.compile(
    r"(?:^\s*--\s*migration-name\s*:|\bPRAGMA\s+user_version\b|\bschema_migrations\b)",
    re.IGNORECASE | re.MULTILINE,
)
_TRANSACTION_RE = re.compile(
    r"(?:^|;)\s*(?:BEGIN(?:\s+(?:DEFERRED|IMMEDIATE|EXCLUSIVE|TRANSACTION))?|"
    r"COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b",
    re.IGNORECASE,
)


class ProductMigrationError(RuntimeError):
    """A fragment registry or assembly invariant was violated."""


@dataclass(frozen=True, slots=True, order=True)
class SchemaObject:
    """A SQLite schema object exclusively owned by one fragment."""

    kind: str
    name: str


@dataclass(frozen=True, slots=True)
class SchemaFragment:
    """An immutable, unnumbered package schema proposal."""

    package: str
    slug: str
    path: Path
    sha256: str
    sql: str
    objects: tuple[SchemaObject, ...]

    @property
    def fragment_id(self) -> str:
        return f"{self.package}/{self.slug}"


@dataclass(frozen=True, slots=True)
class AssemblyStep:
    """A deterministic position in an assembly plan, not a release migration number."""

    position: int
    fragment: SchemaFragment


@dataclass(frozen=True, slots=True)
class AppliedFragment:
    package: str
    slug: str
    sha256: str
    assembly_position: int


class ProductMigrationRegistry:
    """Discover and validate ``schema_fragments/<package>/<slug>.sql`` proposals."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def discover(self) -> tuple[SchemaFragment, ...]:
        if not self._root.is_dir():
            raise ProductMigrationError("schema fragment directory does not exist")
        fragments: list[SchemaFragment] = []
        for path in sorted(self._root.rglob("*.sql"), key=lambda item: item.as_posix()):
            relative = path.relative_to(self._root)
            if len(relative.parts) != 2:
                raise ProductMigrationError(
                    f"fragment must be schema_fragments/<package>/<slug>.sql: {relative.as_posix()}"
                )
            package, filename = relative.parts
            slug = path.stem
            if not _PACKAGE_RE.fullmatch(package):
                raise ProductMigrationError(f"invalid package identifier: {package}")
            if not _SLUG_RE.fullmatch(slug):
                raise ProductMigrationError(f"invalid or numbered fragment slug: {filename}")
            raw = path.read_bytes()
            try:
                sql = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProductMigrationError(
                    f"fragment is not UTF-8: {relative.as_posix()}"
                ) from exc
            if sql.startswith("\ufeff"):
                raise ProductMigrationError(
                    f"fragment must not contain a BOM: {relative.as_posix()}"
                )
            if _FORBIDDEN_VERSIONING_RE.search(sql):
                raise ProductMigrationError(
                    f"fragment attempts to own release numbering: {relative.as_posix()}"
                )
            if _TRANSACTION_RE.search(_without_sql_comments(sql)):
                raise ProductMigrationError(
                    f"fragment attempts to control the assembly transaction: {relative.as_posix()}"
                )
            objects = _schema_objects(sql, relative)
            fragments.append(
                SchemaFragment(
                    package=package,
                    slug=slug,
                    path=path,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    sql=sql,
                    objects=objects,
                )
            )
        if not fragments:
            raise ProductMigrationError("no schema fragments found")
        self._reject_conflicts(fragments)
        return tuple(fragments)

    def plan(self) -> tuple[AssemblyStep, ...]:
        fragments = sorted(
            self.discover(), key=lambda item: (item.package != "D00a", item.package, item.slug)
        )
        return tuple(
            AssemblyStep(position=position, fragment=fragment)
            for position, fragment in enumerate(fragments, start=1)
        )

    @staticmethod
    def _reject_conflicts(fragments: list[SchemaFragment]) -> None:
        identities: set[str] = set()
        owners: dict[tuple[str, str], str] = {}
        for fragment in fragments:
            if fragment.fragment_id in identities:
                raise ProductMigrationError(f"duplicate fragment identity: {fragment.fragment_id}")
            identities.add(fragment.fragment_id)
            for schema_object in fragment.objects:
                key = (schema_object.kind.casefold(), schema_object.name.casefold())
                previous = owners.get(key)
                if previous is not None:
                    raise ProductMigrationError(
                        f"schema object {schema_object.kind}:{schema_object.name} is owned by "
                        f"both {previous} and {fragment.fragment_id}"
                    )
                owners[key] = fragment.fragment_id


class ProductMigrationAssembler:
    """Apply a complete registry plan atomically to a real SQLite connection."""

    def __init__(self, registry: ProductMigrationRegistry) -> None:
        self._registry = registry

    def apply(self, connection: sqlite3.Connection) -> tuple[AppliedFragment, ...]:
        plan = self._registry.plan()
        if connection.in_transaction:
            raise ProductMigrationError("assembler requires a connection outside a transaction")
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise ProductMigrationError("SQLite foreign key enforcement could not be enabled")
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._create_registry_table(connection)
            recorded = self._read_recorded(connection)
            planned_ids = {step.fragment.fragment_id for step in plan}
            missing = set(recorded) - planned_ids
            if missing:
                raise ProductMigrationError(
                    "applied product fragment is absent from registry: "
                    + ", ".join(sorted(missing))
                )
            for step in plan:
                fragment = step.fragment
                old = recorded.get(fragment.fragment_id)
                if old is not None:
                    if old.sha256 != fragment.sha256 or old.assembly_position != step.position:
                        raise ProductMigrationError(
                            f"applied product fragment has drifted: {fragment.fragment_id}"
                        )
                    continue
                for statement in _statements(fragment.sql, fragment.fragment_id):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO product_schema_fragments"
                    "(package, slug, sha256, assembly_position) VALUES (?, ?, ?, ?)",
                    (fragment.package, fragment.slug, fragment.sha256, step.position),
                )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ProductMigrationError("product fragments violate foreign key constraints")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise ProductMigrationError("SQLite integrity_check failed after product assembly")
            connection.commit()
        except BaseException as exc:
            connection.rollback()
            if isinstance(exc, ProductMigrationError):
                raise
            if isinstance(exc, sqlite3.DatabaseError):
                raise ProductMigrationError("product fragment assembly failed") from exc
            raise
        return tuple(self._read_recorded(connection)[step.fragment.fragment_id] for step in plan)

    @staticmethod
    def _create_registry_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS product_schema_fragments("
            "package TEXT NOT NULL, slug TEXT NOT NULL, sha256 TEXT NOT NULL "
            "CHECK(length(sha256)=64), assembly_position INTEGER NOT NULL UNIQUE "
            "CHECK(assembly_position>0), PRIMARY KEY(package,slug)) STRICT"
        )

    @staticmethod
    def _read_recorded(connection: sqlite3.Connection) -> dict[str, AppliedFragment]:
        rows = connection.execute(
            "SELECT package,slug,sha256,assembly_position FROM product_schema_fragments "
            "ORDER BY assembly_position"
        ).fetchall()
        result: dict[str, AppliedFragment] = {}
        for row in rows:
            item = AppliedFragment(str(row[0]), str(row[1]), str(row[2]), int(row[3]))
            result[f"{item.package}/{item.slug}"] = item
        return result


def _without_sql_comments(sql: str) -> str:
    no_blocks = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\r\n]*", " ", no_blocks)


def _schema_objects(sql: str, relative: Path) -> tuple[SchemaObject, ...]:
    matches = tuple(_OBJECT_RE.finditer(_without_sql_comments(sql)))
    if not matches:
        raise ProductMigrationError(f"fragment creates no schema object: {relative.as_posix()}")
    objects: list[SchemaObject] = []
    seen: set[tuple[str, str]] = set()
    for match in matches:
        if match.group("conditional") is not None:
            raise ProductMigrationError(
                f"fragment masks an object conflict with IF NOT EXISTS: {relative.as_posix()}"
            )
        item = SchemaObject(match.group("kind").upper(), match.group("name"))
        key = (item.kind.casefold(), item.name.casefold())
        if key in seen:
            raise ProductMigrationError(
                f"fragment creates schema object more than once: {item.kind}:{item.name}"
            )
        seen.add(key)
        objects.append(item)
    return tuple(sorted(objects))


def _statements(sql: str, fragment_id: str) -> tuple[str, ...]:
    statements: list[str] = []
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            if _without_sql_comments(pending).strip():
                statements.append(pending)
            pending = ""
    if pending.strip():
        raise ProductMigrationError(f"incomplete SQL statement in fragment: {fragment_id}")
    return tuple(statements)
