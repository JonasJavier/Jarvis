"""ApprovalService (ADR-012, permissions-and-approvals.md).

An approval is bound to the sha256 of the canonical JSON of the exact operation. Before an
execution consumes it, the digest is recomputed from the *current* operation and the *current*
project policy, so any change of target, commit, artifact, parameters or policy invalidates it.
Approvals are single-use: consumption is atomic and tied to the idempotency key of the execution.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from approvals.models import LIVE_STATUSES, Approval, ApprovalStatus
from audit.services import record
from identity.verifier import VerifiedIdentity
from policies.actions import LLM_ACTORS, Action, Actor
from policies.models import GlobalPolicy
from projects.models import Project

# Identities allowed to ask for or consume an approval. LLM agents and the owner never do.
EXECUTING_ACTORS: frozenset[Actor] = frozenset(
    {Actor.CONTROL_PLANE, Actor.DEPLOYER, Actor.STAGING_TRIGGER}
)


class ApprovalError(Exception):
    pass


class ApprovalNotFound(ApprovalError):
    """No approved approval matches the operation as it is now."""


class ApprovalInvalid(ApprovalError):
    """An approval existed, but the operation or the policy changed since it was approved."""


class ApprovalExpired(ApprovalError):
    pass


class ApprovalAlreadyUsed(ApprovalError):
    pass


class ApprovalForbiddenActor(ApprovalError):
    pass


class InvalidApprovalTransition(ApprovalError):
    pass


@dataclass(frozen=True)
class ActionRequest:
    """The exact operation an approval is about."""

    project: Project
    action: Action
    target: str = ""
    subject_ref: str = ""
    params: dict[str, Any] = field(default_factory=dict)


def action_digest(
    *,
    project_slug: str,
    action: str,
    target: str,
    subject_ref: str,
    params: dict[str, Any],
    manifest_hash: str,
) -> str:
    material = {
        "project_id": project_slug,
        "action": action,
        "target": target,
        "subject_ref": subject_ref,
        "params": params,
        "manifest_hash": manifest_hash,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def digest_for(request: ActionRequest) -> str:
    """Digest of `request` against the project's *current* policy."""
    return action_digest(
        project_slug=request.project.slug,
        action=request.action.value,
        target=request.target,
        subject_ref=request.subject_ref,
        params=request.params,
        manifest_hash=request.project.contract_policy.manifest_hash,
    )


