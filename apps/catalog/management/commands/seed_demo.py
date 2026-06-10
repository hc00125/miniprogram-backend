from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from apps.catalog.models import Addon, Package, PackageGroup, PlayerType


class Command(BaseCommand):
    help = 'Seed default catalog data for local miniprogram testing.'

    def handle(self, *args, **options):
        group, _ = PackageGroup.objects.get_or_create(name='默认推荐', defaults={'sort_order': 1})
        packages = [
            ('四套娱乐陪', 4, 15, '默认娱乐陪玩套餐'),
            ('五套五蛋陪', 5, 25, '默认五人套餐'),
            ('六套五弹陪', 6, 30, '默认六人套餐'),
        ]
        for name, count, price, description in packages:
            Package.objects.get_or_create(
                name=name,
                defaults={'player_count': count, 'base_price': price, 'description': description, 'group': group},
            )

        addons = [
            ('女陪', 8, 2),
            ('技术陪', 10, 3),
            ('金牌陪', 20, 4),
            ('明星陪', 25, 5),
        ]
        for name, price, priority in addons:
            Addon.objects.get_or_create(name=name, defaults={'price_per_player': price, 'priority': priority})
            PlayerType.objects.get_or_create(
                name=name,
                defaults={'priority': priority, 'can_view_addon_priority': priority, 'price_extra': 0},
            )

        if not User.objects.filter(username='admin').exists():
            User.objects.create_superuser('admin', password='admin123')
            self.stdout.write(self.style.WARNING('Created dev admin: admin / admin123'))

        self.stdout.write(self.style.SUCCESS('Seed data ready.'))
