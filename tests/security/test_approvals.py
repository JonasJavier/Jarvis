"""Approval invariants (ADR-012): digest-bound, single-use, expiring, owner-only decisions."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.utils import timezone

from approvals.models import Approval, ApprovalStatus
from approvals.services import (
    ActionRequest,
    ApprovalAlreadyUsed,
    ApprovalExpired,
    ApprovalForbiddenActor,
    ApprovalInvalid,
    ApprovalNotFound,
    ApprovalService,
    InvalidApprovalTransition,
    digest_for,
)
from audit.models import AuditEvent
from identity.verifier import VerifiedIdentity
from policies.actions import Action, Actor
from policies.manifests.loader import ManifestBundle
from projects.models import Project
from tests.conftest import get_project

pytestmark = pytest.mark.django_db

KEY = "deploy:prod:example:run-1"


@pytest.fixture
def service() -> ApprovalService:
    return ApprovalService()


@pytest.fixture
def deploy(project: Project) -> ActionRequest:
    return ActionRequest(
        project=project,
        action=Action.DEPLOY_PRODUCTION,
        target="production",
        subject_ref="commit:0123abcd",
        params={"strategy": "rolling"},
    )


@pytest.fixture
def approved(service: ApprovalService, deploy: ActionRequest, owner: VerifiedIdentity) -> Approval:
    approval = service.request(deploy, requested_by=Actor.CONTROL_PLANE, reason="level 2")
    return service.approve(approval, identity=owner, source="test")


def test_action_without_approval_fails(service: ApprovalService, deploy: ActionRequest) -> None:
    with pytest.raises(ApprovalNotFound):
        service.consume(deploy, actor=Actor.DEPLOYER, idempotency_key=KEY)


def test_pending_approval_is_not_enough(service: ApprovalService, deploy: ActionRequest) -> None:
    approval = service.request(deploy, requested_by=Actor.CONTROL_PLANE)
    assert approval.status == ApprovalStatus.PENDING
    with pytest.raises(ApprovalNotFound):
        service.consume(deploy, actor=Actor.DEPLOYER, idempotency_key=KEY)


def test_valid_approval_is_consumed_exactly_once(
    service: ApprovalService, deploy: ActionRequest, approved: Approval
) -> None:
    used = service.consume(deploy, actor=Actor.DEPLOYER, idempotency_key=KEY)
    assert used.pk == approved.pk
    assert used.status == ApprovalStatus.USED
    assert used.used_at is not None
    assert used.used_by_key == KEY

    # A redelivery of the same execution continues; a new execution does not.
    assert service.consume(deploy, actor=Actor.DEPLOYER, idempotency_key=KEY).pk == used.pk
    with pytest.raises(ApprovalAlreadyUsed):
        service.consume(deploy, actor=Actor.DEPLOYER, idempotency_key="deploy:prod:example:run-2")


@pytest.mark.parametrize(
    "change",
    [
        {"subject_ref": "commit:ffff9999"},
        {"params": {"strategy": "recreate"}},
        {"params": {"strategy": "rolling", "extra": True}},
    ],
)
def test_changed_commit_or_params_invalidate_the_approval(
    service: ApprovalService, deploy: ActionRequest, approved: Approval, change: dict[str, object]
) -> None:
    changed = ActionRequest(
        project=deploy.project,
        action=deploy.action,
        target=deploy.target,
        subject_ref=str(change.get("subject_ref", deploy.subject_ref)),
        params=dict(change.get("params", deploy.params)),  # type: ignore[call-overload]
    )
    with pytest.raises(ApprovalInvalid):
        service.consume(changed, actor=Actor.DEPLOYER, idempotency_key=KEY)
    approved.refresh_from_db()
    assert approved.status == ApprovalStatus.INVALIDATED
    assert AuditEvent.objects.filter(action="approval.invalidated").exists()


def test_changed_target_does_not_match(
    service: ApprovalService, deploy: ActionRequest, approved: Approval
) -> None:
    staging = ActionRequest(
        project=deploy.project,
        action=deploy.action,
        target="staging",
        subject_ref=deploy.subject_ref,
        params=deploy.params,
    )
    with pytest.raises(ApprovalNotFound):
        service.consume(staging, actor=Actor.DEPLOYER, idempotency_key=KEY)


def test_policy_change_invalidates_the_approval(
    service: ApprovalService,
    deploy: ActionRequest,
    approved: Approval,
    configure_project: Callable[..., Project],
) -> None:
    before = digest_for(deploy)
    configure_project(level=3)  # manifest_hash changes
    after = ActionRequest(
        project=get_project(),
        action=deploy.action,
        target=deploy.target,
        subject_ref=deploy.subject_ref,
        params=deploy.params,
    )
    assert digest_for(after) != before
    with pytest.raises(ApprovalInvalid):
        service.consume(after, actor=Actor.DEPLOYER, idempotency_key=KEY)
    approved.refresh_from_db()
    assert approved.status == ApprovalStatus.INVALIDATED


def test_expired_approval_cannot_be_consumed(
    service: ApprovalService, deploy: ActionRequest, approved: Approval
) -> None:
    Approval.objects.filter(pk=approved.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(ApprovalExpired):
        service.consume(deploy, actor=Actor.DEPLOYER, idempotency_key=KEY)
    approved.refresh_from_db()
    assert approved.status == ApprovalStatus.EXPIRED


def test_expired_pending_approval_cannot_be_approved(
    service: ApprovalService, deploy: ActionRequest, owner: VerifiedIdentity
) -> None:
    approval = service.request(
        deploy, requested_by=Actor.CONTROL_PLANE, ttl=timedelta(milliseconds=1)
    )
    Approval.objects.filter(pk=approval.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(ApprovalExpired):
        service.approve(approval, identity=owner)
    assert service.expire_pending() == 0  # already expired above
    approval.refresh_from_db()
    assert approval.status == ApprovalStatus.EXPIRED


def test_approval_from_another_project_does_not_serve(
    service: ApprovalService,
    deploy: ActionRequest,
    owner: VerifiedIdentity,
    add_project: Callable[..., None],
    reload_manifests: Callable[[], ManifestBundle],
) -> None:
    add_project("other", client_id="example-client", repository="my-org/other")
    reload_manifests()
    other = ActionRequest(
        project=get_project("other"),
        action=deploy.action,
        target=deploy.target,
        subject_ref=deploy.subject_ref,
        params=deploy.params,
    )
    service.approve(service.request(other, requested_by=Actor.CONTROL_PLANE), identity=owner)
    with pytest.raises(ApprovalNotFound):
        service.consume(deploy, actor=Actor.DEPLOYER, idempotency_key=KEY)


@pytest.mark.parametrize(
    "actor", [Actor.OPS_AGENT, Actor.CLIENT_AGENT, Actor.CODING_WORKER, Actor.OWNER, Actor.CI]
)
def test_llm_agents_and_owner_cannot_request_or_consume(
    service: ApprovalService, deploy: ActionRequest, approved: Approval, actor: Actor
) -> None:
    with pytest.raises(ApprovalForbiddenActor):
        service.request(deploy, requested_by=actor)
    with pytest.raises(ApprovalForbiddenActor):
        service.consume(deploy, actor=actor, idempotency_key=KEY)
    approved.refresh_from_db()
    assert approved.status == ApprovalStatus.APPROVED


def test_only_the_owner_decides(
    service: ApprovalService, deploy: ActionRequest, stranger: VerifiedIdentity
) -> None:
    approval = service.request(deploy, requested_by=Actor.CONTROL_PLANE)
    with pytest.raises(ApprovalForbiddenActor):
        service.approve(approval, identity=stranger)
    with pytest.raises(ApprovalForbiddenActor):
        service.reject(approval, identity=stranger)
    approval.refresh_from_db()
    assert approval.status == ApprovalStatus.PENDING


def test_rejected_approval_is_final(
    service: ApprovalService, deploy: ActionRequest, owner: VerifiedIdentity
) -> None:
    approval = service.request(deploy, requested_by=Actor.CONTROL_PLANE)
    service.reject(approval, identity=owner, source="test")
    with pytest.raises(InvalidApprovalTransition):
        service.approve(approval, identity=owner)
    with pytest.raises(ApprovalNotFound):
        service.consume(deploy, actor=Actor.DEPLOYER, idempotency_key=KEY)


def test_request_is_idempotent_per_operation(
    service: ApprovalService, deploy: ActionRequest
) -> None:
    first = service.request(deploy, requested_by=Actor.CONTROL_PLANE)
    second = service.request(deploy, requested_by=Actor.CONTROL_PLANE)
    assert first.pk == second.pk
    assert Approval.objects.count() == 1


def test_default_ttl_comes_from_global_manifest(
    service: ApprovalService, deploy: ActionRequest
) -> None:
    approval = service.request(deploy, requested_by=Actor.CONTROL_PLANE)
    ttl = approval.expires_at - approval.requested_at
    assert timedelta(minutes=59) < ttl <= timedelta(minutes=60)


def test_every_transition_is_audited(
    service: ApprovalService, deploy: ActionRequest, approved: Approval
) -> None:
    service.consume(deploy, actor=Actor.DEPLOYER, idempotency_key=KEY)
    actions = list(
        AuditEvent.objects.filter(target_type="approval", target_id=str(approved.pk))
        .order_by("id")
        .values_list("action", flat=True)
    )
    assert actions == ["approval.requested", "approval.approved", "approval.consumed"]
    consumed = AuditEvent.objects.get(action="approval.consumed")
    assert consumed.payload["action_digest"] == approved.action_digest
    assert consumed.payload["idempotency_key"] == KEY
