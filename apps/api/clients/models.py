"""Clients and their contacts. Materialized from `project_manifests/clients/*.yaml` (ADR-017)."""

from typing import Any

from django.db import models

from clients.normalization import ContactKind, normalize_contact


class Client(models.Model):
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    budget_daily_usd = models.DecimalField(max_digits=10, decimal_places=2)
    budget_monthly_usd = models.DecimalField(max_digits=10, decimal_places=2)
    manifest_path = models.CharField(max_length=255)
    manifest_hash = models.CharField(max_length=64)
    source_commit = models.CharField(max_length=64, blank=True)
    loaded_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self) -> str:
        return self.slug


class Contact(models.Model):
    KIND_CHOICES = [(kind.value, kind.value) for kind in ContactKind]

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="contacts")
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    value = models.CharField(max_length=320)

    class Meta:
        ordering = ["client__slug", "kind", "value"]
        constraints = [
            # One normalized value can never point to two clients (no ambiguous identification).
            models.UniqueConstraint(fields=["kind", "value"], name="unique_contact_per_kind"),
        ]

    def __str__(self) -> str:
        return f"{self.client.slug}:{self.kind}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if normalize_contact(self.kind, self.value) != self.value:
            raise ValueError("Contact values must be stored in canonical normalized form.")
        super().save(*args, **kwargs)
