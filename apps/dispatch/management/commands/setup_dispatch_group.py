from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from apps.dispatch.models import Customer


class Command(BaseCommand):
    help = 'Create the least-privilege dispatch group; never modifies users or passwords.'

    @transaction.atomic
    def handle(self, *args, **options):
        content_type = ContentType.objects.get_for_model(Customer)
        permission, _ = Permission.objects.get_or_create(content_type=content_type, codename='use_console', defaults={'name':'使用客服派单工作台'})
        group, created = Group.objects.get_or_create(name='客服派单工作台')
        group.permissions.add(permission)
        self.stdout.write('客服派单工作台权限组已就绪；未添加任何账号、未变更密码或管理员标记。')
