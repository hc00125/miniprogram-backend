from django.core.management.base import BaseCommand

from apps.orders.payment_deadlines import expire_due_unpaid_orders


class Command(BaseCommand):
    help = '自动取消进入待支付超过10分钟仍未完成付款的订单'

    def handle(self, *args, **options):
        expired = expire_due_unpaid_orders()
        self.stdout.write(self.style.SUCCESS(f'已自动取消 {expired} 张超时未支付订单'))
