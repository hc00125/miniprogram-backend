from django.core.management.base import BaseCommand

from apps.orders.designations import expire_due_designations
from apps.orders.payment_deadlines import expire_due_unpaid_orders


class Command(BaseCommand):
    help = '处理超时指定邀请，并自动取消超过10分钟仍未支付的订单'

    def handle(self, *args, **options):
        expired_designations = expire_due_designations()
        expired_orders = expire_due_unpaid_orders()
        self.stdout.write(self.style.SUCCESS(
            f'已处理 {expired_designations} 条超时指定邀请，自动取消 {expired_orders} 张超时未支付订单'
        ))
