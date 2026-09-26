"""Pre-order pricing for the existing /boss/order creation route."""
from decimal import Decimal
from rest_framework.exceptions import ValidationError
from .services import build_order_plan
from .surcharges import quote_amount
from .checkout_gates import checkout_blockers
from apps.wallet.spend_service import digest


def quote_order(data):
    from .duration_services import prepare_duration_payload
    prepared, hours, hourly = prepare_duration_payload(data)
    plan = build_order_plan(prepared)
    surcharge = quote_amount(data.get('surcharge_diamonds', 0), plan['required_players'])
    if surcharge['amount_diamonds'] and (plan['targeted_order'] or plan['designated_player_ids']):
        raise ValidationError({'code': 'SURCHARGE_DESIGNATED_FLOW_UNAVAILABLE',
            'detail': '指定邀请与专属商品有独立履约流程，暂未接入整单加价'})
    base = plan['total_price']
    # Match the existing Order pre_save pricing without persisting a preview.
    # Fixed-tier specs multiply their per-hour total by booked_hours there.
    if not plan['targeted_order'] and plan['spec'] is not None:
        from .models import Order
        from .designated_pricing import calculate_designated_pricing
        from apps.common.money import money
        preview = Order(package=plan['package'], spec_id=plan['spec'].pk,
            required_players=plan['required_players'], designated_players=plan['designated_player_ids'],
            designated_types=plan['designated_types'])
        priced = calculate_designated_pricing(preview)
        if priced:
            base = priced['total']
    if hourly:
        from apps.common.money import money
        base = money(base * Decimal(hours))
    if surcharge['amount_diamonds']:
        from .surcharge_lifecycle import settlement_snapshot
        settlement_snapshot(surcharge)  # Reject inexact fish split before payment.
    total = base + Decimal(surcharge['amount_yuan'])
    if not total.is_finite() or base < 0 or total > Decimal('9999999999.99'):
        checkout_error('INVALID_AMOUNT')
    q = {'contract_version': 'surcharge-v2', 'base_amount_yuan': format(base, '.2f'),
        'surcharge_amount_yuan': surcharge['amount_yuan'], 'total_amount_yuan': format(total, '.2f'),
        'base_amount_diamonds': format(base * 10, '.1f'),
        'total_amount_diamonds': format(total * 10, '.1f'),
        'required_players': plan['required_players'], 'surcharge': surcharge}
    # Bind every price component and identity, not only the coincidental total.
    q['quote_version'] = digest({'quote': q, 'items': [
        {'package': i['package'].pk, 'spec': i['spec'].pk if i['spec'] else None,
         'quantity': i['quantity'], 'unit_price': str(i['unit_price'])} for i in plan['order_items']],
        'addons': plan['normalized_addons'], 'addon_id': data.get('addon_id'),
        'booked_hours': str(plan['booked_hours'])})
    q['blockers'] = checkout_blockers() if surcharge['amount_diamonds'] else []
    q['can_submit'] = not q['blockers']
    return q


def checkout_error(code, detail=None, conflict=False):
    from rest_framework.exceptions import APIException
    exc = APIException({'code': code, 'detail': detail or code})
    exc.status_code = 409 if conflict else 400
    raise exc


