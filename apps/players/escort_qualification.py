from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .escort_models import OrderEscortRequirementSnapshot
from .models import Player, PlayerEscortApplication, PlayerEscortQualification


def get_escort_qualification(player):
    qualification, _ = PlayerEscortQualification.objects.get_or_create(player=player)
    return qualification


def player_has_approved_escort_qualification(player):
    if not player or not getattr(player, 'pk', None):
        return False
    return PlayerEscortQualification.objects.filter(
        player_id=player.pk,
        status=PlayerEscortQualification.STATUS_APPROVED,
    ).exists()


def order_requires_escort_qualification(order):
    if not order or not getattr(order, 'pk', None):
        return False
    snapshot_value = (
        OrderEscortRequirementSnapshot.objects
        .filter(order_id=order.pk)
        .values_list('requires_escort_qualification', flat=True)
        .first()
    )
    if snapshot_value is not None:
        return bool(snapshot_value)
    package = getattr(order, 'package', None)
    return bool(package and getattr(package, 'requires_escort_qualification', False))


def escort_order_block_reason(order, player):
    if not order_requires_escort_qualification(order):
        return ''
    if player_has_approved_escort_qualification(player):
        return ''
    return '该订单为护航单，仅护航资格已通过的陪玩可以接单'


@transaction.atomic
def submit_escort_application(player, experience, evidence_urls):
    player = Player.objects.select_for_update().get(pk=player.pk)
    qualification = get_escort_qualification(player)
    if qualification.status == PlayerEscortQualification.STATUS_APPROVED:
        raise ValidationError({'detail': '您的护航资格已通过，无需重复申请'})
    if qualification.status == PlayerEscortQualification.STATUS_SUSPENDED:
        raise ValidationError({'detail': '护航资格已被暂停，请联系管理员处理'})

    pending = (
        PlayerEscortApplication.objects
        .select_for_update()
        .filter(player=player, status=PlayerEscortApplication.STATUS_PENDING)
        .first()
    )
    if pending:
        pending.experience = experience
        pending.evidence_urls = evidence_urls
        pending.reject_reason = ''
        pending.review_note = ''
        pending.save(update_fields=['experience', 'evidence_urls', 'reject_reason', 'review_note'])
        application = pending
    else:
        application = PlayerEscortApplication.objects.create(
            player=player,
            experience=experience,
            evidence_urls=evidence_urls,
        )

    qualification.status = PlayerEscortQualification.STATUS_PENDING
    qualification.review_note = ''
    qualification.reviewed_at = None
    qualification.reviewed_by = None
    qualification.save(update_fields=['status', 'review_note', 'reviewed_at', 'reviewed_by', 'updated_at'])
    return application, qualification


@transaction.atomic
def approve_escort_application(application, operator=None, review_note=''):
    application = PlayerEscortApplication.objects.select_for_update().select_related('player').get(pk=application.pk)
    if application.status != PlayerEscortApplication.STATUS_PENDING:
        raise ValidationError({'detail': '只有待审核的护航申请可以通过'})

    now = timezone.now()
    reviewer = operator if getattr(operator, 'is_authenticated', False) else None
    application.status = PlayerEscortApplication.STATUS_APPROVED
    application.reject_reason = ''
    application.review_note = (review_note or '')[:300]
    application.reviewed_at = now
    application.reviewed_by = reviewer
    application.save(update_fields=['status', 'reject_reason', 'review_note', 'reviewed_at', 'reviewed_by'])

    qualification = get_escort_qualification(application.player)
    qualification.status = PlayerEscortQualification.STATUS_APPROVED
    qualification.review_note = application.review_note
    qualification.reviewed_at = now
    qualification.reviewed_by = reviewer
    qualification.save(update_fields=['status', 'review_note', 'reviewed_at', 'reviewed_by', 'updated_at'])
    return application, qualification


@transaction.atomic
def reject_escort_application(application, reason, operator=None, review_note=''):
    application = PlayerEscortApplication.objects.select_for_update().select_related('player').get(pk=application.pk)
    if application.status != PlayerEscortApplication.STATUS_PENDING:
        raise ValidationError({'detail': '只有待审核的护航申请可以拒绝'})

    now = timezone.now()
    reviewer = operator if getattr(operator, 'is_authenticated', False) else None
    reject_reason = (reason or '护航资格审核未通过，请补充材料后重新申请')[:300]
    application.status = PlayerEscortApplication.STATUS_REJECTED
    application.reject_reason = reject_reason
    application.review_note = (review_note or '')[:300]
    application.reviewed_at = now
    application.reviewed_by = reviewer
    application.save(update_fields=['status', 'reject_reason', 'review_note', 'reviewed_at', 'reviewed_by'])

    qualification = get_escort_qualification(application.player)
    qualification.status = PlayerEscortQualification.STATUS_REJECTED
    qualification.review_note = reject_reason
    qualification.reviewed_at = now
    qualification.reviewed_by = reviewer
    qualification.save(update_fields=['status', 'review_note', 'reviewed_at', 'reviewed_by', 'updated_at'])
    return application, qualification
