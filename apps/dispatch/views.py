import json
from functools import wraps
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from rest_framework.exceptions import APIException, ValidationError
from .models import Customer


def console_access(user):
    return bool(user.is_authenticated and user.is_active and user.has_perm('dispatch.use_console'))


def endpoint(methods):
    def decorate(func):
        @never_cache
        @csrf_protect
        @require_http_methods(methods)
        @wraps(func)
        def wrapped(request, *args, **kwargs):
            if not console_access(request.user):
                return JsonResponse({'detail': '请使用具有客服派单权限的账号登录'}, status=403)
            try:
                return func(request, *args, **kwargs)
            except APIException as exc:
                payload = exc.detail if isinstance(exc.detail, dict) else {'detail': exc.detail}
                return JsonResponse(payload, status=exc.status_code)
        return wrapped
    return decorate


def body(request, serializer_class):
    try:
        if len(request.body) > 16000:
            raise ValueError()
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise ValueError()
    except (ValueError, UnicodeDecodeError):
        raise ValidationError({'detail': '请求格式不正确'})
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def customer_data(customer):
    return {'id': customer.pk, 'ref': f'customer:{customer.pk}', 'nickname': customer.nickname,
            'note': customer.note, 'user_id': customer.user_id, 'bound': bool(customer.user_id)}


@endpoint(['GET', 'POST'])
def customers(request):
    from .serializers import CustomerInput
    if request.method == 'GET':
        from django.db.models import Q
        from apps.accounts.models import ClientProfile
        text = request.GET.get('q', '').strip()[:100]
        qs = Customer.objects.order_by('-id')
        profiles = ClientProfile.objects.filter(user__is_active=True, user__dispatch_customer__isnull=True).order_by('-id')
        if text:
            match = Q(nickname__icontains=text) | Q(note__icontains=text)
            if text.isascii() and text.isdigit() and len(text) <= 18:
                match |= Q(pk=int(text))
            qs = qs.filter(match)
            profiles = profiles.filter(nickname__icontains=text)
        results = [customer_data(c) for c in qs[:20]]
        results += [{'ref': f'user:{p.user_id}', 'nickname': p.nickname or f'用户{p.user_id}',
                     'note': '已有小程序账号', 'bound': True, 'user_id': p.user_id, 'id': None} for p in profiles[:20]]
        return JsonResponse({'results': results})
    data = body(request, CustomerInput)
    customer = Customer.objects.create(created_by=request.user, **data)
    return JsonResponse(customer_data(customer), status=201)


@endpoint(['POST'])
def quote(request):
    from .serializers import QuoteInput
    from .services import quote as quote_service
    return JsonResponse(quote_service(body(request, QuoteInput), request.user))


@endpoint(['GET', 'POST'])
def orders(request):
    from .serializers import OrderInput
    from .services import create_dispatch
    if request.method == 'GET':
        return order_list(request)
    order, created = create_dispatch(body(request, OrderInput), request.user)
    return JsonResponse({'order_no': order.order_no, 'status': order.status}, status=201 if created else 200)


@endpoint(['POST'])
def review_claim(request, pk):
    from .claims import ReviewInput, review_claim as review, claim_data
    return JsonResponse(claim_data(review(pk, body(request, ReviewInput), request.user)))


@endpoint(['GET'])
def catalog(request):
    from apps.catalog.models import Package
    from .services import money
    rows = []
    for p in Package.objects.filter(is_active=True, selling_mode=Package.SELLING_MODE_PUBLIC, is_custom=False).prefetch_related('specs').order_by('id'):
        specs = [{'id': s.pk, 'name': s.name, 'price_yuan': format(money(s.price), '.2f')} for s in p.specs.all() if s.is_active]
        if p.specs.all() and not specs:
            continue
        rows.append({'id': p.pk, 'name': p.name, 'price_yuan': format(money(p.base_price), '.2f'),
                     'players': p.player_count, 'max_hours': 1 if p.product_type == Package.PRODUCT_TYPE_GUARANTEE else 24, 'specs': specs})
    return JsonResponse({'packages': rows})


