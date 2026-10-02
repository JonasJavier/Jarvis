from typing import TYPE_CHECKING, Any

from audit.models import AuditEvent

if TYPE_CHECKING:
    from clients.models import Client
    from projects.models import Project


def record(
    *,
    actor: str,
    action: str,
    target_type: str = "",
    target_id: str = "",
    client: "Client | None" = None,
    project: "Project | None" = None,
    correlation_id: str = "",
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append an audit event. Callers must never put secrets or raw personal data in `payload`."""
    return AuditEvent.objects.create(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        client=client,
        project=project,
        correlation_id=correlation_id,
        payload=payload or {},
    )
