"""Client and internal projects. Materialized from `project_manifests/projects/*.yaml` (ADR-017)."""

from django.db import models


class Ownership(models.TextChoices):
    CLIENT = "client"
    INTERNAL = "internal"


class Project(models.Model):
    slug = models.SlugField(max_length=64, unique=True)
    client = models.ForeignKey("clients.Client", on_delete=models.PROTECT, related_name="projects")
    name = models.CharField(max_length=200)
    # Stored lowercase: GitHub owner/repo names are case-insensitive.
    repository = models.CharField(max_length=200, unique=True)
    default_branch = models.CharField(max_length=255)
    ownership = models.CharField(max_length=16, choices=Ownership.choices)
    is_active = models.BooleanField(default=True)
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
