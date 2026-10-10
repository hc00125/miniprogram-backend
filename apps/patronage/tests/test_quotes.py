from decimal import Decimal
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.patronage.models import PlayerPatronageConfig
from apps.wallet.models import ClientWallet, WalletSpendAttempt


class QuoteTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='quote-owner')
        self.profile = ClientProfile.objects.create(user=self.user, openid='private-owner', nickname='老板')
        self.player = Player.objects.create(name='陪玩', player_type=PlayerType.objects.create(name='类型', priority=1))
        self.individual = PlayerPatronageConfig.objects.create(player=self.player, hourly_rate_yuan=Decimal('50.00'))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def quote(self, code='day_pass'):
        return self.client.post('/api/patronage/quotes/', {'player_id': self.player.pk, 'package_code': code}, format='json')

    def test_invalid_identifiers_are_rejected_without_500(self):
        for value in ('abc', '-1', '1.5', '', '999999999999999999999999999999999999999999'):
            for endpoint in ('catalog', 'crowns'):
                with self.subTest(value=value, endpoint=endpoint):
                    response = self.client.get(f'/api/patronage/{endpoint}/', {'player_id': value})
                    self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get('/api/patronage/crowns/').status_code, 400)

    def test_legacy_authentication_is_readonly_and_never_creates_user(self):
        from datetime import timedelta
        from django.utils import timezone
        self.client.force_authenticate(None)
        self.player.session_token = 'legacy-patronage-test'
        self.player.token_expires_at = timezone.now() + timedelta(days=1)
        self.player.save()
        self.client.credentials(HTTP_AUTHORIZATION='Bearer legacy-patronage-test')
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.quote('day').status_code, 401)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
        self.player.user = self.user
        self.player.save()
        self.assertEqual(self.quote('day').status_code, 200)

    def test_precision_and_restrictions_fail_closed_without_repair_writes(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.patronage.models import PatronageSettings
        # V3 forbids 50.01 hourly rates at storage; keep the precision probe
        # on a configurable naming price that is not a whole official coin.
        global_config = PatronageSettings.objects.create(day_price_yuan=Decimal('188.01'))
        response = self.quote('day')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'PRICE_PRECISION_UNSUPPORTED')
        entry = self.client.get('/api/patronage/catalog/', {'player_id': self.player.pk}).json()['packages'][0]
        self.assertFalse(entry['available'])
        self.assertIsNone(entry['amount_yuan'])
        self.assertEqual(entry['blockers'], ['PRICE_PRECISION_UNSUPPORTED'])
        global_config.day_price_yuan = Decimal('188.00')
        global_config.commission_rate = Decimal('0.2501')
        global_config.save()
        result = self.quote('day').json()
        self.assertEqual(result['commission_rate'], '0.2501')
        self.assertEqual(result['platform_amount_yuan'], '47.0188')
        self.assertEqual(result['player_amount_yuan'], '140.9812')
        self.profile.account_status = 'suspended'
        self.profile.account_suspended_until = timezone.now() - timedelta(days=1)
        self.profile.save()
        with CaptureQueriesContext(connection) as queries:
            result = self.quote('day').json()
        self.assertIn('ACCOUNT_RESTRICTED', result['blockers'])
        self.assertIn('INSUFFICIENT_BALANCE', result['blockers'])
        self.assertFalse(ClientWallet.objects.filter(profile=self.profile).exists())
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        global_config.enabled = False
        global_config.save()
        self.assertEqual(self.quote('day').json()['code'], 'CONFIGURATION_DISABLED')

    def test_restricted_target_account_cannot_be_quoted_or_publicly_exposed(self):
        from apps.accounts.models import ClientProfile
        target_user = get_user_model().objects.create_user(username='target-restricted')
        target_profile = ClientProfile.objects.create(user=target_user, openid='target-private', nickname='受限陪玩')
        self.player.user = target_user
        self.player.save()
        target_profile.account_status = 'banned'
        target_profile.save()
        response = self.quote('day')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'PLAYER_UNAVAILABLE')
        self.assertEqual(self.client.get('/api/patronage/catalog/', {'player_id':self.player.pk}).status_code, 404)
        self.assertEqual(self.client.get('/api/patronage/crowns/', {'player_id':self.player.pk}).status_code, 404)
        target_profile.account_status = 'active'
        target_profile.save()
        target_user.is_active = False
        target_user.save()
        self.assertEqual(self.quote('day').json()['code'], 'PLAYER_UNAVAILABLE')

    def test_checkout_reserved_credit_is_not_available_balance(self):
        from apps.catalog.models import Package
        from apps.orders.models import Order
        from apps.wallet.models import RechargeOrder
        wallet = ClientWallet.objects.create(profile=self.profile, balance=Decimal('500.00'))
        package = Package.objects.create(name='隔离测试商品', player_count=1, base_price=20)
        order = Order.objects.create(order_no='PATRONAGE-RESERVED', boss_user=self.user,
            boss_wechat='isolated-only', package=package, required_players=1,
            total_price_per_hour=20, total_amount=20, status=Order.STATUS_PENDING_PAYMENT)
        RechargeOrder.objects.create(recharge_no='PATRONAGE-RESERVED-RECHARGE', profile=self.profile,
            amount=Decimal('20'), channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_CREDITED, checkout_order_no=order.order_no)
        with CaptureQueriesContext(connection) as queries:
            response = self.quote()
        self.assertEqual(response.json()['available_diamonds'], '4800.0')
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal('500.00'))

    def test_zero_commission_and_version_changes_are_exact(self):
        from apps.patronage.models import PatronageSettings
        before = self.quote().json()['price_version']
        config = PatronageSettings.objects.create(commission_rate=Decimal('0'))
        data = self.quote().json()
        self.assertEqual(data['commission_rate'], '0.00')
        self.assertEqual(data['platform_amount_yuan'], '0.00')
        self.assertEqual(data['player_amount_yuan'], '350.00')
        self.assertNotEqual(data['price_version'], before)
        self.individual.hourly_rate_yuan = Decimal('100')
        self.individual.save()
        after = self.quote().json()
        self.assertEqual(after['amount_yuan'], '700.00')
        self.assertNotEqual(after['price_version'], data['price_version'])

    def test_all_naming_prices_and_approved_annual_duration(self):
        expected = {'day':'188.00','week':'520.00','month':'1314.00','quarter':'2888.00','year':'9999.00'}
        for code, amount in expected.items():
            with self.subTest(code=code):
                data = self.quote(code).json()
                self.assertEqual(data['amount_yuan'], amount)
                self.assertEqual(Decimal(data['platform_amount_yuan']) + Decimal(data['player_amount_yuan']), Decimal(amount))
                self.assertFalse(data['can_submit'])
        self.assertNotIn('YEAR_DURATION_UNCONFIRMED', self.quote('year').json()['blockers'])

    def test_missing_disabled_hourly_and_nonpublic_player_are_blocked(self):
        self.individual.hourly_rate_yuan = None
        self.individual.save()
        self.assertEqual(self.quote().json()['code'], 'HOURLY_RATE_UNSET')
        self.individual.hourly_rate_yuan = Decimal('50')
        self.individual.enabled = False
        self.individual.save()
        self.assertEqual(self.quote().json()['code'], 'CONFIGURATION_DISABLED')
        for status in ('pending', 'rejected', 'disabled'):
            self.player.status = status
            self.player.save()
            self.assertEqual(self.quote('day').json()['code'], 'PLAYER_UNAVAILABLE')
            self.assertEqual(self.client.get('/api/patronage/catalog/', {'player_id':self.player.pk}).status_code, 404)

    def test_authentication_and_methods_cannot_create_transactions(self):
        from rest_framework_simplejwt.tokens import AccessToken
        self.client.force_authenticate(None)
        self.assertEqual(self.quote('day').status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(self.user)}')
        self.assertEqual(self.quote('day').status_code, 200)
        self.assertEqual(self.client.get('/api/patronage/records/').json()['count'], 0)
        for endpoint in ('catalog', 'records', 'crowns'):
            self.assertEqual(self.client.post(f'/api/patronage/{endpoint}/', {}, format='json').status_code, 405)
        self.assertEqual(self.client.get('/api/patronage/quotes/').status_code, 405)
        self.assertEqual(self.client.post('/api/patronage/purchases/', {}, format='json').status_code, 400)
        self.assertEqual(self.client.get('/api/patronage/purchases/by-key/', {'idempotency_key':'unknown'}).status_code, 404)
        self.assertEqual(self.quote('bad-code').status_code, 400)
        for value in (None, 0, -1, True, '1.2', 'NaN', 'Infinity', '99999999999999999999999'):
            self.assertEqual(self.client.post('/api/patronage/quotes/', {'player_id':value, 'package_code':'day'}, format='json').status_code, 400)

    def test_quote_returns_exact_split_reservation_adjusted_balance_without_writes(self):
        wallet = ClientWallet.objects.create(profile=self.profile, balance=Decimal('500.00'))
        WalletSpendAttempt.objects.create(wallet=wallet, kind='gift', business_no='isolated-only',
            idempotency_key='isolated-key', request_digest='a'*64, amount=Decimal('100.00'), reserved_amount=Decimal('100.00'), status='unknown')
        with CaptureQueriesContext(connection) as queries:
            response = self.quote()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['amount_yuan'], '350.00')
        self.assertEqual(data['amount_diamonds'], '3500.0')
        self.assertEqual(data['platform_amount_yuan'], '87.50')
        self.assertEqual(data['player_amount_yuan'], '262.50')
        self.assertEqual(data['commission_rate'], '0.25')
        self.assertEqual(data['available_diamonds'], '4000.0')
        self.assertFalse(data['can_submit'])
        self.assertIn('PURCHASE_NOT_ENABLED', data['blockers'])
        self.assertRegex(data['price_version'], r'^[0-9a-f]{64}$')
        self.assertEqual(data['price_version'], self.quote().json()['price_version'])
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
