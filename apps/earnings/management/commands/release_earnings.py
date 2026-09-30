from django.core.management.base import BaseCommand

from apps.earnings.services import release_due_earnings


class Command(BaseCommand):
    help = '将已满审核期且未冻结的陪玩工资转为可提现鱼干'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=500)

    def handle(self, *args, **options):
        released = release_due_earnings(limit=max(1, options['limit']))
        self.stdout.write(self.style.SUCCESS(f'已释放 {released} 条工资记录'))
