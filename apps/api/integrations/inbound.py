"""Queue handler for `inbound_event:*` tasks: routes a persisted event by its source."""

from audit.services import record
from tickets.models import InboundEvent


def handle_inbound_event(ident: str) -> None:
    event = InboundEvent.objects.get(pk=int(ident))
    match event.source:
        case "github":
            from integrations.github.events import handle_delivery

            handle_delivery(event.pk)
        case "whatsapp":
            from messaging.events import handle_whatsapp_event

            handle_whatsapp_event(event.pk)
        case _:
            record(
                actor="control_plane",
                action="event.ignored",
                target_type="inbound_event",
                target_id=str(event.pk),
                payload={"source": event.source[:32]},
            )
