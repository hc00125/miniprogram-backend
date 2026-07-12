from django.core.management.base import BaseCommand

from apps.earnings.services import create_order_earnings
from apps.orders.models import Order


class Command(BaseCommand):
    help = '为历史已完成且已支付的普通订单补建陪玩工资记录'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=500)

    def handle(self, *args, **options):
        limit = max(1, options['limit'])
        queryset = (
            Order.objects
            .filter(
                order_type=Order.ORDER_TYPE_NORMAL,
                status=Order.STATUS_COMPLETED,
                paid=True,
                player_earnings__isnull=True,
            )
            .distinct()
            .order_by('id')[:limit]
        )
        order_count = 0
        earning_count = 0
        for order in queryset:
            results = create_order_earnings(order, completed_at=order.end_time)
            if results:
                order_count += 1
                earning_count += len(results)
        self.stdout.write(
            self.style.SUCCESS(f'已处理 {order_count} 个订单，生成 {earning_count} 条工资记录')
        )
