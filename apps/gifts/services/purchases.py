"""Diamond purchases with atomic inventory delivery or immediate fish income."""
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from apps.accounts.models import ClientProfile
from apps.gifts.models import Gift, GiftPurchase, GiftInventoryLot, GiftInventoryLedger
from apps.wallet.services import get_or_lock_wallet
from apps.wallet import spend_service as spend


def policy():
    p = getattr(settings, 'GIFT_TRANSACTION_POLICY', {})
    if not p.get('version') or p.get('platform_approved') is not True:
        spend.fail('POLICY_UNCONFIRMED')
    if any(type(p.get(k)) is not int or p[k] <= 0 for k in ('max_quantity', 'max_diamonds')):
        spend.fail('POLICY_UNCONFIRMED')
    if 'daily_diamonds' not in p or (p['daily_diamonds'] is not None and
            (type(p['daily_diamonds']) is not int or p['daily_diamonds'] <= 0)):
        spend.fail('POLICY_UNCONFIRMED')
    if not isinstance(p.get('recipient_ids'), list) and not (
            p.get('recipient_ids') is None and p.get('recipient_scope') == 'approved'):
        spend.fail('POLICY_UNCONFIRMED')
    return p


def lock_authorization(user, gift_code, recipient_id=None):
    """After wallet/attempt: users by PK, profiles by PK, player, gift, config.

    Locks last through the prepared->dispatching commit (authorization point).
    Later revocations cannot alter this authorization snapshot. Recipient bans
    may still block local direct delivery; that compensation remains unresolved.
    """
    from django.contrib.auth import get_user_model
    from apps.players.models import Player
    from apps.gifts.models import PlayerGiftConfig
    recipient_user_id = Player.objects.filter(pk=recipient_id).values_list('user_id', flat=True).first()
    ids = sorted({pk for pk in (user.pk, recipient_user_id) if pk is not None})
    users = list(get_user_model().objects.select_for_update().filter(pk__in=ids).order_by('pk'))
    list(ClientProfile.objects.select_for_update().filter(user_id__in=ids).order_by('pk'))
    player = Player.objects.select_for_update().filter(pk=recipient_id).first()
    if player and player.user_id != recipient_user_id:
        spend.fail('RECIPIENT_INELIGIBLE')
    gift = Gift.objects.select_for_update().filter(code=gift_code).first()
    if gift and player:
        list(PlayerGiftConfig.objects.select_for_update().filter(player=player, gift=gift))
    return next(u for u in users if u.pk == user.pk), gift


def profile_for(user):
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.get(pk=user.pk)
    if not user.is_active:
        spend.fail('ACCOUNT_RESTRICTED')
    profile = ClientProfile.objects.filter(user=user).first()
    if not profile or profile.account_status != ClientProfile.ACCOUNT_STATUS_ACTIVE:
        spend.fail('ACCOUNT_RESTRICTED')
    return profile


def recipient_for(user, recipient_id, p):
    from apps.players.models import Player
    player = Player.objects.filter(pk=recipient_id, status='approved', is_publicly_visible=True,
                                   user__is_active=True).select_related('user').first()
    allowed = p.get('recipient_ids')
    if not player or player.user_id == user.pk or (allowed is not None and player.pk not in allowed):
        spend.fail('RECIPIENT_INELIGIBLE')
    profile_for(player.user)
    return player


def price_version(gift, p):
    return spend.digest({'gift_id': gift.pk, 'price': gift.price_diamonds,
                         'updated': gift.updated_at.isoformat(), 'policy': p['version']})


