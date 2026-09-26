import uuid
from django.conf import settings
from django.db import transaction
from django.db.models import F, Sum
from apps.gifts.models import (Gift, GiftTransfer, GiftInventoryLot, GiftInventoryLedger,
                               GiftTransferAllocation, GiftEarning)
from apps.wallet import spend_service as spend
from apps.wallet.services import get_or_lock_wallet
from .purchases import policy, profile_for, recipient_for, lock_authorization
from .configuration import resolve_commission
from .income import credit_entitlement


def quote(user, *, gift_code, quantity, recipient_id):
    """Read-only stock quote; it neither purchases nor reserves any diamonds."""
    profile_for(user)
    p = policy()
    if type(quantity) is not int or not 1 <= quantity <= p['max_quantity']:
        spend.fail('INVALID_REQUEST')
    player = recipient_for(user, recipient_id, p)
    # Sale visibility and sendability differ: owned delisted gifts remain usable.
    gift = Gift.objects.filter(code=gift_code, kind='gift', send_enabled=True).first()
    if not gift:
        spend.fail('GIFT_UNAVAILABLE')
    commission = resolve_commission(player.pk, gift.pk)
    available = GiftInventoryLot.objects.filter(owner=user, gift=gift,
        status='active', remaining__gt=0).aggregate(total=Sum(F('remaining') - F('reserved')))['total'] or 0
    blockers = []
    if not getattr(settings, 'GIFT_INVENTORY_SEND_ENABLED', False):
        blockers.append('FEATURE_DISABLED')
    if available < quantity:
        blockers.append('INSUFFICIENT_INVENTORY')
    return {'gift_code': gift_code, 'quantity': quantity, 'recipient_id': player.pk,
        'commission_version': commission['version'], 'available_quantity': available,
        'payment_diamonds': 0, 'policy_version': p['version'],
        'can_submit': not blockers, 'blockers': blockers}


def deliver_direct(record):
    # A successful debit must not deliver to a now-restricted account. Leave
    # persisted succeeded evidence + reservation for audited recovery instead.
    recipient_for(record.buyer, record.recipient_id, {'recipient_ids': [record.recipient_id]})
    transfer = GiftTransfer.objects.create(transfer_no='GT' + uuid.uuid4().hex,
        sender=record.buyer, recipient=record.recipient, gift=record.gift, quantity=record.quantity,
        source='direct', purchase=record, idempotency_key='purchase:' + record.purchase_no,
        request_digest=record.request_digest, commission_snapshot=record.snapshot['commission'], status='delivered')
    credit_entitlement(transfer, eligible=record.snapshot['total_diamonds'] > 0, source=record.snapshot)


def transfer(user, *, gift_code, quantity, recipient_id, commission_version, idempotency_key):
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 100:
        spend.fail('INVALID_REQUEST')
    fingerprint = spend.digest(dict(gift_code=gift_code, quantity=quantity,
        recipient_id=recipient_id, commission_version=commission_version))
    profile = profile_for(user)
    with transaction.atomic():
        get_or_lock_wallet(profile)
        previous = GiftTransfer.objects.filter(sender=user, idempotency_key=idempotency_key).first()
        if previous:
            if previous.request_digest != fingerprint:
                spend.fail('IDEMPOTENCY_CONFLICT')
            return previous
        user, _ = lock_authorization(user, gift_code, recipient_id)
        profile_for(user)
        if not getattr(settings, 'GIFT_INVENTORY_SEND_ENABLED', False):
            spend.fail('FEATURE_DISABLED')
        p = policy()
        if type(quantity) is not int or not 1 <= quantity <= p['max_quantity']:
            spend.fail('INVALID_REQUEST')
        player = recipient_for(user, recipient_id, p)
        gift = Gift.objects.filter(code=gift_code, kind='gift', send_enabled=True).first()
        if not gift:
            spend.fail('GIFT_UNAVAILABLE')
        commission = resolve_commission(player.pk, gift.pk, expected_version=commission_version)
        lots = list(GiftInventoryLot.objects.select_for_update().filter(owner=user, gift=gift,
            status='active', remaining__gt=0).order_by('id'))
        if sum(l.remaining - l.reserved for l in lots) < quantity:
            spend.fail('INSUFFICIENT_INVENTORY')
        sent = GiftTransfer.objects.create(transfer_no='GT' + uuid.uuid4().hex, sender=user,
            recipient=player, gift=gift, quantity=quantity, source='inventory',
            idempotency_key=idempotency_key, request_digest=fingerprint,
            commission_snapshot=commission, status='delivered')
        remaining = quantity
        for lot in lots:
            take = min(remaining, lot.remaining - lot.reserved)
            if not take:
                continue
            lot.remaining -= take
            lot.save(update_fields=['remaining', 'updated_at'])
            allocation = GiftTransferAllocation.objects.create(transfer=sent, lot=lot, quantity=take, snapshot=lot.snapshot)
            GiftInventoryLedger.objects.create(lot=lot, event_key=f'send:{sent.transfer_no}:{lot.pk}',
                kind='send', quantity_delta=-take, actor=user, reason='库存赠送，不重复扣款')
            credit_entitlement(sent, allocation=allocation, eligible=lot.earnings_eligible, source=lot.snapshot)
            remaining -= take
            if not remaining:
                break
        return sent
