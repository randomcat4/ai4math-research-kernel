"""One acyclic materialization point for all published business router factories."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rk.http_shell import RouterProtocol

RouterFactory = Callable[[], RouterProtocol]
AdminBindingFactory = Callable[[], None]


@dataclass(frozen=True, slots=True)
class PublishedRouteFactories:
    """Factories for the complete public route graph.

    The fields are deliberately fixed: a page cannot append a private endpoint and no
    router receives the application under construction.
    """

    command: RouterFactory
    query: RouterFactory
    activity: RouterFactory
    artifact: RouterFactory
    session: RouterFactory
    admin: AdminBindingFactory
    review: RouterFactory

    def materialize(self) -> tuple[RouterProtocol, ...]:
        self.admin()
        return (
            self.command(),
            self.query(),
            self.activity(),
            self.artifact(),
            self.session(),
            self.review(),
        )


__all__ = ["AdminBindingFactory", "PublishedRouteFactories", "RouterFactory"]