def prepare_creation(data, user):
    """Caller owns atomic block. Serialize per buyer before checking the key."""
    from django.contrib.auth import get_user_model
    from apps.catalog.models import Package, PackageSpec, Addon
    from .surcharge_models import OrderCheckout
    amount = data.get('surcharge_diamonds', 0)
    key = data.get('idempotency_key')
    if not amount and not key:
        return None, None
    if not user or not user.is_authenticated or not key or len(key) > 100:
        checkout_error('IDEMPOTENCY_KEY_REQUIRED')
    get_user_model().objects.select_for_update().get(pk=user.pk)
    fingerprint = digest({k: v for k, v in data.items() if k != 'idempotency_key'})
    previous = OrderCheckout.objects.filter(boss=user, idempotency_key=key).first()
    if previous:
        if previous.request_digest != fingerprint:
            checkout_error('IDEMPOTENCY_CONFLICT', conflict=True)
        return previous.order, None
    items = data.get('items') or [{'package_id': data.get('package_id'), 'spec_id': data.get('spec_id')}]
    list(Package.objects.select_for_update().filter(pk__in=[i['package_id'] for i in items]).order_by('pk'))
    list(PackageSpec.objects.select_for_update().filter(pk__in=[i.get('spec_id') for i in items if i.get('spec_id')]).order_by('pk'))
    addons = [i.get('addon_id') for i in data.get('addon_details') or []] + [data.get('addon_id')]
    list(Addon.objects.select_for_update().filter(pk__in=[i for i in addons if i]).order_by('pk'))
    q = quote_order(data)
    if amount and not q['can_submit']:
        checkout_error(q['blockers'][0])
    if amount and data.get('quote_version') != q['quote_version']:
        checkout_error('PRICE_CHANGED', '请重新报价并确认总额', conflict=True)
    if Decimal(q['total_amount_yuan']) > Decimal('9999999999.99'):
        checkout_error('INVALID_AMOUNT')
    return None, (q, key, fingerprint)


def persist_creation(order, creation):
    if creation is None:
        return
    import uuid
    from .surcharge_models import OrderCheckout, OrderSurcharge
    q, key, fingerprint = creation
    if Decimal(str(order.total_amount)) != Decimal(q['base_amount_yuan']):
        checkout_error('PRICE_CHANGED', '实际原单价格与确认报价不一致，请重新报价', conflict=True)
    sc = None
    if q['surcharge']['amount_diamonds']:
        from .surcharge_lifecycle import settlement_snapshot
        sc = OrderSurcharge.objects.create(order=order, boss=order.boss_user,
            surcharge_no='SC' + uuid.uuid4().hex, amount_diamonds=q['surcharge']['amount_diamonds'],
            amount_yuan=Decimal(q['surcharge_amount_yuan']), idempotency_key='checkout:' + str(order.pk),
            request_digest=fingerprint, allocation_snapshot=q['surcharge'],
            policy_snapshot=settlement_snapshot(q['surcharge']))
    OrderCheckout.objects.create(order=order, boss=order.boss_user, surcharge=sc,
        idempotency_key=key, request_digest=fingerprint, quote_snapshot=q,
        base_amount=Decimal(q['base_amount_yuan']), surcharge_amount=Decimal(q['surcharge_amount_yuan']),
        total_amount=Decimal(q['total_amount_yuan']))


def checkout_data(order):
    from .surcharge_models import OrderCheckout
    item = OrderCheckout.objects.filter(order=order).select_related('attempt', 'surcharge').first()
    if item is None:
        return None
    state = item.status
    if item.attempt_id:
        state = {'completed': 'paid', 'succeeded': 'processing', 'prepared': 'processing',
                 'dispatching': 'unknown'}.get(item.attempt.status, item.attempt.status)
    from .surcharge_models import OrderCheckoutRefund
    refund = OrderCheckoutRefund.objects.filter(checkout=item).select_related('base_refund').first()
    refund_data = None if refund is None else {
        'refund_no': refund.refund_no, 'local_status': 'succeeded',
        'base_amount_yuan': format(refund.base_refund.amount, '.2f'),
        'surcharge_amount_yuan': format(refund.surcharge_amount, '.2f'),
        'total_amount_yuan': format(refund.base_refund.amount + refund.surcharge_amount, '.2f'),
        'remote_status': refund.remote_status}
    return {'contract_version': 'surcharge-v2', 'payment_status': state, 'refund': refund_data,
        'idempotency_key': item.idempotency_key, 'quote_version': item.quote_snapshot['quote_version'],
        'base_amount_yuan': format(item.base_amount, '.2f'),
        'surcharge_amount_yuan': format(item.surcharge_amount, '.2f'),
        'total_amount_yuan': format(item.total_amount, '.2f'),
        'total_amount_diamonds': format(item.total_amount * 10, '.1f'),
        'attempt_id': item.attempt.external_id if item.attempt_id else None,
        'surcharge': item.surcharge.allocation_snapshot if item.surcharge_id else item.quote_snapshot['surcharge'],
        'blockers': checkout_blockers() if item.surcharge_id else []}