class ApprovalService:
    def request(
        self,
        request: ActionRequest,
        *,
        requested_by: Actor,
        reason: str = "",
        correlation_id: str = "",
        ttl: timedelta | None = None,
    ) -> Approval:
        """Create a pending approval, or return the live one for this exact operation."""
        self._check_actor(requested_by, "request")
        digest = digest_for(request)
        with transaction.atomic():
            existing = (
                Approval.objects.select_for_update()
                .filter(action_digest=digest, status__in=LIVE_STATUSES)
                .first()
            )
            if existing is not None:
                return existing
            if ttl is None:
                ttl = timedelta(minutes=GlobalPolicy.current().approval_default_ttl_minutes)
            approval = Approval.objects.create(
                project=request.project,
                action=request.action.value,
                target=request.target,
                subject_ref=request.subject_ref,
                params=request.params,
                manifest_hash=request.project.contract_policy.manifest_hash,
                action_digest=digest,
                reason=reason[:200],
                requested_by=requested_by.value,
                expires_at=timezone.now() + ttl,
                correlation_id=correlation_id,
            )
            self._audit(approval, "approval.requested", requested_by.value)
            return approval

    def approve(
        self, approval: Approval, *, identity: VerifiedIdentity, source: str = "api"
    ) -> Approval:
        return self._decide(approval, ApprovalStatus.APPROVED, identity, source)

    def reject(
        self, approval: Approval, *, identity: VerifiedIdentity, source: str = "api"
    ) -> Approval:
        return self._decide(approval, ApprovalStatus.REJECTED, identity, source)

    def consume(self, request: ActionRequest, *, actor: Actor, idempotency_key: str) -> Approval:
        """Atomically consume the approval for `request` as it is *now*.

        A redelivery of the same execution (same idempotency key) returns the already-used
        approval; any other execution needs a new approval.
        """
        self._check_actor(actor, "consume")
        if not idempotency_key:
            raise ValueError("an idempotency key is required to consume an approval")
        digest = digest_for(request)
        now = timezone.now()
        expired: Approval | None = None
        with transaction.atomic():
            approval = (
                Approval.objects.select_for_update()
                .filter(
                    action_digest=digest,
                    status__in=[ApprovalStatus.APPROVED, ApprovalStatus.USED],
                )
                .first()
            )
            if approval is not None and approval.status == ApprovalStatus.USED:
                if approval.used_by_key == idempotency_key:
                    return approval
                raise ApprovalAlreadyUsed("approval already consumed by another execution")
            if approval is not None and approval.expires_at <= now:
                expired = approval
            elif approval is not None:
                approval.status = ApprovalStatus.USED
                approval.used_at = now
                approval.used_by_key = idempotency_key
                approval.save(update_fields=["status", "used_at", "used_by_key"])
                self._audit(
                    approval,
                    "approval.consumed",
                    actor.value,
                    {"idempotency_key": idempotency_key},
                )
                return approval
        # Bookkeeping below runs in its own transaction so it survives the raise.
        if expired is not None:
            with transaction.atomic():
                self._set_status(expired, ApprovalStatus.EXPIRED, actor.value, now)
            raise ApprovalExpired("approval expired before it was used")
        with transaction.atomic():
            invalidated = self._invalidate_stale(request, digest, actor)
        if invalidated:
            raise ApprovalInvalid(
                f"approval for {request.action.value} on {request.project.slug} no longer matches "
                "the operation or the policy"
            )
        raise ApprovalNotFound(
            f"no approved approval for {request.action.value} on {request.project.slug}"
        )

    def expire_pending(self) -> int:
        """Mark live approvals past `expires_at` as expired. Returns how many."""
        now = timezone.now()
        count = 0
        with transaction.atomic():
            for approval in Approval.objects.select_for_update().filter(
                status__in=LIVE_STATUSES, expires_at__lte=now
            ):
                self._set_status(approval, ApprovalStatus.EXPIRED, "system:approvals", now)
                count += 1
        return count

    # --- internals ------------------------------------------------------------------------

    def _decide(
        self,
        approval: Approval,
        status: ApprovalStatus,
        identity: VerifiedIdentity,
        source: str,
    ) -> Approval:
        if not identity.is_owner:
            raise ApprovalForbiddenActor("only the owner can decide an approval")
        now = timezone.now()
        with transaction.atomic():
            approval = Approval.objects.select_for_update().get(pk=approval.pk)
            if approval.status != ApprovalStatus.PENDING:
                raise InvalidApprovalTransition(f"approval is {approval.status}, not pending")
            if approval.expires_at > now:
                approval.status = status
                approval.decided_by = identity.subject
                approval.decided_at = now
                approval.decision_source = source
                approval.save(
                    update_fields=["status", "decided_by", "decided_at", "decision_source"]
                )
                self._audit(
                    approval,
                    f"approval.{status.value}",
                    f"owner:{identity.subject}",
                    {"source": source},
                )
                return approval
        with transaction.atomic():
            self._set_status(approval, ApprovalStatus.EXPIRED, identity.subject, now)
        raise ApprovalExpired("approval expired before it was decided")

    def _invalidate_stale(self, request: ActionRequest, digest: str, actor: Actor) -> int:
        """Approved approvals for the same action/target whose digest no longer matches."""
        stale = Approval.objects.select_for_update().filter(
            project=request.project,
            action=request.action.value,
            target=request.target,
            status=ApprovalStatus.APPROVED,
        )
        now = timezone.now()
        count = 0
        for approval in stale:
            if approval.action_digest != digest:
                self._set_status(
                    approval, ApprovalStatus.INVALIDATED, actor.value, now, {"new_digest": digest}
                )
                count += 1
        return count

    def _set_status(
        self,
        approval: Approval,
        status: ApprovalStatus,
        actor: str,
        now: datetime,
        payload: dict[str, Any] | None = None,
    ) -> None:
        approval.status = status
        if status in (ApprovalStatus.EXPIRED, ApprovalStatus.INVALIDATED):
            approval.decided_at = approval.decided_at or now
        approval.save(update_fields=["status", "decided_at"])
        self._audit(approval, f"approval.{status.value}", actor, payload)

    @staticmethod
    def _check_actor(actor: Actor, verb: str) -> None:
        if actor in LLM_ACTORS or actor not in EXECUTING_ACTORS:
            raise ApprovalForbiddenActor(f"actor '{actor}' cannot {verb} approvals")

    @staticmethod
    def _audit(
        approval: Approval, action: str, actor: str, payload: dict[str, Any] | None = None
    ) -> None:
        record(
            actor=actor,
            action=action,
            target_type="approval",
            target_id=str(approval.pk),
            client=approval.project.client,
            project=approval.project,
            correlation_id=approval.correlation_id,
            payload={
                "approval_action": approval.action,
                "action_digest": approval.action_digest,
                "status": approval.status,
                **(payload or {}),
            },
        )
