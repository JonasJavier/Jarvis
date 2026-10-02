"""Manifests are the only writer of policy data (ADR-017); identification is unambiguous."""

from collections.abc import Callable
from pathlib import Path

import pytest
from django.contrib import admin
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import RequestFactory
from django.utils import timezone

from audit.models import AuditEvent
from clients.admin import ContactInline
from clients.models import Client, Contact
from policies.manifests.loader import ManifestError, load_bundle
from policies.manifests.materialize import apply_bundle
from policies.models import ContractPolicy
from projects.models import Project

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("model", [Client, Project, ContractPolicy, AuditEvent])
def test_admin_cannot_add_change_or_delete_policy_data(model: type) -> None:
    request = RequestFactory().get("/admin/")
    request.user = User(is_superuser=True, is_staff=True)
    model_admin = admin.site._registry[model]
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)


def test_contact_inline_is_read_only() -> None:
    request = RequestFactory().get("/admin/")
    request.user = User(is_superuser=True, is_staff=True)
    inline = ContactInline(Client, admin.site)
    assert not inline.has_add_permission(request, None)
    assert not inline.has_change_permission(request, None)
    assert not inline.has_delete_permission(request, None)


def make_client(slug: str) -> Client:
    return Client.objects.create(
        slug=slug,
        name=slug,
        budget_daily_usd=1,
        budget_monthly_usd=1,
        manifest_path=f"clients/{slug}.yaml",
        manifest_hash="0" * 64,
        loaded_at=timezone.now(),
    )


def test_contact_must_be_stored_normalized() -> None:
    client = make_client("acme")
    with pytest.raises(ValueError, match="canonical"):
        Contact.objects.create(client=client, kind="whatsapp", value="+1 809 555 0100")


def test_same_contact_cannot_belong_to_two_clients() -> None:
    Contact.objects.create(client=make_client("a"), kind="email", value="x@example.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        Contact.objects.create(client=make_client("b"), kind="email", value="x@example.com")


def test_equivalent_contacts_across_manifests_are_rejected(
    manifests_dir: Path, add_client: Callable[..., None]
) -> None:
    # Same number written differently: must be detected after normalization.
    add_client("acme", phone="+1 (809) 555-0100", email="it@acme.example")
    with pytest.raises(ManifestError) as exc_info:
        load_bundle(manifests_dir)
    assert any("ambiguous identification" in e for e in exc_info.value.errors)


def test_duplicate_yaml_keys_are_rejected(manifests_dir: Path) -> None:
    # PyYAML would silently keep the last value, hiding a policy override.
    path = manifests_dir / "projects" / "example.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "\npolicy:\n  autonomy_level: 3\n", encoding="utf-8"
    )
    with pytest.raises(ManifestError) as exc_info:
        load_bundle(manifests_dir)
    assert any("duplicate key" in e for e in exc_info.value.errors)


def test_audit_payloads_do_not_contain_contact_values(manifests_dir: Path) -> None:
    apply_bundle(load_bundle(manifests_dir), commit="")
    payloads = " ".join(str(e.payload) for e in AuditEvent.objects.all())
    assert "+18095550100" not in payloads
    assert "soporte@example.com" not in payloads
