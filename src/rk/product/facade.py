"""Composition of product capabilities behind the unique public seam."""

from __future__ import annotations

from typing import Protocol

from rk.product.api import (
    ArtifactOperation,
    ArtifactResult,
    EventStream,
    ProductCommand,
    ProductReceipt,
    ProductSession,
    QueryResult,
    QuerySpec,
    SubscriptionSpec,
)


class CommandPort(Protocol):
    def execute(self, session: ProductSession, request: ProductCommand) -> ProductReceipt: ...


class QueryPort(Protocol):
    def execute(self, session: ProductSession, spec: QuerySpec) -> QueryResult: ...


class SubscriptionPort(Protocol):
    def open(self, session: ProductSession, spec: SubscriptionSpec) -> EventStream: ...


class ArtifactPort(Protocol):
    def execute(self, session: ProductSession, request: ArtifactOperation) -> ArtifactResult: ...


class ResearchProductFacade:
    """Route all product calls to package-owned ports without exposing those ports."""

    def __init__(
        self,
        *,
        commands: CommandPort,
        queries: QueryPort,
        subscriptions: SubscriptionPort,
        artifacts: ArtifactPort,
    ) -> None:
        self.__commands = commands
        self.__queries = queries
        self.__subscriptions = subscriptions
        self.__artifacts = artifacts

    def command(self, session: ProductSession, request: ProductCommand) -> ProductReceipt:
        return self.__commands.execute(session, request)

    def query(self, session: ProductSession, spec: QuerySpec) -> QueryResult:
        return self.__queries.execute(session, spec)

    def subscribe(self, session: ProductSession, spec: SubscriptionSpec) -> EventStream:
        return self.__subscriptions.open(session, spec)

    def artifact(self, session: ProductSession, request: ArtifactOperation) -> ArtifactResult:
        return self.__artifacts.execute(session, request)


__all__ = [
    "ArtifactPort",
    "CommandPort",
    "QueryPort",
    "ResearchProductFacade",
    "SubscriptionPort",
]
