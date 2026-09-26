"""Authenticated v1 gift API. Reads never dispatch or recover payments."""
from functools import wraps
from django.core.exceptions import ValidationError as ModelValidationError
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from apps.accounts.authentication import LenientJWTAuthentication
from apps.gifts.models import GiftPurchase, GiftInventoryLot, GiftTransfer, GiftEarning
from apps.wallet.api_contract import endpoint, validated, StrictInteger
from .services import purchases
from .services.transfers import transfer, quote as stock_quote


class QuoteInput(serializers.Serializer):
    gift_code = serializers.CharField(max_length=50)
    quantity = StrictInteger(min_value=1)
    mode = serializers.ChoiceField(choices=['direct', 'inventory'])
    recipient_id = StrictInteger(min_value=1, required=False, allow_null=True)


class PurchaseInput(QuoteInput):
    price_version = serializers.CharField(min_length=64, max_length=64)
    commission_version = StrictInteger(min_value=1, required=False, allow_null=True)
    idempotency_key = serializers.CharField(max_length=100)
    code = serializers.CharField(max_length=256, required=False, allow_blank=True)


class InventoryQuoteInput(serializers.Serializer):
    gift_code = serializers.CharField(max_length=50)
    quantity = StrictInteger(min_value=1)
    recipient_id = StrictInteger(min_value=1)


class TransferInput(InventoryQuoteInput):
    commission_version = StrictInteger(min_value=1)
    idempotency_key = serializers.CharField(max_length=100)


def purchase_data(record):
    attempt = record.attempt
    status = record.status
    if attempt and attempt.status in ('unknown', 'dispatching'):
        status = 'unknown' if attempt.status == 'unknown' else 'processing'
    return {'purchase_no': record.purchase_no, 'payment_status': status,
        'amount_diamonds': record.snapshot['total_diamonds'],
        'blockers': [attempt.blocker] if attempt and attempt.blocker else []}


@endpoint(['POST'])
def quotes(request):
    data = purchases.quote(request.user, **validated(QuoteInput, request))
    data.pop('commission_snapshot', None)
    return Response(data)


@endpoint(['POST'])
def inventory_quotes(request):
    return Response(stock_quote(request.user, **validated(InventoryQuoteInput, request)))


@endpoint(['POST'])
def buy(request):
    record = purchases.purchase(request.user, **validated(PurchaseInput, request))
    return Response(purchase_data(record))


@endpoint(['GET'])
def capabilities(request):
    from django.conf import settings
    blockers = []
    recipient_eligible = None
    p = {}
    try:
        p = purchases.policy()
        purchases.profile_for(request.user)
        raw_recipient = request.query_params.get('recipient_id')
        if raw_recipient is not None:
            try:
                recipient_id = int(raw_recipient)
            except (TypeError, ValueError):
                raise ValidationError({'code': 'INVALID_REQUEST'})
            purchases.recipient_for(request.user, recipient_id, p)
            recipient_eligible = True
    except ValidationError as exc:
        code = str(exc.detail.get('code', 'INVALID_REQUEST'))
        blockers.append(code)
        if code == 'RECIPIENT_INELIGIBLE':
            recipient_eligible = False
    return Response({'contract_version': '1.1-immediate-fish',
        'catalog_read': bool(getattr(settings, 'GIFT_CATALOG_ENABLED', True)),
        'records_read': True, 'inventory_read': True, 'quote_supported': True,
        'purchase_supported': True, 'inventory_transfer_supported': True,
        'purchase_enabled': bool(getattr(settings, 'GIFT_PURCHASE_ENABLED', False)) and not blockers,
        'inventory_transfer_enabled': bool(getattr(settings, 'GIFT_INVENTORY_SEND_ENABLED', False)) and not blockers,
        'recipient_eligible': recipient_eligible, 'blockers': blockers,
        'max_quantity': p.get('max_quantity'), 'max_diamonds': p.get('max_diamonds'),
        'daily_diamonds': p.get('daily_diamonds'), 'policy_version': p.get('version'),
        'unknown_recovery': 'manual_review', 'earnings_withdrawable': True,
        'earnings_currency': 'fish', 'inventory_quote_supported': True})


@endpoint(['GET'])
def purchase_by_key(request):
    return Response(purchase_data(get_object_or_404(GiftPurchase, buyer=request.user,
        idempotency_key=request.query_params.get('idempotency_key', ''))))


@endpoint(['GET'])
def transfer_by_key(request):
    return Response(transfer_data(get_object_or_404(transfer_records(), sender=request.user,
        idempotency_key=request.query_params.get('idempotency_key', '')), request))