def quote(user, *, gift_code, quantity, mode, recipient_id=None):
    p = policy()
    profile = profile_for(user)
    if type(quantity) is not int or not 1 <= quantity <= p['max_quantity'] or mode not in ('inventory', 'direct'):
        spend.fail('INVALID_REQUEST')
    gift = Gift.objects.filter(code=gift_code, kind='gift', is_active=True).first()
    if not gift or quantity * gift.price_diamonds > p['max_diamonds']:
        spend.fail('GIFT_UNAVAILABLE')
    commission = None
    if mode == 'direct':
        player = recipient_for(user, recipient_id, p)
        if not gift.send_enabled:
            spend.fail('GIFT_SEND_DISABLED')
        from .configuration import resolve_commission
        commission = resolve_commission(player.pk, gift.pk)
    elif recipient_id is not None:
        spend.fail('INVALID_REQUEST')
    from apps.wallet.coin_balance_service import _reserved_checkout_amount
    wallet = profile.wallet
    available = max(Decimal('0'), wallet.balance - spend.reserved(wallet) - _reserved_checkout_amount(profile))
    return {'gift_code': gift_code, 'quantity': quantity, 'mode': mode, 'recipient_id': recipient_id,
        'price_version': price_version(gift, p), 'total_diamonds': quantity * gift.price_diamonds,
        'available_diamonds': str(available * 10), 'commission_version': commission['version'] if commission else None,
        'commission_snapshot': commission, 'policy_version': p['version'],
        'can_submit': bool(getattr(settings, 'GIFT_PURCHASE_ENABLED', False)),
        'blockers': [] if getattr(settings, 'GIFT_PURCHASE_ENABLED', False) else ['FEATURE_DISABLED']}


def purchase(user, *, gift_code, quantity, mode, price_version, idempotency_key,
             recipient_id=None, commission_version=None, code='', adapter=None):
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 100:
        spend.fail('INVALID_REQUEST')
    intent = dict(gift_code=gift_code, quantity=quantity, mode=mode, recipient_id=recipient_id,
                  price_version=price_version, commission_version=commission_version)
    fingerprint = spend.digest(intent)
    profile = profile_for(user)
    with transaction.atomic():
        get_or_lock_wallet(profile)
        previous = GiftPurchase.objects.filter(buyer=user, idempotency_key=idempotency_key).first()
        if previous:
            if previous.request_digest != fingerprint:
                spend.fail('IDEMPOTENCY_CONFLICT')
            record = previous
        else:
            user, gift = lock_authorization(user, gift_code, recipient_id)
            if not getattr(settings, 'GIFT_PURCHASE_ENABLED', False):
                spend.fail('FEATURE_DISABLED')
            current = quote(user, gift_code=gift_code, quantity=quantity, mode=mode, recipient_id=recipient_id)
            if current['price_version'] != price_version:
                spend.fail('PRICE_CHANGED')
            if current['commission_version'] != commission_version:
                spend.fail('CONFIG_CHANGED')
            p = policy()
            day = timezone.now().date()
            total = sum(r.snapshot['total_diamonds'] for r in GiftPurchase.objects.filter(
                buyer=user, created_at__date=day).exclude(status__in=('failed', 'cancelled')))
            if p['daily_diamonds'] is not None and total + current['total_diamonds'] > p['daily_diamonds']:
                spend.fail('DAILY_LIMIT')
            gift.refresh_from_db()
            if (not gift.is_active or gift.kind != 'gift' or
                globals()['price_version'](gift, p) != current['price_version'] or
                gift.price_diamonds * quantity != current['total_diamonds']):
                spend.fail('PRICE_CHANGED')
            business_no = 'GP' + uuid.uuid4().hex
            # Zero-price catalogue gifts have no payment attempt or wallet entry.
            # Keep the positive-amount invariant of the shared spend engine intact.
            attempt = None
            if current['total_diamonds'] > 0:
                attempt = spend.reserve(profile, kind='gift', business_no=business_no, key=idempotency_key,
                    amount=Decimal(current['total_diamonds']) / 10, intent=intent)
            snapshot = {'name': gift.name, 'image': gift.image.name, 'unit_diamonds': gift.price_diamonds,
                'total_diamonds': current['total_diamonds'], 'price_version': price_version,
                'policy_version': p['version'], 'policy_digest': spend.digest(p),
                'source': attempt.source_snapshot if attempt else {'kind': 'free'},
                'payment_kind': 'diamonds' if attempt else 'free'}
            if mode == 'direct':
                snapshot['commission'] = current['commission_snapshot']
            record = GiftPurchase.objects.create(purchase_no=business_no, buyer=user, gift=gift,
                mode=mode, recipient_id=recipient_id, quantity=quantity, snapshot=snapshot,
                idempotency_key=idempotency_key, request_digest=fingerprint, attempt=attempt, status='processing')
            if attempt is None:
                _deliver_record(record)
        if record.attempt_id is None:
            if record.status != 'paid' or record.snapshot.get('payment_kind') != 'free' or record.snapshot.get('total_diamonds') != 0:
                spend.fail('PURCHASE_REQUIRES_REVIEW')
            return record
    result = spend.execute(record.attempt_id, adapter=adapter, code=code, apply=fulfill)
    if result.status == 'unknown':
        GiftPurchase.objects.filter(pk=record.pk).update(status='unknown')
    record.refresh_from_db()
    return record


