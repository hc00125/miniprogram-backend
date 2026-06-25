import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from apps.catalog.models import Addon, Package, PackageGroup, PlayerType


class Command(BaseCommand):
    help = 'Seed default catalog data for local miniprogram testing.'

    def handle(self, *args, **options):
        group_specs = [
            ('端游保底单', 1),
            ('趣味玩法单', 2),
            ('特色大金单', 3),
            ('单局保底单', 4),
            ('清图累计单', 5),
            ('极限机密单', 6),
        ]
        groups = {}
        for name, sort_order in group_specs:
            group, _ = PackageGroup.objects.update_or_create(
                name=name,
                defaults={'sort_order': sort_order, 'is_active': True},
            )
            groups[name] = group

        packages = [
            ('端游保底单', '体验单·电视台保底888W', 1, 58, '体验单：电视台保底888W，适合首次下单。'),
            ('端游保底单', '体验单·电视台保底1088W', 1, 68, '体验单：电视台保底1088W，适合首次下单。'),
            ('端游保底单', '猛攻单·电视台保底1288W', 1, 88, '猛攻单：电视台保底1288W。'),
            ('端游保底单', '猛攻单·电视台保底1488W', 1, 98, '猛攻单：电视台保底1488W。'),
            ('端游保底单', '猛攻单·电视台保底1688W', 1, 128, '猛攻单：电视台保底1688W。'),
            ('端游保底单', '猛攻单·电视台保底2688W', 1, 188, '猛攻单：电视台保底2688W。'),
            ('端游保底单', '猛攻单·电视台保底3988W', 1, 288, '猛攻单：电视台保底3988W。'),
            ('端游保底单', '猛攻单·电视台保底5888W', 1, 399, '猛攻单：电视台保底5888W。'),
            ('端游保底单', '猛攻单·电视台保底10001W', 1, 688, '猛攻单：电视台保底10001W。'),
            ('趣味玩法单', '摸金圣手·保200W', 1, 20, '基础定价20R，保200W，按对应规则结算。'),
            ('趣味玩法单', '机械收集者·6把满配', 1, 30, '基础定价30R，累计带出6把指定满配装备结单。'),
            ('趣味玩法单', '超级保底单·保3200W', 1, 360, '基础定价360R，保3200W，按对应规则结算。'),
            ('趣味玩法单', '黄金收割者', 1, 368, '定价368R，无保底，累计收集8个不同黄金物品结单。'),
            ('趣味玩法单', '大金天平', 1, 388, '基础定价388R，无上限保底，按大金天平规则结单。'),
            ('特色大金单', '定制大金·必出一大金', 1, 88, '88R保必出一大金。'),
            ('特色大金单', '累计双大金', 1, 99, '累计摸出双大金，未达成则继续服务。'),
            ('特色大金单', '单局双大金', 1, 298, '298R保单局双大金。'),
            ('特色大金单', '单局三大金', 1, 688, '688R保单局三大金。'),
            ('特色大金单', '单局四大金', 1, 1688, '1688R保单局四大金。'),
            ('单局保底单', '单局保底718W', 1, 108, '单局保底718W，未达成则继续服务。'),
            ('单局保底单', '单局保底818W', 1, 210, '单局保底818W，未达成则继续服务。'),
            ('单局保底单', '单局保底918W', 1, 288, '单局保底918W，未达成则继续服务。'),
            ('单局保底单', '单局保底1018W', 1, 358, '单局保底1018W，未达成则继续服务。'),
            ('单局保底单', '单局保底1288W', 1, 488, '单局保底1288W，未达成则继续服务。'),
            ('清图累计单', '电视台清图单', 1, 68, '电视台清图，不清图不结单；满五把送一把888保底。'),
            ('清图累计单', '累计三大金', 1, 148, '累计摸出三大金，未达成则继续服务。'),
            ('极限机密单', '极限大金·保底588W', 1, 188, '188R保底588W。'),
            ('极限机密单', '机密单·保底1亿', 1, 688, '机密单688R，不出机密保底1亿，三护。'),
            ('极限机密单', '机密单·三护48小时', 1, 788, '机密单788R，三护；时效48小时可暂停。'),
            ('极限机密单', '理想国单·三护12小时', 1, 1888, '理想国单1888R，三护；时效12小时可暂停。'),
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