@endpoint(['GET'])
def purchase_list(request):
    def render(record):
        return dict(purchase_data(record),
            gift_name=record.snapshot.get('name') or record.gift.name,
            image_url=gift_image(record.gift, request), quantity=record.quantity,
            mode=record.mode, recipient_name=record.recipient.name if record.recipient_id else None,
            created_at=record.created_at.isoformat())
    return paginated(request, GiftPurchase.objects.filter(buyer=request.user)
        .select_related('attempt', 'gift', 'recipient').order_by('-id'), render)


@endpoint(['GET'])
def purchase_detail(request, purchase_no):
    return Response(purchase_data(get_object_or_404(GiftPurchase, purchase_no=purchase_no, buyer=request.user)))


def paginated(request, queryset, render):
    paginator = PageNumberPagination()
    paginator.page_size = 20
    page = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response([render(row) for row in page])


@endpoint(['GET'])
def inventory(request):
    return paginated(request, GiftInventoryLot.objects.filter(owner=request.user, gift__kind='gift', remaining__gt=0).select_related('gift').order_by('id'),
        lambda row: {'lot_id': row.pk, 'gift_code': row.gift.code, 'name': row.snapshot.get('name', row.gift.name),
            'remaining': row.remaining, 'available': row.remaining-row.reserved if row.status == 'active' and row.gift.send_enabled else 0,
            'status': row.status, 'image_url': gift_image(row.gift, request), 'created_at': row.created_at.isoformat()})


@endpoint(['POST'])
def send(request):
    record = transfer(request.user, **validated(TransferInput, request))
    return Response(transfer_data(record, request))


def transfer_records():
    # Participant names are public display fields, never login names/OpenIDs.
    return GiftTransfer.objects.select_related('gift', 'sender__client_profile', 'recipient', 'purchase').prefetch_related('allocations')


def gift_image(gift, request=None):
    if not gift.image:
        return ''
    return request.build_absolute_uri(gift.image.url) if request else gift.image.url


def transfer_data(row, request=None):
    gift_name = ''
    snapshots = [row.purchase.snapshot] if row.purchase_id else [a.snapshot for a in row.allocations.all()]
    for snapshot in snapshots:
        name = snapshot.get('name') if isinstance(snapshot, dict) else None
        if isinstance(name, str) and name.strip():
            gift_name = name.strip()
            break
    profile = row.sender.client_profile if hasattr(row.sender, 'client_profile') else None
    sender_name = (profile.nickname or '').strip() if profile else ''
    return {'transfer_no': row.transfer_no, 'gift_code': row.gift.code, 'quantity': row.quantity,
            'gift_name': gift_name or row.gift.name, 'image_url': gift_image(row.gift, request),
            'sender_name': sender_name or '未设置昵称的用户',
            'recipient_name': (row.recipient.name or '').strip() or '未设置昵称的陪玩师',
            'status': row.status, 'created_at': row.created_at.isoformat()}


@endpoint(['GET'])
def sent(request):
    return paginated(request, transfer_records().filter(sender=request.user).order_by('-id'), lambda row: transfer_data(row, request))


@endpoint(['GET'])
def received(request):
    return paginated(request, transfer_records().filter(recipient__user=request.user).order_by('-id'), lambda row: transfer_data(row, request))


@endpoint(['GET'])
def earnings(request):
    rows = GiftEarning.objects.filter(player__user=request.user).select_related(
        'allocation', 'transfer__gift', 'transfer__sender__client_profile',
        'transfer__recipient', 'transfer__purchase').prefetch_related('transfer__allocations').order_by('-id')
    def render(row):
        delivery = transfer_data(row.transfer, request)
        # Allocation-specific history must not repeat the transfer's total quantity.
        gift_name = (row.allocation.snapshot.get('name') if row.allocation_id else None) or delivery['gift_name']
        credited = (row.snapshot.get('monetary_accrual') and row.snapshot.get('settlement') == 'immediate'
                    and row.snapshot.get('currency') == 'fish' and row.snapshot.get('wallet_destination') == 'player_wallet')
        return {'id': row.pk, 'status': row.status, 'monetary_accrual': row.snapshot.get('monetary_accrual', False),
            'blockers': row.snapshot.get('blockers', []), 'net_amount': str(row.net_amount), 'available_amount': str(row.available_amount),
            'currency': row.snapshot.get('currency'), 'wallet_destination': row.snapshot.get('wallet_destination'),
            'credited_amount': row.snapshot.get('credited_amount'), 'debt_offset_amount': str(row.debt_offset_amount),
            'gift_name': gift_name, 'image_url': delivery['image_url'], 'sender_name': delivery['sender_name'],
            'recipient_name': delivery['recipient_name'], 'quantity': row.allocation.quantity if row.allocation_id else row.transfer.quantity,
            'earnings_eligible': row.earnings_eligible, 'created_at': row.created_at.isoformat(),
            'credited_at': row.release_at.isoformat() if credited and row.release_at else None}
    return paginated(request, rows, render)
