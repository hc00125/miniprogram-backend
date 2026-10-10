from django.db.models import Sum
from apps.orders.models import Order
from .models import RechargeOrder
from .services import ZERO, qmoney


def _reserved_checkout_amount(profile, *, exclude_order_no=''):
    """Credited order-linked value cannot be reassigned to another purchase."""
    pending_order_nos = Order.objects.filter(boss_user=profile.user, paid=False,
        status=Order.STATUS_PENDING_PAYMENT).exclude(order_no=str(exclude_order_no or '')).values_list('order_no', flat=True)
    total = RechargeOrder.objects.filter(profile=profile, status=RechargeOrder.STATUS_CREDITED,
        checkout_order_no__in=pending_order_nos).aggregate(total=Sum('amount')).get('total')
    return qmoney(total or ZERO)


def pay_order_with_coin_aware_balance(order_no, user, code='', user_ip='127.0.0.1', *,
        allow_credited_checkout_recovery=False, idempotency_key=None):
    """Keep the existing DTO/route; all original coin captures are durable."""
    from apps.orders.surcharge_models import OrderCheckout
    if OrderCheckout.objects.filter(order__order_no=order_no, surcharge__isnull=False).exists():
        from apps.orders.checkout_payment import pay
        return pay(order_no, user, code=code)
    from .spend_models import OrderWalletSpend
    from .coin_sync import coin_backed_wallet_amount, has_pending_coin_refunds
    from .services import pay_order_with_balance
    from .diamonds import coin_units_per_yuan
    profile = getattr(user, 'client_profile', None)
    if (profile and not OrderWalletSpend.objects.filter(order__order_no=order_no).exists()
            and not coin_backed_wallet_amount(profile) and not has_pending_coin_refunds(profile)):
        # Preserve the old cash-only transaction and ledger category. Its
        # locked recheck rejects racing coin credits and shared reservations.
        result = pay_order_with_balance(order_no, user)
        result.update(wechat_coin_units=0, wechat_coin_units_per_yuan=coin_units_per_yuan(),
            wechat_coin_diamonds='0.0')
        from apps.orders.checkout import checkout_data
        checkout = checkout_data(Order.objects.get(order_no=order_no))
        if checkout is not None:
            result['checkout'] = checkout
        return result
    from .order_spend import pay
    return pay(order_no, user, code=code, user_ip=user_ip,
        allow_credited_checkout_recovery=allow_credited_checkout_recovery,
        idempotency_key=idempotency_key)
