from django.core.management.base import BaseCommand

from apps.orders.room_entry_requeue import expire_due_room_entries


class Command(BaseCommand):
    help = '将付款后10分钟仍未确认进入房间的陪玩视为拒单，并把空缺名额转回公共抢单大厅'

    def handle(self, *args, **options):
        updated = expire_due_room_entries()
        self.stdout.write(self.style.SUCCESS(f'已释放 {updated} 个进入房间超时名额并转入公共抢单大厅'))
