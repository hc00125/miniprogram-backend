import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from apps.catalog.models import Addon, Package, PackageGroup, PlayerType


class Command(BaseCommand):
    help = 'Seed default catalog data for local miniprogram testing.'

    def handle(self, *args, **options):
        group_specs = [
            ('推荐套餐', 1),
            ('基础陪玩套餐', 2),
            ('趣味单', 3),
            ('特色单', 4),
        ]
        groups = {}
        for name, sort_order in group_specs:
            group, _ = PackageGroup.objects.update_or_create(
                name=name,
                defaults={'sort_order': sort_order, 'is_active': True},
            )
            groups[name] = group

        obsolete_group_names = [
            '默认推荐',
            '端游保底单',
            '趣味玩法单',
            '特色大金单',
            '单局保底单',
            '清图累计单',
            '极限机密单',
        ]
        PackageGroup.objects.filter(name__in=obsolete_group_names).update(is_active=False)

        guarantee_description = (
            '基础价58。规格：电视台保底888w/58、1088w/68、1288w/88、1488w/98、'
            '1688w/128、2688w/188、3988w/288、5888w/399、10001w/688。'
        )
        packages = [
            ('推荐套餐', '六套六弹', 1, 60, '推荐套餐：6套6弹配置。'),
            ('特色单', '暗区突围端游保底单', 1, 58, guarantee_description),
        ]
        for group_name, name, count, price, description in packages:
            Package.objects.update_or_create(
                name=name,
                defaults={
                    'player_count': count,
                    'base_price': price,
                    'description': description,
                    'group': groups[group_name],
                    'is_active': True,
                    'is_custom': False,
                },
            )

        obsolete_package_names = [
            '四套娱乐陪',
            '五套五蛋陪',
            '六套五弹陪',
            '体验单·电视台保底888W',
            '体验单·电视台保底1088W',
            '猛攻单·电视台保底1288W',
            '猛攻单·电视台保底1488W',
            '猛攻单·电视台保底1688W',
            '猛攻单·电视台保底2688W',
            '猛攻单·电视台保底3988W',
            '猛攻单·电视台保底5888W',
            '猛攻单·电视台保底10001W',
            '摸金圣手·保200W',
            '机械收集者·6把满配',
            '超级保底单·保3200W',
            '黄金收割者',
            '大金天平',
            '定制大金·必出一大金',
            '累计双大金',
            '单局双大金',
            '单局三大金',
            '单局四大金',
            '单局保底718W',
            '单局保底818W',
            '单局保底918W',
            '单局保底1018W',
            '单局保底1288W',
            '电视台清图单',
            '累计三大金',
            '极限大金·保底588W',
            '机密单·保底1亿',
            '机密单·三护48小时',
            '理想国单·三护12小时',
        ]
        Package.objects.filter(name__in=obsolete_package_names).update(is_active=False)

        addons = [
            ('女陪', 8, 2),
            ('技术陪', 10, 3),
            ('金牌陪', 20, 4),
            ('明星陪', 25, 5),
        ]
        for name, price, priority in addons:
            Addon.objects.update_or_create(
                name=name,
                defaults={'price_per_player': price, 'priority': priority, 'is_active': True},
            )
            PlayerType.objects.update_or_create(
                name=name,
                defaults={
                    'priority': priority,
                    'can_view_addon_priority': priority,
                    'price_extra': 0,
                    'is_active': True,
                },
            )

        if not User.objects.filter(username='admin').exists():
            password = os.environ.get('DEMO_ADMIN_PASSWORD')
            if password:
                User.objects.create_superuser('admin', password=password)
                self.stdout.write(self.style.WARNING('Created dev admin from DEMO_ADMIN_PASSWORD.'))
            else:
                self.stdout.write(self.style.WARNING('Skipped dev admin creation: DEMO_ADMIN_PASSWORD is not set.'))

        self.stdout.write(self.style.SUCCESS('Seed data ready.'))
