from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand


ROLE_DEFINITIONS = {
    '运营管理员': {
        'accounts': {'view_clientprofile', 'change_clientprofile', 'view_viptier', 'view_bossconsumptionledger', 'add_bossconsumptionledger'},
        'catalog': {'view_packagegroup', 'view_package', 'change_package', 'view_packageimage', 'change_packageimage', 'view_packagespec', 'change_packagespec', 'view_addon', 'view_playertype'},
        'orders': {'view_order', 'change_order', 'view_orderplayer', 'change_orderplayer', 'view_orderdesignation', 'change_orderdesignation'},
        'players': {'view_player', 'change_player', 'view_playerapplication', 'change_playerapplication'},
    },
    '财务管理员': {
        'accounts': {'view_clientprofile', 'view_viptier', 'view_bossconsumptionledger', 'add_bossconsumptionledger'},
        'earnings': 'all',
        'payments': 'all',
        'orders': {'view_order', 'view_orderplayer'},
        'players': {'view_player'},
    },
    '客服管理员': {
        'accounts': {'view_clientprofile', 'view_viptier', 'view_bossconsumptionledger'},
        'orders': {'view_order', 'change_order', 'view_orderplayer', 'change_orderplayer', 'view_orderdesignation', 'change_orderdesignation'},
        'players': {'view_player', 'change_player', 'view_playerapplication', 'change_playerapplication'},
        'catalog': {'view_packagegroup', 'view_package', 'view_packagespec', 'view_playertype'},
    },
    '内容管理员': {
        'catalog': 'all',
    },
    '只读审计': {
        'accounts': 'view',
        'catalog': 'view',
        'earnings': 'view',
        'orders': 'view',
        'payments': 'view',
        'players': 'view',
    },
}


class Command(BaseCommand):
    help = '创建或刷新运营、财务、客服、内容与只读审计后台角色。'

    def add_arguments(self, parser):
        parser.add_argument('--clear', action='store_true', help='先清空这些角色的旧权限再重新分配')

    def handle(self, *args, **options):
        for group_name, app_rules in ROLE_DEFINITIONS.items():
            group, created = Group.objects.get_or_create(name=group_name)
            if options['clear']:
                group.permissions.clear()

            permissions = Permission.objects.none()
            for app_label, rule in app_rules.items():
                app_permissions = Permission.objects.filter(content_type__app_label=app_label)
                if rule == 'all':
                    selected = app_permissions
                elif rule == 'view':
                    selected = app_permissions.filter(codename__startswith='view_')
                else:
                    selected = app_permissions.filter(codename__in=rule)
                permissions = permissions | selected

            permission_list = list(permissions.distinct())
            group.permissions.add(*permission_list)
            state = '创建' if created else '刷新'
            self.stdout.write(self.style.SUCCESS(f'{state}角色：{group_name}（{len(permission_list)}项权限）'))

        self.stdout.write(self.style.WARNING('超级管理员请保持独立账号，不要加入上述日常角色。'))