def authorize_dispatch(attempt):
    record = GiftPurchase.objects.filter(attempt=attempt).first()
    if record is None:
        return  # Generic engine tests/consumers have no gift business record.
    user, _ = lock_authorization(record.buyer, record.gift.code, record.recipient_id)
    if not getattr(settings, 'GIFT_PURCHASE_ENABLED', False):
        spend.fail('FEATURE_DISABLED')
    p = policy()
    profile_for(user)
    if p['version'] != record.snapshot['policy_version'] or spend.digest(p) != record.snapshot.get('policy_digest'):
        spend.fail('POLICY_UNCONFIRMED')
    current = quote(record.buyer, gift_code=record.gift.code, quantity=record.quantity,
                    mode=record.mode, recipient_id=record.recipient_id)
    if current['price_version'] != record.snapshot['price_version']:
        spend.fail('PRICE_CHANGED')
    if record.mode == 'direct' and current['commission_snapshot'] != record.snapshot['commission']:
        spend.fail('CONFIG_CHANGED')
    buyer_profile = profile_for(user)
    recipient = None
    if record.mode == 'direct':
        player = recipient_for(user, record.recipient_id, p)
        recipient_profile = profile_for(player.user)
        recipient = {'player_id': player.pk, 'user_id': player.user_id, 'status': player.status,
            'is_publicly_visible': player.is_publicly_visible, 'is_active': player.user.is_active,
            'account_status': recipient_profile.account_status}
    return {'schema_version': 1, 'policy_digest': spend.digest(p),
        'price_version': current['price_version'], 'total_diamonds': current['total_diamonds'],
        'commission': current['commission_snapshot'],
        'buyer': {'user_id': user.pk, 'is_active': user.is_active, 'account_status': buyer_profile.account_status},
        'recipient': recipient}


def fulfill(attempt):
    record = GiftPurchase.objects.select_for_update().get(attempt=attempt)
    _deliver_record(record)


def _deliver_record(record):
    """Called inside the confirmed-payment or authenticated free transaction."""
    if record.status == 'paid':
        return
    if record.mode == 'direct':
        from .transfers import deliver_direct
        deliver_direct(record)
    else:
        lot = GiftInventoryLot.objects.create(owner=record.buyer, gift=record.gift, source='paid',
            source_reference=record.purchase_no, purchase=record, quantity=record.quantity,
            remaining=record.quantity, cost_diamonds=record.snapshot['total_diamonds'],
            earnings_eligible=record.snapshot['total_diamonds'] > 0, snapshot=record.snapshot)
        GiftInventoryLedger.objects.create(lot=lot, event_key='purchase:' + record.purchase_no,
            kind='purchase', quantity_delta=record.quantity, actor=record.buyer,
            reason='免费领取入库（不计收益）' if record.snapshot['total_diamonds'] == 0 else '已确认购买入库')
    record.status = 'paid'
    record.save(update_fields=['status', 'updated_at'])
