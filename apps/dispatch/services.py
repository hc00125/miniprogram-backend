import hashlib
import json
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError, PermissionDenied, APIException
from apps.accounts.models import ClientProfile
from apps.accounts.access import ensure_account_operational
from apps.catalog.models import Package, PackageSpec
from apps.common.money import money
from apps.orders.models import Order, OrderStatusLog
from apps.orders.kook_notifications import order_event_boundary
from .models import Customer, DispatchReceipt


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str, separators=(',', ':')).encode()).hexdigest()


def conflict(detail):
    error = APIException({'detail': detail})
    error.status_code = 409
    raise error


def check_operator(actor):
    if not actor.is_authenticated or not actor.is_active or not actor.has_perm('dispatch.use_console'):
        raise PermissionDenied('无客服派单权限')


def customer_reference(ref, actor, create=False):
    kind, number = ref.split(':')
    number = int(number)
    if kind == 'customer':
        qs = Customer.objects.select_for_update() if create else Customer.objects
        customer = qs.filter(pk=number).first()
        if not customer:
            raise ValidationError({'detail': '客户不存在，请重新选择'})
        if customer.user_id:
            ensure_account_operational(customer.user)
        return customer
    profile = ClientProfile.objects.select_related('user').filter(user_id=number, user__is_active=True).first()
    if not profile:
        raise ValidationError({'detail': '小程序用户不存在，请重新选择'})
    ensure_account_operational(profile.user)
    if not create:
        return profile
    get_user_model().objects.select_for_update().get(pk=number)
    customer, _ = Customer.objects.get_or_create(user=profile.user,
        defaults={'nickname': profile.nickname or f'用户{number}', 'created_by': actor})
    return customer


def order_payload(data):
    package = Package.objects.filter(pk=data['package_id'], is_active=True, selling_mode=Package.SELLING_MODE_PUBLIC).first()
    if not package or package.is_custom:
        raise ValidationError({'detail': '请选择上架的公开固定价格商品'})
    if package.specs.exists() and not data.get('spec_id'):
        raise ValidationError({'detail': '请选择商品规格'})
    return {'package_id': package.pk, 'spec_id': data.get('spec_id'), 'quantity': data['hours'],
            'booked_hours': data['hours'], 'boss_note': data.get('boss_note', ''), 'game_id': data.get('game_id', ''),
            'boss_wechat': ''}


def quote(data, actor):
    check_operator(actor)
    customer_reference(data['customer_ref'], actor)
    from apps.orders.checkout import quote_order
    q = quote_order(order_payload(data))
    amount = Decimal(q['total_amount_yuan'])
    if amount <= 0 or amount > Decimal('9999999999.99'):
        raise ValidationError({'detail': '商品价格不可用，请联系管理员核对'})
    return {'total_amount_yuan': format(amount, '.2f'), 'required_players': q['required_players'],
            'hours': data['hours'], 'quote_version': digest({'catalog': q['quote_version'],
                'customer_ref': data['customer_ref'], 'game_id': data.get('game_id', ''), 'boss_note': data.get('boss_note', '')})}


@transaction.atomic
@order_event_boundary
def create_dispatch(data, actor):
    check_operator(actor)
    get_user_model().objects.select_for_update().get(pk=actor.pk)
    fingerprint = digest(data)
    previous = DispatchReceipt.objects.select_related('order').filter(request_key=data['request_key']).first()
    if previous:
        if previous.created_by_id != actor.pk or previous.request_digest != fingerprint:
            conflict('该发单请求已使用，请先核对原订单，不要重复发单')
        return previous.order, False
    if data.get('received') is not True:
        raise ValidationError({'detail': '请先核实线下收款'})
    customer = customer_reference(data['customer_ref'], actor, create=True)
    Package.objects.select_for_update().get(pk=data['package_id'])
    if data.get('spec_id'):
        list(PackageSpec.objects.select_for_update().filter(pk=data['spec_id']))
    q = quote(data, actor)
    if q['quote_version'] != data['quote_version']:
        conflict('商品价格或派单内容已变化，请重新核价和确认收款')
    from apps.orders.duration_services import create_order
    payload = order_payload(data)
    payload['boss_wechat'] = f'customer:{customer.pk}'
    order = create_order(payload, user=customer.user, skip_content_security=True,
        source_context={'source': Order.SOURCE_STAFF, 'customer': customer, 'created_by': actor})
    if money(order.total_amount) != money(q['total_amount_yuan']):
        conflict('报价与订单金额不一致，已阻止派单，请联系管理员')
    order.paid = True
    order.payment_method = 'staff_offline'
    order.payment_confirmed_at = timezone.now()
    order.payment_confirmed_by = actor
    order.save(update_fields=['paid', 'payment_method', 'payment_confirmed_at', 'payment_confirmed_by'])
    DispatchReceipt.objects.create(order=order, request_key=data['request_key'], request_digest=fingerprint,
        received_amount=money(order.total_amount), receipt_note=data['receipt_note'], created_by=actor)
    OrderStatusLog.objects.create(order=order, from_status=order.status, to_status=order.status,
        operator=actor, reason='客服核实线下收款后派单；未扣小程序钻石')
    return order, True
