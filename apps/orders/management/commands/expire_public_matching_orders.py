from django.core.management.base import BaseCommand

from apps.orders.matching import expire_public_matching_orders


class Command(BaseCommand):
    help = (
        '取消首次进入公开抢单大厅满2小时仍未凑齐的未付款公开订单。'
        '建议由服务器定时任务每分钟执行一次。指定陪玩订单不在处理范围内。'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit',
            type=int,
            default=500,
            help='单次最多处理的到期订单数，默认500',
        )

    def handle(self, *args, **options):
        limit = max(1, int(options.get('limit') or 500))
        expired = expire_public_matching_orders(limit=limit)
        self.stdout.write(self.style.SUCCESS(
            f'公开匹配2小时超时处理完成：自动取消 {expired} 张订单。'
        ))
