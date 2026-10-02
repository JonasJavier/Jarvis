"""AuditEvent is append-only at the application level (ADR-020)."""

import pytest

from audit.models import AppendOnlyError, AuditEvent
from audit.services import record

pytestmark = pytest.mark.django_db


@pytest.fixture
def event() -> AuditEvent:
    return record(actor="test", action="test.created", payload={"n": 1})


def test_event_cannot_be_updated(event: AuditEvent) -> None:
    event.action = "tampered"
    with pytest.raises(AppendOnlyError):
        event.save()


def test_event_cannot_be_deleted(event: AuditEvent) -> None:
    with pytest.raises(AppendOnlyError):
        event.delete()


def test_queryset_cannot_update_or_delete(event: AuditEvent) -> None:
    with pytest.raises(AppendOnlyError):
        AuditEvent.objects.filter(pk=event.pk).update(action="tampered")
    with pytest.raises(AppendOnlyError):
        AuditEvent.objects.filter(pk=event.pk).delete()
    with pytest.raises(AppendOnlyError):
        AuditEvent.objects.bulk_update([event], ["action"])
    assert AuditEvent.objects.get(pk=event.pk).action == "test.created"
