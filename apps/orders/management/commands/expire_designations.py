from django.core.management.base import BaseCommand

from apps.orders.designations import expire_due_designations


class Command(BaseCommand):
    help = '将已超时的指定陪玩邀请转为公开抢单名额'

    def handle(self, *args, **options):
        expired = expire_due_designations()
        self.stdout.write(self.style.SUCCESS(f'已处理 {expired} 条超时指定邀请'))
