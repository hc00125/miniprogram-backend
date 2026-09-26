"""Durable external refund dispatch for committed full checkout cancellations.

Unknown/dispatching are never sent again. No balance-delta recovery or fake
query evidence. The existing user-authenticated sync entry supplies session_key.
"""
from django.db import connection, transaction
from apps.orders.surcharge_models import OrderCheckoutRefund
from apps.payments.models import Refund


def sync_checkout_refunds(profile, session_key):
    if connection.in_atomic_block:
        raise RuntimeError('Remote refund requires committed local cancellation')
    ids = list(OrderCheckoutRefund.objects.filter(checkout__boss_id=profile.user_id,
        remote_status__in=('prepared', 'succeeded')).order_by('pk').values_list('pk', flat=True))
    count = 0
    for pk in ids:
        dispatch = False
        with transaction.atomic():
            intent = OrderCheckoutRefund.objects.select_for_update().get(pk=pk)
            if intent.remote_status == 'prepared':
                intent.remote_status = 'dispatching'
                intent.save(update_fields=['remote_status', 'updated_at'])
                dispatch = True
        if dispatch:
            from .coin_sync import user_xpay_post
            try:
                evidence = user_xpay_post('/xpay/cancel_currency_pay', intent.request_payload,
                    session_key, idempotent_success=False)
                succeeded = isinstance(evidence, dict) and evidence.get('errcode') == 0
            except Exception:
                evidence = {}
                succeeded = False
            with transaction.atomic():
                intent = OrderCheckoutRefund.objects.select_for_update().get(pk=pk)
                intent.remote_status = 'succeeded' if succeeded else 'unknown'
                intent.evidence = evidence
                intent.save(update_fields=['remote_status', 'evidence', 'updated_at'])
        if intent.remote_status == 'succeeded':
            # May be replayed after a crash between remote evidence and local
            # source unlock. Never dispatch a succeeded request again.
            with transaction.atomic():
                intent = OrderCheckoutRefund.objects.select_for_update().get(pk=pk)
                refund = Refund.objects.select_for_update().get(pk=intent.base_refund_id)
                payload = dict(refund.notify_payload)
                if payload.get('wechat_coin_refund_status') != 'succeeded':
                    payload['wechat_coin_refund_status'] = 'succeeded'
                    payload['wechat_coin_remote_refund_order_id'] = intent.refund_no
                    Refund.objects.filter(pk=refund.pk).update(notify_payload=payload)
                    count += 1
    return count


def pending_checkout_refunds(profile):
    return OrderCheckoutRefund.objects.filter(checkout__boss_id=profile.user_id,
        remote_status__in=('prepared', 'dispatching', 'unknown')).exists()


def surcharge_refund_coin_map(profile):
    from decimal import Decimal
    result = {}
    for intent in OrderCheckoutRefund.objects.filter(checkout__boss_id=profile.user_id,
            remote_status='succeeded').iterator():
        amount = Decimal(intent.source_snapshot['surcharge_coin_yuan'])
        if amount:
            result[intent.refund_no] = (amount, int(intent.source_snapshot['original']['scale']))
    return result