def order_data(order, detail=False):
    from .services import money
    nickname = order.customer.nickname if order.customer_id else ''
    if not nickname and order.boss_user_id and hasattr(order.boss_user, 'client_profile'):
        nickname = order.boss_user.client_profile.nickname
    result = {'order_no': order.order_no, 'source': order.source, 'nickname': nickname or '历史客户',
        'customer_id': order.customer_id, 'status': order.status, 'paid': order.paid,
        'amount_yuan': format(money(order.total_amount), '.2f'), 'hours': order.booked_hours,
        'package_name': order.package_name_snapshot or order.package.name, 'spec_name': order.spec_name_snapshot or '',
        'created_by': order.created_by.get_username() if order.created_by_id else '历史记录',
        'created_at': order.created_at, 'required_players': order.required_players,
        'players': [op.player.name for op in order.order_players.all()]}
    if detail:
        from .models import DispatchReceipt
        receipt = DispatchReceipt.objects.filter(order=order).first()
        from .cancellations import cancellation_block_reason
        blocked = cancellation_block_reason(order)
        result.update(can_cancel_dispatch=not bool(blocked), cancel_block_reason=blocked,
            cancel_reason=order.cancel_reason or '', canceled_at=order.canceled_at,
            offline_refund_status='unconfirmed' if order.source == 'staff' and order.status == order.STATUS_CANCELLED else None)
        result.update(game_id=order.game_id or '', boss_note=order.boss_note or '',
            receipt_note=receipt.receipt_note if receipt else '',
            logs=list(order.status_logs.order_by('-id').values('reason', 'created_at')[:30]))
    return result


def order_query():
    from apps.orders.models import Order
    return Order.objects.select_related('customer', 'created_by', 'package', 'boss_user__client_profile').prefetch_related('order_players__player')


def order_list(request):
    from django.core.paginator import Paginator
    from django.db.models import Q
    from apps.orders.models import Order
    qs = order_query().order_by('-created_at', '-id')
    source = request.GET.get('source', 'staff')
    if source in ('staff', 'self', 'legacy'):
        qs = qs.filter(source=source)
    state = request.GET.get('status', '')
    if state:
        qs = qs.filter(status=state)
    if request.GET.get('mine') == '1':
        qs = qs.filter(created_by=request.user)
    text = request.GET.get('q', '').strip()[:100]
    if text:
        qs = qs.filter(Q(order_no__icontains=text) | Q(customer__nickname__icontains=text) | Q(boss_user__client_profile__nickname__icontains=text))
    page = Paginator(qs, 25).get_page(request.GET.get('page', '1'))
    return JsonResponse({'results': [order_data(o) for o in page], 'page': page.number,
        'pages': page.paginator.num_pages, 'count': page.paginator.count})


@endpoint(['POST'])
def cancel_order(request, order_no):
    from .serializers import CancelInput
    from .cancellations import cancel_dispatch
    order = cancel_dispatch(order_no, body(request, CancelInput), request.user)
    return JsonResponse(order_data(order_query().get(pk=order.pk), detail=True))


@endpoint(['GET'])
def order_detail(request, order_no):
    order = order_query().filter(order_no=order_no).first()
    if order is None:
        return JsonResponse({'detail': '订单不存在'}, status=404)
    return JsonResponse(order_data(order, detail=True))


@endpoint(['GET'])
def submission(request, key):
    from .models import DispatchReceipt
    receipt = DispatchReceipt.objects.filter(request_key=key, created_by=request.user).first()
    if receipt is None:
        return JsonResponse({'detail': '尚未查到该请求；请稍后查询或使用原请求重试，勿换新请求重复派单'}, status=404)
    return JsonResponse(order_data(order_query().get(pk=receipt.order_id)))


@endpoint(['GET'])
def claims(request):
    from .models import HistoryClaim
    from .claims import claim_data
    rows = []
    for c in HistoryClaim.objects.select_related('customer', 'applicant__client_profile').filter(status='pending').order_by('id')[:100]:
        row = claim_data(c)
        row.update(customer_name=c.customer.nickname if c.customer_id else '客户编号不存在',
                   customer_note=c.customer.note if c.customer_id else '',
                   applicant_name=c.applicant.client_profile.nickname if hasattr(c.applicant, 'client_profile') else '无小程序资料',
                   applicant_user_id=c.applicant_id)
        rows.append(row)
    return JsonResponse({'results': rows})



