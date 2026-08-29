import os

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.catalog.models import Package

from apps.accounts.models import BossConsumptionLedger, ClientProfile, VipTier


REQUIRED_GROUPS = {'运营管理员', '财务管理员', '客服管理员', '内容管理员', '只读审计'}


class Command(BaseCommand):
    help = '检查P2上线所需数据库、VIP、后台角色、媒体目录和商品素材配置。'

    def handle(self, *args, **options):
        failures = []
        warnings = []

        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                cursor.fetchone()
            self.stdout.write(self.style.SUCCESS('✓ 数据库连接正常'))
        except Exception as exc:
            failures.append(f'数据库连接失败：{exc}')

        tier_count = VipTier.objects.filter(is_active=True).count()
        if tier_count < 1:
            failures.append('没有启用的VIP等级')
        else:
            self.stdout.write(self.style.SUCCESS(f'✓ 已启用 {tier_count} 个VIP等级'))

        missing_groups = REQUIRED_GROUPS - set(Group.objects.filter(name__in=REQUIRED_GROUPS).values_list('name', flat=True))
        if missing_groups:
            failures.append('后台角色未初始化：' + '、'.join(sorted(missing_groups)))
        else:
            self.stdout.write(self.style.SUCCESS('✓ 后台最小权限角色已初始化'))

        media_root = str(getattr(settings, 'MEDIA_ROOT', '') or '')
        if not media_root:
            warnings.append('MEDIA_ROOT 未配置')
        elif not os.path.isdir(media_root):
            warnings.append(f'MEDIA_ROOT 目录不存在：{media_root}')
        elif not os.access(media_root, os.W_OK):
            failures.append(f'MEDIA_ROOT 不可写：{media_root}')
        else:
            self.stdout.write(self.style.SUCCESS(f'✓ 媒体目录可写：{media_root}'))

        active_packages = Package.objects.filter(is_active=True)
        missing_cover = active_packages.filter(cover_url__in=['', None], image_url__in=['', None]).count()
        missing_detail = active_packages.filter(detail_images=[]).count()
        if missing_cover:
            warnings.append(f'{missing_cover} 个上架商品缺少旧封面字段，请确认已使用“商品图片”上传区配置封面')
        if missing_detail:
            warnings.append(f'{missing_detail} 个上架商品没有旧详情长图字段，请确认已配置上传详情图或详情文字')

        self.stdout.write(
            f'数据概览：老板 {ClientProfile.objects.count()} 人，消费流水 {BossConsumptionLedger.objects.count()} 条，上架商品 {active_packages.count()} 个'
        )
        for warning in warnings:
            self.stdout.write(self.style.WARNING('⚠ ' + warning))
        if failures:
            for failure in failures:
                self.stderr.write(self.style.ERROR('✗ ' + failure))
            raise CommandError('P2健康检查未通过')
        self.stdout.write(self.style.SUCCESS('P2健康检查通过'))
