from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum

from apps.orders.models import Order
from apps.payments.models import Payment, Refund
from apps.payments.refund_integrity import settle_balance_refund
from apps.payments.services import create_refund
from apps.wallet.models import ClientWalletLedger


CENT = Decimal('0.01')


def qmoney(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


class Command(BaseCommand):
    help = (
        '对账余额支付退款：补处理待处理/退款中的余额退款；可选报告或创建'
        '“已取消但没有退款记录”的历史订单退款。默认仅预览。'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='实际执行；不加此参数仅输出预览',
        )
        parser.add_argument(
            '--create-missing',
            action='store_true',
            help='为已取消且明确仍有可退余额的订单创建剩余金额退款；必须同时使用 --apply',
        )
        parser.add_argument(
            '--order-no',
            default='',
            help='仅处理指定订单号，便于先修复单个用户反馈',
        )

    def handle(self, *args, **options):
        apply_changes = bool(options['apply'])
        create_missing = bool(options['create_missing'])
        order_no = str(options.get('order_no') or '').strip()
        if create_missing and not apply_changes:
            raise CommandError('--create-missing 必须与 --apply 一起使用')

        pending = Refund.objects.filter(
            payment__channel='balance',
            status__in=[Refund.STATUS_PENDING, Refund.STATUS_PROCESSING],
        ).select_related('payment', 'order')
        if order_no:
            pending = pending.filter(order_id=order_no)

        pending_count = 0
        for refund in pending.iterator(chunk_size=200):
            pending_count += 1
            self.stdout.write(
                f'[待补入账] order={refund.order_id} refund={refund.refund_no} '
                f'amount=¥{qmoney(refund.amount)} status={refund.status}'
            )
            if apply_changes:
                settled = settle_balance_refund(refund.pk, operator=refund.created_by)
                self.stdout.write(self.style.SUCCESS(
                    f'  已入账：{settled.refund_no} → {settled.status}'
                ))

        payments = Payment.objects.filter(
            channel='balance',
            order__status=Order.STATUS_CANCELLED,
            status__in=['paid', 'refunded'],
        ).select_related('order')
        if order_no:
            payments = payments.filter(order_id=order_no)

        missing_count = 0
        for payment in payments.iterator(chunk_size=200):
            active_total = payment.refunds.filter(
                status__in=[
                    Refund.STATUS_PENDING,
                    Refund.STATUS_PROCESSING,
                    Refund.STATUS_SUCCEEDED,
                ]
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
            remaining = qmoney(payment.amount) - qmoney(active_total)
            if remaining <= 0:
                continue
            missing_count += 1
            has_refund_ledger = ClientWalletLedger.objects.filter(
                wallet__profile__user_id=payment.order.boss_user_id,
                entry_type=ClientWalletLedger.TYPE_REFUND_IN,
                reference_id__in=payment.refunds.values_list('refund_no', flat=True),
            ).exists()
            self.stdout.write(self.style.WARNING(
                f'[缺退款记录] order={payment.order_id} payment={payment.payment_no} '
                f'paid=¥{qmoney(payment.amount)} active_refunded=¥{qmoney(active_total)} '
                f'remaining=¥{remaining} existing_refund_ledger={has_refund_ledger}'
            ))
            if create_missing:
                refund = create_refund(
                    payment.payment_no,
                    remaining,
                    reason='历史已取消余额订单对账补退',
                    operator=None,
                )
                self.stdout.write(self.style.SUCCESS(
                    f'  已创建并入账：{refund.refund_no} amount=¥{qmoney(refund.amount)}'
                ))

        mode = '执行完成' if apply_changes else '预览完成（未修改数据）'
        self.stdout.write(self.style.SUCCESS(
            f'{mode}：待补入账 {pending_count} 笔；缺退款记录 {missing_count} 笔。'
        ))
