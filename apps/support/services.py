"""Transactional complaint operations shared by authenticated APIs and staff UI."""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import APIException, ValidationError
from apps.orders.models import Order
from .models import Complaint, ComplaintMessage, ComplaintStatusHistory
from rest_framework.exceptions import PermissionDenied


def enforce_rate(queryset, setting_name, default):
    from datetime import timedelta
    from django.conf import settings
    from django.utils import timezone
    from rest_framework.exceptions import Throttled
    limit = max(1, int(getattr(settings, setting_name, default)))
    if queryset.filter(created_at__gte=timezone.now() - timedelta(hours=1)).count() >= limit:
        raise Throttled(wait=3600, detail='提交过于频繁，请稍后再试')


def available_attachments(user, ids):
    from .models import ComplaintAttachment
    if len(ids) > 3 or len(ids) != len(set(ids)):
        raise ValidationError({'attachment_ids': '最多3张且不可重复'})
    rows = ComplaintAttachment.objects.select_for_update().filter(pk__in=ids, uploader=user, complaint__isnull=True, message__isnull=True).order_by('pk')
    if len(list(rows)) != len(ids):
        raise ValidationError({'attachment_ids': '附件不存在、不属于本人或已被使用'})
    return rows


def private_storage():
    from django.conf import settings
    from django.core.files.storage import FileSystemStorage
    from pathlib import Path
    root = Path(getattr(settings, 'COMPLAINT_PRIVATE_ROOT', '/var/lib/touchi/complaint-attachments')).resolve()
    if root == Path(settings.MEDIA_ROOT).resolve() or Path(settings.MEDIA_ROOT).resolve() in root.parents:
        raise RuntimeError('投诉附件目录不得位于公开MEDIA_ROOT')
    return FileSystemStorage(location=str(root))


@transaction.atomic
def upload_attachment(user, uploaded):
    import io
    import uuid
    import warnings
    from PIL import Image, UnidentifiedImageError
    from django.core.files.base import ContentFile
    from .models import ComplaintAttachment
    get_user_model().objects.select_for_update().get(pk=user.pk)
    enforce_rate(ComplaintAttachment.objects.filter(uploader=user), 'COMPLAINT_UPLOAD_LIMIT_PER_HOUR', 30)
    if not uploaded or uploaded.size > 5 * 1024 * 1024:
        raise ValidationError({'file': '请上传不超过5MB的图片'})
    raw = uploaded.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise ValidationError({'file': '图片过大'})
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(raw))
            fmt = image.format
            if fmt not in ('JPEG', 'PNG', 'WEBP') or image.width * image.height > 20000000:
                raise ValueError('unsupported image')
            image.verify()
            image = Image.open(io.BytesIO(raw))
            image.load()
            clean = Image.new('RGB', image.size)
            clean.paste(image.convert('RGB'))
            output = io.BytesIO()
            clean.save(output, format='JPEG', quality=90)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValidationError({'file': '仅支持有效的JPEG、PNG、WEBP图片'})
    content = output.getvalue()
    storage = private_storage()
    name = storage.save(uuid.uuid4().hex + '.jpg', ContentFile(content))
    try:
        return ComplaintAttachment.objects.create(uploader=user, name=uploaded.name[:140] + '.jpg', storage_name=name, content_type='image/jpeg', size=len(content))
    except Exception:
        storage.delete(name)
        raise


class Conflict(APIException):
    status_code = 409
    default_detail = '请求与已有材料冲突'


@transaction.atomic
def create_complaint(user, data):
    # Serialize requests for a user across workers, not process-local cache locks.
    get_user_model().objects.select_for_update().get(pk=user.pk)
    data = dict(data)
    order_no = data.pop('order_no', '')
    attachment_ids = data.pop('attachment_ids', [])
    existing = Complaint.objects.filter(user=user, client_request_id=data['client_request_id']).first()
    if existing:
        if (any(getattr(existing, k) != v for k, v in data.items()) or (existing.order.order_no if existing.order else '') != order_no or sorted(existing.attachments.filter(message__isnull=True).values_list('pk', flat=True)) != sorted(attachment_ids)):
            raise Conflict('同一请求编号不能用于不同材料')
        return existing, False
    enforce_rate(Complaint.objects.filter(user=user), 'COMPLAINT_CREATE_LIMIT_PER_HOUR', 10)
    order = None
    if order_no:
        order = Order.objects.filter(Q(boss_user=user) | Q(order_players__player__user=user), order_no=order_no).distinct().first()
        if order is None:
            raise ValidationError({'order_no': '订单不存在或不属于当前用户'})
    attachments = available_attachments(user, attachment_ids)
    complaint = Complaint.objects.create(user=user, order=order, **data)
    attachments.update(complaint=complaint)
    ComplaintStatusHistory.objects.create(complaint=complaint, from_status='', to_status='pending', operator=user)
    return complaint, True


@transaction.atomic
def transition_complaint(complaint, status, actor, reason=''):
    if not actor.is_active or not actor.is_staff or not actor.has_perm('support.change_complaint'):
        raise PermissionDenied('无工单处理权限')
    if status not in dict(Complaint.STATUS_CHOICES):
        raise ValidationError({'status': '无效状态'})
    locked = Complaint.objects.select_for_update().get(pk=complaint.pk)
    _transition(locked, status, actor, reason)
    complaint.status = locked.status
    return locked


def _transition(complaint, status, actor, reason=''):
    if complaint.status == status:
        return
    ComplaintStatusHistory.objects.create(complaint=complaint, from_status=complaint.status, to_status=status, operator=actor, reason=reason[:300])
    complaint.status = status
    complaint.save(update_fields=['status', 'updated_at'])


@transaction.atomic
def add_complaint_message(complaint, author, content, is_internal=False, attachment_ids=None):
    get_user_model().objects.select_for_update().get(pk=author.pk)
    enforce_rate(ComplaintMessage.objects.filter(author=author), 'COMPLAINT_MESSAGE_LIMIT_PER_HOUR', 30)
    complaint = Complaint.objects.select_for_update().get(pk=complaint.pk)
    staff = author.is_active and author.is_staff and author.has_perm('support.change_complaint')
    if (author.pk != complaint.user_id or is_internal) and not staff:
        raise PermissionDenied('无工单处理权限')
    if complaint.status == 'closed':
        raise Conflict('工单已关闭，不可追加材料')
    attachments = available_attachments(author, attachment_ids or [])
    message = ComplaintMessage.objects.create(complaint=complaint, author=author, content=content, is_internal=is_internal)
    attachments.update(complaint=complaint, message=message)
    if author.pk == complaint.user_id and complaint.status == 'awaiting_user':
        _transition(complaint, 'processing', author, '用户补充材料')
    complaint.save(update_fields=['updated_at'])
    return message
