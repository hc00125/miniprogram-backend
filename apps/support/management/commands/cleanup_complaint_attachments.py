"""Only expired, unbound uploads; default dry-run, never deletes case evidence."""
from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from apps.support.models import ComplaintAttachment
from apps.support.services import private_storage


class Command(BaseCommand):
    help = '清理超过保留期的未关联投诉附件；默认预览，--execute 才删除'

    def add_arguments(self, parser):
        parser.add_argument('--execute', action='store_true')

    def handle(self, *args, **options):
        hours = int(getattr(settings, 'COMPLAINT_ORPHAN_RETENTION_HOURS', 168))
        if hours < 24:
            raise CommandError('孤儿附件保留期不得低于24小时')
        cutoff = timezone.now() - timedelta(hours=hours)
        candidates = ComplaintAttachment.objects.filter(complaint__isnull=True, message__isnull=True, created_at__lt=cutoff)
        count = 0
        for pk in candidates.values_list('pk', flat=True).iterator():
            with transaction.atomic():
                item = ComplaintAttachment.objects.select_for_update().filter(pk=pk, complaint__isnull=True, message__isnull=True, created_at__lt=cutoff).first()
                if item is None:
                    continue
                count += 1
                if options['execute']:
                    # Keep the DB row on a storage error so the next run can retry.
                    private_storage().delete(item.storage_name)
                    item.delete()
        self.stdout.write(f"{'deleted' if options['execute'] else 'dry-run'}: {count}")
