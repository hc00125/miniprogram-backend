from django.shortcuts import get_object_or_404
from django.db.models import Sum, F
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from apps.accounts.authentication import LenientJWTAuthentication
from .models import Order
from .surcharges import surcharge_readiness
from apps.wallet.api_contract import endpoint, StrictInteger, validated
from rest_framework import serializers
from .surcharge_models import OrderSurcharge


class SurchargeInput(serializers.Serializer):
    amount_diamonds = StrictInteger(min_value=1)
    idempotency_key = serializers.CharField(max_length=100)
    code = serializers.CharField(max_length=256, required=False, allow_blank=True)


def surcharge_data(item):
    status = item.status
    if item.attempt_id and item.attempt.status == 'unknown':
        status = 'unknown'
    return {'surcharge_no': item.surcharge_no, 'payment_status': status,
        'amount_diamonds': item.amount_diamonds, 'refunded_diamonds': item.refunded_diamonds,
        'blockers': item.policy_snapshot.get('blockers', [])}


@endpoint(['GET'])
def surcharge_by_key(request, order_no):
    item = get_object_or_404(OrderSurcharge, order__order_no=order_no, boss=request.user,
        idempotency_key=request.query_params.get('idempotency_key', ''))
    return Response(surcharge_data(item))


@endpoint(['GET'])
def surcharge_detail(request, order_no, surcharge_no):
    item = get_object_or_404(OrderSurcharge, order__order_no=order_no,
        surcharge_no=surcharge_no, boss=request.user)
    return Response(surcharge_data(item))


@endpoint(['GET', 'POST'])
def surcharge_overview(request, order_no):
    # No staff bypass and no lazy account/expiry mutation in this read endpoint.
    order = get_object_or_404(Order, order_no=order_no, boss_user=request.user)
    if request.method == 'POST':
        from .surcharge_payment import pay
        return Response(surcharge_data(pay(order_no, request.user, **validated(SurchargeInput, request))))
    records = order.surcharges.all()
    confirmed = records.filter(status__in=('paid', 'partially_refunded', 'refunded'))
    paid = confirmed.aggregate(total=Sum(F('amount_diamonds') - F('refunded_diamonds')))['total'] or 0
    refunded = confirmed.aggregate(total=Sum('refunded_diamonds'))['total'] or 0
    processing = records.filter(status__in=('processing', 'unknown')).aggregate(total=Sum('amount_diamonds'))['total'] or 0
    paginator = PageNumberPagination()
    paginator.page_size = 20
    page = paginator.paginate_queryset(records.order_by('-id'), request)
    data = surcharge_readiness(order)
    data.update({'order_no': order.order_no, 'required_players': order.required_players,
                 'paid_diamonds': paid, 'refunded_diamonds': refunded, 'processing_diamonds': processing,
                 'amount_options_diamonds': [],
                 'records': [{'surcharge_no': item.surcharge_no, 'payment_status': item.status,
                              'amount_diamonds': item.amount_diamonds, 'refunded_diamonds': item.refunded_diamonds,
                              'created_at': item.created_at.isoformat()} for item in page],
                 'count': paginator.page.paginator.count,
                 'next': paginator.get_next_link(), 'previous': paginator.get_previous_link()})
    return Response(data)
