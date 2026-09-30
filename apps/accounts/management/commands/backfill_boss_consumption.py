from django.core.management.base import BaseCommand

from apps.orders.models import Order
from apps.payments.models import Refund

from apps.accounts.models import ClientProfile
from apps.accounts.vip import record_completed_order, record_successful_refund


class Command(BaseCommand):
    help = '根据历史已完成订单和退款记录，幂等补齐老板消费流水及VIP等级。'

    def add_arguments(self, parser):
        parser.add_argument('--profile-id', type=int)
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, **options):
        profile_id = options.get('profile_id')
        limit = max(0, options.get('limit') or 0)

        orders = Order.objects.filter(paid=True, status=Order.STATUS_COMPLETED).select_related('boss_user')
        refunds = Refund.objects.filter(status=Refund.STATUS_SUCCEEDED).select_related('order__boss_user')

        if profile_id:
            profile = ClientProfile.objects.get(pk=profile_id)
            orders = orders.filter(boss_user=profile.user)
            refunds = refunds.filter(order__boss_user=profile.user)

        orders = orders.order_by('created_at', 'id')
        refunds = refunds.order_by('created_at', 'id')
        if limit:
            orders = orders[:limit]

        order_created = 0
        refund_created = 0
        for order in orders.iterator() if not limit else orders:
            entry = record_completed_order(order)
            if entry:
                order_created += 1
        for refund in refunds.iterator():
            entry = record_successful_refund(refund)
            if entry:
                refund_created += 1

        self.stdout.write(self.style.SUCCESS(
            f'消费流水补齐完成：订单流水处理 {order_created} 条，退款流水处理 {refund_created} 条。重复执行不会重复记账。'
        ))
