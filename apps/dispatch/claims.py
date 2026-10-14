from datetime import timedelta
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.views.decorators.cache import never_cache
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound, Throttled
from rest_framework_simplejwt.authentication import JWTAuthentication
from apps.accounts.models import ClientProfile
from apps.accounts.access import ensure_account_operational
from apps.orders.models import Order, OrderStatusLog
from .models import Customer, HistoryClaim
from .serializers import StrictInput
from .services import check_operator, conflict
from rest_framework import serializers


class ClaimInput(StrictInput):
    customer_id = serializers.IntegerField(min_value=1, max_value=9223372036854775807)
    message = serializers.CharField(max_length=300)


class ReviewInput(StrictInput):
    decision = serializers.ChoiceField(choices=['approve', 'reject'])
    verified = serializers.BooleanField(required=False, default=False)
    note = serializers.CharField(max_length=300)


def claim_data(item):
    # Never expose customer name, internal note or historical orders before approval.
    return {'id': item.pk, 'customer_id': item.requested_number, 'status': item.status,
            'message': item.message, 'created_at': item.created_at, 'reviewed_at': item.reviewed_at}


@api_view(['GET', 'POST'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
@never_cache
def client_claims(request):
    if not ClientProfile.objects.filter(user=request.user).exists():
        raise PermissionDenied('请使用微信登录的小程序账号')
    if request.method == 'GET':
        customer = Customer.objects.filter(user=request.user).first()
        return Response({'customer': None if customer is None else {'id': customer.pk, 'nickname': customer.nickname},
                         'claims': [claim_data(c) for c in HistoryClaim.objects.filter(applicant=request.user).order_by('-pk')[:30]]})
    ensure_account_operational(request.user)
    serializer = ClaimInput(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        existing = HistoryClaim.objects.filter(applicant=request.user, requested_number=data['customer_id'], status='pending').first()
        if existing:
            return Response(claim_data(existing), status=200)
        if HistoryClaim.objects.filter(applicant=request.user, created_at__gte=timezone.now()-timedelta(days=1)).count() >= 5:
            raise Throttled(detail='今天提交的认领申请较多，请联系原客服核实')
        item = HistoryClaim.objects.create(applicant=request.user, requested_number=data['customer_id'],
            customer=Customer.objects.filter(pk=data['customer_id']).first(), message=data['message'])
    return Response(claim_data(item), status=201)


@transaction.atomic
def review_claim(pk, data, actor):
    check_operator(actor)
    original = HistoryClaim.objects.filter(pk=pk).first()
    if original is None:
        raise NotFound('申请不存在')
    # User -> customer -> claim is the shared binding lock order.
    get_user_model().objects.select_for_update().get(pk=original.applicant_id)
    customer = Customer.objects.select_for_update().filter(pk=original.customer_id).first()
    item = HistoryClaim.objects.select_for_update().get(pk=pk)
    desired = 'approved' if data['decision'] == 'approve' else 'rejected'
    if item.status != 'pending':
        if item.status == desired:
            return item
        conflict('该申请已经处理，请刷新状态')
    if desired == 'approved':
        if data.get('verified') is not True:
            raise ValidationError({'detail': '必须先通过原群或私聊核实申请人身份'})
        if customer is None:
            conflict('客户档案不存在，请驳回并让老板联系原客服')
        if customer.user_id not in (None, item.applicant_id):
            conflict('此客户档案已由其他账号认领，不能重复绑定')
        if Customer.objects.filter(user_id=item.applicant_id).exclude(pk=customer.pk).exists():
            conflict('此账号已关联其他档案，请管理员核对，不要按昵称自动合并')
        if not item.applicant.is_active:
            conflict('申请账号已停用')
        ensure_account_operational(item.applicant)
        historical = Order.objects.filter(customer=customer, source=Order.SOURCE_STAFF)
        if historical.exclude(boss_user_id=None).exclude(boss_user_id=item.applicant_id).exists():
            conflict('历史订单存在其他账号归属，请管理员核对')
        customer.user_id = item.applicant_id
        customer.save(update_fields=['user'])
        # Metadata-only update: never replay order save/payment/earnings/VIP signals.
        numbers = list(historical.filter(boss_user_id=None).values_list('id', flat=True))
        historical.filter(boss_user_id=None).update(boss_user_id=item.applicant_id)
        OrderStatusLog.objects.bulk_create([OrderStatusLog(order_id=n, operator=actor,
            from_status='', to_status='', reason=f'客服核实认领申请#{item.pk}，只关联客户账号，账务不变') for n in numbers])
    item.status = desired
    item.reviewed_by = actor
    item.review_note = data['note']
    item.reviewed_at = timezone.now()
    item.save(update_fields=['status', 'reviewed_by', 'review_note', 'reviewed_at'])
    return item
