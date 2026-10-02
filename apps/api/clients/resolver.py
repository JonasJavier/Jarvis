"""ProjectResolver: normalized contact -> client/project, or `NeedsIdentification` (ADR-010).

Exact match only. A sender that cannot be normalized, is unknown, belongs to an inactive client,
or maps to a client with several active projects and no unambiguous hint is *not* identified and
no action on repositories or infrastructure may follow.
"""

from dataclasses import dataclass

from clients.models import Client, Contact
from clients.normalization import ContactKind, ContactNormalizationError, normalize_contact
from projects.models import Project


@dataclass(frozen=True)
class Identified:
    client: Client
    project: Project
    sender_value: str  # canonical form


@dataclass(frozen=True)
class NeedsIdentification:
    reason: str
    client: Client | None = None  # known client, ambiguous project


Resolution = Identified | NeedsIdentification


def resolve(
    kind: ContactKind | str, raw_value: str, *, project_hint: str | None = None
) -> Resolution:
    try:
        kind = ContactKind(kind)
    except ValueError:
        return NeedsIdentification("unknown contact kind")
    try:
        value = normalize_contact(kind, raw_value)
    except ContactNormalizationError:
        return NeedsIdentification("sender could not be normalized")

    contact = Contact.objects.select_related("client").filter(kind=kind, value=value).first()
    if contact is None:
        return NeedsIdentification("unknown sender")
    client = contact.client
    if not client.is_active:
        return NeedsIdentification("client is inactive", client=client)

    projects = list(Project.objects.filter(client=client, is_active=True))
    if project_hint:
        hint = project_hint.strip().lower()
        matches = [p for p in projects if p.slug == hint or p.repository == hint]
        if len(matches) == 1:
            return Identified(client=client, project=matches[0], sender_value=value)
        return NeedsIdentification("project hint does not match an active project", client=client)
    if len(projects) == 1:
        return Identified(client=client, project=projects[0], sender_value=value)
    if not projects:
        return NeedsIdentification("client has no active project", client=client)
    return NeedsIdentification("client has several active projects", client=client)
