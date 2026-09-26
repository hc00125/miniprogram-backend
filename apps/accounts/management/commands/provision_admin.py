import getpass
import os

from django.contrib.auth.models import Group, User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


ROLE_NAMES = ['运营管理员', '财务管理员', '客服管理员', '内容管理员', '只读审计']


class Command(BaseCommand):
    help = '创建或更新独立后台账号，并绑定最小权限角色。密码从环境变量或交互输入读取。'

    def add_arguments(self, parser):
        parser.add_argument('--username', required=True)
        parser.add_argument('--email', default='')
        parser.add_argument('--role', choices=ROLE_NAMES)
        parser.add_argument('--superuser', action='store_true')
        parser.add_argument('--password-env', default='ADMIN_INITIAL_PASSWORD')
        parser.add_argument('--rotate-password', action='store_true', help='已有账号也重新设置密码')

    def handle(self, *args, **options):
        if not options['superuser'] and not options.get('role'):
            raise CommandError('普通后台账号必须指定 --role')

        call_command('bootstrap_admin_roles')
        username = options['username'].strip()
        user, created = User.objects.get_or_create(username=username)
        user.email = options['email'].strip()
        user.is_active = True
        user.is_staff = True
        user.is_superuser = bool(options['superuser'])

        if not user.is_superuser:
            group = Group.objects.get(name=options['role'])
            user.groups.set([group])
            user.user_permissions.clear()
        else:
            user.groups.clear()

        must_set_password = created or options['rotate_password'] or not user.has_usable_password()
        if must_set_password:
            env_name = options['password_env']
            password = os.environ.get(env_name) or getpass.getpass(f'请输入 {username} 的新密码：')
            if not password:
                raise CommandError('密码不能为空')
            try:
                validate_password(password, user=user)
            except ValidationError as exc:
                raise CommandError('；'.join(exc.messages)) from exc
            user.set_password(password)

        user.save()
        role_text = '超级管理员' if user.is_superuser else options['role']
        action = '创建' if created else '更新'
        self.stdout.write(self.style.SUCCESS(f'已{action}后台账号 {username}，角色：{role_text}'))
        if must_set_password:
            self.stdout.write(self.style.WARNING('请首次登录后再次更换密码，并删除终端中的临时环境变量。'))
