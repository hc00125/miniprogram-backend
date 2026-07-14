from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.orders.models import OrderPlayer


class Command(BaseCommand):
    help = '将超过10分钟仍未确认进入老板房间的陪玩标记为超时待核实'

    def handle(self, *args, **options):
        updated = OrderPlayer.objects.filter(
            room_join_status=OrderPlayer.ROOM_ENTRY_PENDING,
            room_join_deadline__isnull=False,
            room_join_deadline__lte=timezone.now(),
        ).update(room_join_status=OrderPlayer.ROOM_ENTRY_OVERDUE)
        self.stdout.write(self.style.SUCCESS(f'已标记 {updated} 条进入房间超时记录'))
