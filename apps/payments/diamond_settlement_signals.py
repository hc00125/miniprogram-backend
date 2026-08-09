import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.wallet.diamonds import yuan_to_diamonds
from apps.wallet.models import ClientWalletLedger
from apps.wallet.services import get_or_lock_wallet, qmoney, write_wallet_ledger

from .models import Payment
from .virtualpay import VIRTUAL_CHANNEL


logger = logging.getLogger(__name__)
DIAMOND_SETTLEMENT_KEY = 'diamond_settlement'


def settle_virtual_payment_diamonds(payment_or_id):
    """把微信直接支付统一记成“人民币购钻石 → 钻石支付订单”。

    两条内部钱包流水在同一事务内一正一负，因此老板可用余额净变化始终为0。
    普通退款只需要恢复订单消费，即自然得到钻石；微信原路退款则撤销人民币
    资金侧，不需要先人为制造一笔钱包退款。
    """
    payment_id = getattr(payment_or_id, 'pk', payment_or_id)

    with transaction.atomic():
        payment = (
            Payment.objects
            .select_for_update(of=('self',))
            .select_related('order', 'order__boss_user')
            .get(pk=payment_id)
        )
        if payment.channel != VIRTUAL_CHANNEL:
            return payment
        if payment.status not in {'paid', 'refunded'}:
            return payment

        order = payment.order
        boss_user = order.boss_user if order and order.boss_user_id else None
        profile = getattr(boss_user, 'client_profile', None) if boss_user else None
        if not profile:
            raise RuntimeError(f'微信虚拟支付 {payment.payment_no} 找不到老板钱包账户')

        amount = qmoney(payment.amount)
        if amount <= 0:
            raise RuntimeError(f'微信虚拟支付 {payment.payment_no} 金额不正确')

        wallet = get_or_lock_wallet(profile)

        credit, _credit_created = write_wallet_ledger(
            wallet,
            ClientWalletLedger.TYPE_VIRTUAL_PURCHASE_CREDIT,
            amount,
            reference_type='payment',
            reference_id=payment.payment_no,
            note=f'微信直付 {payment.payment_no}：人民币自动兑换钻石（内部结算）',
        )
        spend, _spend_created = write_wallet_ledger(
            wallet,
            ClientWalletLedger.TYPE_VIRTUAL_ORDER_PAYMENT,
            -amount,
            reference_type='payment',
            reference_id=payment.payment_no,
            note=f'微信直付 {payment.payment_no}：钻石支付订单 {payment.order_id}（内部结算）',
        )

        payload = payment.notify_payload if isinstance(payment.notify_payload, dict) else {}
        current = payload.get(DIAMOND_SETTLEMENT_KEY) or {}
        settled = {
            **(current if isinstance(current, dict) else {}),
            'status': 'settled',
            'amount_yuan': str(amount),
            'diamonds': yuan_to_diamonds(amount),
            'purchase_credit_ledger_id': credit.id,
            'order_payment_ledger_id': spend.id,
            'purchase_credit_reference': payment.payment_no,
            'order_payment_reference': payment.payment_no,
            'settled_at': (current.get('settled_at') if isinstance(current, dict) else None)
            or timezone.now().isoformat(),
        }
        Payment.objects.filter(pk=payment.pk).update(
            notify_payload={**payload, DIAMOND_SETTLEMENT_KEY: settled},
            updated_at=timezone.now(),
        )
        payment.notify_payload = {**payload, DIAMOND_SETTLEMENT_KEY: settled}
        return payment


@receiver(post_save, sender=Payment)
def settle_paid_wechat_virtual_payment(sender, instance, **kwargs):
    if instance.channel != VIRTUAL_CHANNEL or instance.status != 'paid':
        return
    # 支付确认与钻石桥接必须一起成功；失败时抛出异常让外层支付事务回滚，
    # 后续主动查单会安全重试，且流水 reference_id 保证幂等。
    settle_virtual_payment_diamonds(instance.pk)
