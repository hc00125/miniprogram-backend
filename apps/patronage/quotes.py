"""Read-only estimates. No order, wallet, grant or earnings writes."""
import hashlib
import json
from decimal import Decimal
from django.conf import settings
from apps.accounts.models import ClientProfile
from apps.wallet.models import ClientWallet
from apps.wallet.spend_service import reserved
from apps.wallet.coin_balance_service import _reserved_checkout_amount
from .pricing import catalog_data, configuration, decimal_text
from rest_framework.exceptions import ValidationError


def quote_data(user, player, code, *, config_pair=None, exclude_attempt=None):
    pair = config_pair if config_pair is not None else configuration(player)
    catalog = catalog_data(player, config_pair=pair)
    package = next(p for p in catalog['packages'] if p['code'] == code)
    if not package['available']:
        raise ValidationError({'code': package['blockers'][0], 'detail': package['blockers'][0],
                               'blockers': package['blockers']})
    amount = Decimal(package['amount_yuan'])
    rate = Decimal(catalog['commission_rate'])
    platform = amount * rate
    profile = ClientProfile.objects.filter(user=user).first()
    wallet = ClientWallet.objects.filter(profile=profile).first() if profile else None
    active_reserved = reserved(wallet, exclude=exclude_attempt) if wallet else Decimal('0')
    checkout_reserved = _reserved_checkout_amount(profile) if profile else Decimal('0')
    available = max(Decimal('0'), wallet.balance - active_reserved - checkout_reserved) if wallet else Decimal('0')
    blockers = [] if catalog['purchase_enabled'] else ['PURCHASE_NOT_ENABLED']
    if not user.is_active or (profile and profile.account_status != ClientProfile.ACCOUNT_STATUS_ACTIVE):
        # Do not use account_restriction_payload: it repairs expired restrictions.
        blockers.append('ACCOUNT_RESTRICTED')
    if profile is None:
        blockers.append('CLIENT_PROFILE_REQUIRED')
    if available < amount:
        blockers.append('INSUFFICIENT_BALANCE')
    if active_reserved:
        blockers.append('PAYMENT_PENDING')
    if checkout_reserved:
        blockers.append('CHECKOUT_RECOVERY_PENDING')
    if profile:
        from apps.wallet.coin_sync import coin_backed_wallet_buckets
        if any(value > 0 for value in coin_backed_wallet_buckets(profile).values()):
            if not getattr(settings, 'WECHAT_VIRTUALPAY_ENABLED', False):
                blockers.append('PLATFORM_DISABLED')
            if not getattr(settings, 'SHARED_SPEND_PLATFORM_APPROVED', False):
                blockers.append('POLICY_UNCONFIRMED')
    config, individual = pair
    snapshot = {'contract_version': '1.0', 'player_id': player.pk, 'package': package,
                'commission_rate': catalog['commission_rate'],
                'hourly_rate_yuan': catalog['player']['hourly_rate_yuan'],
                'config_updated_at': config.updated_at.isoformat() if config.updated_at else None,
                'player_config_updated_at': individual.updated_at.isoformat() if individual else None}
    version = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return dict(contract_version='1.0', player_id=player.pk, package_code=code,
                amount_yuan=package['amount_yuan'], amount_diamonds=package['amount_diamonds'],
                commission_rate=catalog['commission_rate'],
                platform_amount_yuan=decimal_text(platform),
                player_amount_yuan=decimal_text(amount-platform),
                price_version=version, available_diamonds=f'{available*10:.1f}',
                can_submit=not blockers, blockers=blockers)
