"""Public Microsoft Graph integration boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from graph_integration.inbound import GraphInbound
from graph_integration.models import (
    GraphBinding,
    GraphInboundReceipt,
    GraphOutboundRateBucket,
    GraphOutboundRelease,
)
from graph_integration.outbound import GraphOutbound


class GraphClient(Protocol):
    def delta_page(self, token: str | None) -> dict[str, Any]: ...
    def renew_subscription(self, client_state: str) -> dict[str, Any]: ...
    def download_attachment(self, message_id: str, attachment_id: str) -> bytes: ...
    def send_message(self, write_id: str, message: dict[str, Any]) -> dict[str, Any]: ...
    def create_upload_session(
        self, write_id: str, attachment_id: str, size: int
    ) -> dict[str, Any]: ...
    def upload_chunk(
        self, session: str, offset: int, chunk: bytes, total: int
    ) -> dict[str, Any]: ...
    def probe_write(self, write_id: str) -> dict[str, Any]: ...


@dataclass(slots=True)
class GraphIntegration:
    inbound: GraphInbound
    outbound: GraphOutbound


def graph_tables() -> tuple[Any, ...]:
    return (
        GraphBinding.__table__,
        GraphInboundReceipt.__table__,
        GraphOutboundRelease.__table__,
        GraphOutboundRateBucket.__table__,
    )


def build_graph_integration(
    app: Any,
    client: GraphClient | None = None,
    config: dict[str, Any] | None = None,
) -> GraphIntegration:
    configured = dict(config or {})
    required = {"tenant_ref", "client_ref", "mailbox_ref", "secret_ref"}
    ready = client is not None or required <= set(configured)
    engine = getattr(app.state, "engine", None)
    if engine is not None:
        GraphBinding.metadata.create_all(engine, tables=list(graph_tables()))
    integration = GraphIntegration(
        inbound=GraphInbound(app, client, configured, ready),
        outbound=GraphOutbound(app, client, configured, ready),
    )
    app.state.graph_integration = integration
    runtime = getattr(app.state, "agent_runtime", None)
    if runtime is not None:
        runtime.comms.install_transport(integration.outbound.enqueue)
    return integration


__all__ = [
    "GraphClient",
    "GraphIntegration",
    "build_graph_integration",
    "graph_tables",
]
