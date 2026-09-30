"""Patronage's domain adapter for the shared consumption protocol.

Lock order: ClientWallet -> attempt (existing) -> purchase (existing) ->
ordered users/profiles -> player -> global config -> individual config.
Fulfillment subsequently locks PlayerWallet. No network inside these locks.
"""
from decimal import Decimal
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from apps.accounts.models import ClientProfile
from apps.players.models import Player
from apps.wallet import spend_service as spend
from apps.wallet.services import get_or_lock_wallet
from .models import PatronagePurchase, PatronageSettings, PlayerPatronageConfig, purchase_number
from .quotes import quote_data


def lock_configuration(user_id, player_id):
    recipient_id = Player.objects.filter(pk=player_id).values_list('user_id', flat=True).first()
    ids = sorted({i for i in (user_id, recipient_id) if i is not None})
    users = {u.pk: u for u in get_user_model().objects.select_for_update().filter(pk__in=ids).order_by('pk')}
    profiles = {p.user_id: p for p in ClientProfile.objects.select_for_update().filter(user_id__in=ids).order_by('pk')}
    player = get_object_or_404(Player.objects.select_for_update(), pk=player_id)
    if player.user_id != recipient_id:
        spend.fail('PLAYER_UNAVAILABLE')
    if not users[user_id].is_active or user_id not in profiles or profiles[user_id].account_status != 'active':
        spend.fail('ACCOUNT_RESTRICTED')
    config = PatronageSettings.objects.select_for_update().filter(pk=1).first()
    if config is None:
        spend.fail('PURCHASE_NOT_ENABLED')
    individual = PlayerPatronageConfig.objects.select_for_update().filter(player=player).first()
    return users[user_id], player, (config, individual)


def intent_for(player_id, package_code, price_version):
    return dict(player_id=player_id, package_code=package_code, price_version=price_version)


def ensure_dispatch_enabled(attempt):
    if not (getattr(settings, 'PATRONAGE_PURCHASE_ENABLED', False)
            and PatronageSettings.objects.filter(pk=1, purchase_enabled=True, enabled=True).exists()):
        spend.fail('PURCHASE_NOT_ENABLED')


def prepare(user, *, player_id, package_code, price_version, idempotency_key):
    """Freeze one intent, reserve and bind atomically; repeat POST only reads it."""
    profile = ClientProfile.objects.filter(user_id=user.pk).first()
    if profile is None:
        spend.fail('CLIENT_PROFILE_REQUIRED')
    intent = intent_for(player_id, package_code, price_version)
    with transaction.atomic():
        get_or_lock_wallet(profile)
        previous = PatronagePurchase.objects.filter(boss_id=user.pk, idempotency_key=idempotency_key).first()
        if previous:
            if intent_for(previous.player_id, previous.package_code, previous.price_version) != intent:
                spend.fail('IDEMPOTENCY_CONFLICT')
            return previous, False
        user, player, pair = lock_configuration(user.pk, player_id)
        current = quote_data(user, player, package_code, config_pair=pair)
        if current['price_version'] != price_version:
            spend.fail('PRICE_CHANGED')
        if not current['can_submit']:
            spend.fail(current['blockers'][0])
        business_no = purchase_number()
        amount = Decimal(current['amount_yuan'])
        attempt = spend.reserve(profile, kind='patronage', business_no=business_no,
            key=idempotency_key, amount=amount, intent=intent)
        from .pricing import PACKAGE_CHOICES
        record = PatronagePurchase.objects.create(purchase_no=business_no, boss=user,
            player=player, player_name=player.name, package_code=package_code,
            package_name=dict(PACKAGE_CHOICES)[package_code], amount_yuan=amount,
            commission_rate=Decimal(current['commission_rate']),
            platform_amount_yuan=Decimal(current['platform_amount_yuan']),
            player_amount_yuan=Decimal(current['player_amount_yuan']),
            price_version=price_version, config_snapshot={'quote': current, 'policy_version': 'approved-v3'},
            idempotency_key=idempotency_key, attempt=attempt, payment_status='processing',
            bonus_naming_days=7 if package_code == 'day_pass' else 0)
    return record, True


def purchase(user, *, code='', adapter=None, **intent):
    record, created = prepare(user, **intent)
    if created:
        spend.execute(record.attempt_id, adapter=adapter, code=code)
    return PatronagePurchase.objects.select_related('attempt').get(pk=record.pk)


def authorize_dispatch(attempt):
    """Called while shared wallet/attempt locks are already held; fail closed."""
    record = PatronagePurchase.objects.select_for_update().filter(attempt=attempt).first()
    if record is None:
        spend.fail('ORPHAN_BUSINESS_RECORD')
    intent = intent_for(record.player_id, record.package_code, record.price_version)
    expected = spend.digest({'kind': 'patronage', 'business_no': record.purchase_no,
        'amount': str(record.amount_yuan), 'intent': intent})
    if (attempt.kind != 'patronage' or attempt.wallet.profile.user_id != record.boss_id
        or attempt.business_no != record.purchase_no or attempt.idempotency_key != record.idempotency_key
        or attempt.amount != record.amount_yuan or attempt.request_digest != expected
        or record.payment_status not in ('created', 'processing')):
        spend.fail('PAYMENT_IDENTITY_MISMATCH')
    user, player, pair = lock_configuration(record.boss_id, record.player_id)
    current = quote_data(user, player, record.package_code, config_pair=pair, exclude_attempt=attempt.pk)
    if current['price_version'] != record.price_version:
        spend.fail('PRICE_CHANGED')
    if not current['can_submit']:
        spend.fail(current['blockers'][0])
    return {'policy_version': 'approved-v3', 'price_version': record.price_version,
            'boss_id': record.boss_id, 'player_id': record.player_id, 'amount_yuan': str(record.amount_yuan)}
