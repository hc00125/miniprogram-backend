"""HTTP through real shared spend, PG locks, grants and earnings (offline)."""
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient
from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.wallet.models import ClientWallet, WalletSpendAttempt, ClientWalletLedger
from apps.patronage.models import PatronageSettings, PatronagePurchase, CrownGrant, PatronageEarning


class FakeWeChat:
    """Only external WeChat is fake; never replace shared spend or ORM."""
    def __init__(self, timeout=False):
        self.timeout, self.proven = timeout, False
        self.spends = self.queries = self.authentications = 0

    def authenticate(self, attempt, code):
        from django.db import connection
        assert not connection.in_atomic_block
        self.authentications += 1
        return 'ephemeral-session'

    def spend(self, attempt, session):
        from django.db import connection
        assert not connection.in_atomic_block
        self.spends += 1
        if self.timeout:
            raise TimeoutError('offline unknown')
        return {'errcode': 0, 'order_id': attempt.external_id}

    def query(self, attempt):
        from django.db import connection
        assert not connection.in_atomic_block
        self.queries += 1
        if self.proven:
            return {'order_id': attempt.external_id, 'amount': attempt.source_snapshot['coin_units'],
                'openid': attempt.request_payload['openid'], 'env': attempt.request_payload['env'], 'status': 'succeeded'}
        return None


@override_settings(PATRONAGE_PURCHASE_ENABLED=True, FISH_CRACKER_EXCHANGE_RATE=10, WECHAT_VIRTUALPAY_ENV=1,
                   WECHAT_VIRTUALPAY_ENABLED=True, SHARED_SPEND_PLATFORM_APPROVED=True)
class PatronageHTTPTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('http-buyer')
        self.profile = ClientProfile.objects.create(user=self.user, openid='offline-http', nickname='http-buyer')
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile, defaults={'balance': Decimal('20000')})
        self.player = Player.objects.create(name='HTTP recipient', status='approved', is_publicly_visible=True,
            player_type=PlayerType.objects.create(name='http-type', priority=1))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def quote(self, package='day'):
        response = self.client.post('/api/patronage/quotes/', {'player_id': self.player.pk, 'package_code': package}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def payload(self, key='http-key', package='day'):
        return dict(player_id=self.player.pk, package_code=package,
            price_version=self.quote(package)['price_version'], idempotency_key=key)

    def coin(self, amount='188.00'):
        from apps.wallet.models import RechargeOrder
        RechargeOrder.objects.create(recharge_no='HTTP-COIN', profile=self.profile, amount=Decimal(amount),
            status='credited', notify_payload={'mode': 'short_series_coin', 'wechat_coin_units_per_yuan': 10})
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='recharge', amount=Decimal(amount),
            balance_after=self.wallet.balance, reference_id='HTTP-COIN')

    def test_unknown_http_pending_read_only_then_recovery_never_repays(self):
        from unittest.mock import patch
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from apps.wallet import spend_service
        PatronageSettings.objects.create(purchase_enabled=True)
        self.coin('100.00')
        payload = self.payload()
        adapter = FakeWeChat(timeout=True)
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter):
            result = self.client.post('/api/patronage/purchases/', payload, format='json')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.data['payment_status'], 'unknown')
            self.assertIsNone(result.data['paid_at'])
            attempt = WalletSpendAttempt.objects.get()
            self.assertEqual(attempt.source_snapshot['local_amount'], '88.00')
            with CaptureQueriesContext(connection) as queries:
                readback = self.client.get('/api/patronage/purchases/by-key/', {'idempotency_key': payload['idempotency_key']})
                pending = self.client.get('/api/patronage/purchases/pending/', {'page_size': 1})
                records = self.client.get('/api/patronage/records/')
            self.assertEqual(pending.status_code, 200)
            self.assertEqual(pending.data['count'], 1)
            self.assertEqual(pending.data['results'][0], readback.data)
            self.assertEqual(records.data['results'][0], readback.data)
            self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
            for _ in range(2):
                self.client.post('/api/patronage/purchases/', payload, format='json')
                spend_service.recover(attempt.pk)
            self.assertEqual((adapter.spends, adapter.queries), (1, 2))
            attempt.refresh_from_db()
            self.assertEqual(attempt.reserved_amount, Decimal('188'))
            adapter.proven = True
            spend_service.recover(attempt.pk)
            spend_service.recover(attempt.pk)
            final = self.client.get('/api/patronage/purchases/by-key/', {'idempotency_key': payload['idempotency_key']})
            self.assertEqual(final.data['payment_status'], 'paid')
            self.assertEqual(self.client.get('/api/patronage/purchases/pending/').data['count'], 0)
            self.assertEqual(adapter.spends, 1)
        self.assertEqual(PatronageEarning.objects.count(), 1)
        self.assertEqual(CrownGrant.objects.count(), 1)
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)
        self.assertNotIn('temporary-code', str(PatronagePurchase.objects.get().config_snapshot))

    def test_recovery_command_cancels_prepared_domain_without_debit(self):
        from io import StringIO
        from django.core.management import call_command
        from apps.patronage.purchases import prepare
        PatronageSettings.objects.create(purchase_enabled=True)
        record, _ = prepare(self.user, **self.payload())
        output, errors = StringIO(), StringIO()
        call_command('reconcile_wallet_spends', attempt_id=record.attempt_id, stdout=output, stderr=errors)
        record.refresh_from_db()
        self.assertEqual(record.payment_status, 'processing')
        call_command('reconcile_wallet_spends', apply=True, attempt_id=record.attempt_id, stdout=output, stderr=errors)
        record.refresh_from_db()
        self.assertEqual(record.payment_status, 'failed', errors.getvalue())
        self.assertEqual(record.attempt.status, 'failed')
        self.assertEqual(record.attempt.reserved_amount, 0)
        self.assertFalse(ClientWalletLedger.objects.filter(entry_type='shared_spend').exists())
        self.assertEqual(self.client.get('/api/patronage/purchases/pending/').data['count'], 0)

    def test_fulfillment_failure_returns_retryable_error_and_recovers_original_spend(self):
        from unittest.mock import patch
        from io import StringIO
        from django.core.management import call_command
        from apps.wallet import spend_service
        PatronageSettings.objects.create(purchase_enabled=True)
        self.coin()
        payload = self.payload()
        adapter = FakeWeChat()
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter):
            with patch('apps.patronage.fulfillment.credit_purchase', side_effect=RuntimeError('synthetic income failure')):
                self.client.raise_request_exception = False
                response = self.client.post('/api/patronage/purchases/', payload, format='json')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.data['code'], 'PURCHASE_REQUIRES_RECOVERY')
            attempt = WalletSpendAttempt.objects.get()
            self.assertEqual(attempt.status, 'succeeded')
            self.assertEqual(attempt.reserved_amount, Decimal('188'))
            self.assertFalse(CrownGrant.objects.exists())
            self.assertFalse(PatronageEarning.objects.exists())
            self.assertFalse(ClientWalletLedger.objects.filter(entry_type='shared_spend').exists())
            self.wallet.refresh_from_db()
            self.assertEqual(self.wallet.balance, Decimal('20000'))
            self.assertEqual(self.client.get('/api/patronage/purchases/by-key/', {'idempotency_key': payload['idempotency_key']}).data['payment_status'], 'processing')
            PatronageSettings.objects.filter(pk=1).update(day_price_yuan=999, purchase_enabled=False)
            with override_settings(PATRONAGE_PURCHASE_ENABLED=False):
                call_command('reconcile_wallet_spends', apply=True, attempt_id=attempt.pk, stdout=StringIO())
                spend_service.recover(attempt.pk)
            self.assertEqual((adapter.spends, adapter.authentications, adapter.queries), (1, 1, 0))
        self.assertEqual(PatronageEarning.objects.get().amount_yuan, Decimal('188'))
        self.assertEqual(CrownGrant.objects.count(), 1)
        self.assertEqual(self.client.get('/api/patronage/purchases/by-key/', {'idempotency_key': payload['idempotency_key']}).data['payment_status'], 'paid')

    def test_closed_gate_prevents_even_wechat_authentication_of_prepared(self):
        from apps.patronage.purchases import prepare
        from apps.wallet import spend_service
        from rest_framework.exceptions import ValidationError
        PatronageSettings.objects.create(purchase_enabled=True)
        self.coin()
        record, _ = prepare(self.user, **self.payload())
        adapter = FakeWeChat()
        with override_settings(PATRONAGE_PURCHASE_ENABLED=False):
            with self.assertRaises(ValidationError):
                spend_service.execute(record.attempt_id, adapter=adapter)
        self.assertEqual(adapter.authentications, 0)
        record.refresh_from_db()
        self.assertEqual(record.payment_status, 'failed')

    def test_real_wechat_adapter_requires_platform_enabled_for_patronage(self):
        from unittest.mock import patch
        from apps.patronage.purchases import prepare
        from apps.wallet.spend_adapter import WeChatSpendAdapter
        from rest_framework.exceptions import ValidationError
        PatronageSettings.objects.create(purchase_enabled=True)
        self.coin()
        record, _ = prepare(self.user, **self.payload())
        with override_settings(WECHAT_VIRTUALPAY_ENABLED=False, SHARED_SPEND_PLATFORM_APPROVED=True):
            with patch('apps.wallet.spend_adapter._request_session', return_value='fake-session') as session:
                with self.assertRaises(ValidationError):
                    WeChatSpendAdapter().authenticate(record.attempt, 'temporary-code')
                session.assert_not_called()

    def test_default_off_post_and_catalog_do_not_open_transactions(self):
        config = PatronageSettings.objects.create()
        payload = self.payload()
        for runtime, business in ((False, False), (False, True), (True, False)):
            config.purchase_enabled = business
            config.save()
            payload['price_version'] = self.quote()['price_version']
            with override_settings(PATRONAGE_PURCHASE_ENABLED=runtime):
                self.assertFalse(self.quote()['can_submit'])
                response = self.client.post('/api/patronage/purchases/', payload, format='json')
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data['code'], 'PURCHASE_NOT_ENABLED')
                self.assertFalse(self.client.get('/api/patronage/catalog/', {'player_id': self.player.pk}).data['purchase_enabled'])
        self.assertFalse(PatronagePurchase.objects.exists())
        self.assertFalse(WalletSpendAttempt.objects.exists())

    def test_key_intent_conflicts_and_changed_price_cannot_charge(self):
        PatronageSettings.objects.create(purchase_enabled=True)
        payload = self.payload()
        self.assertEqual(self.client.post('/api/patronage/purchases/', payload, format='json').status_code, 200)
        for change in ({'package_code': 'week'}, {'price_version': '0'*64}, {'player_id': self.player.pk+1000}):
            result = self.client.post('/api/patronage/purchases/', {**payload, **change}, format='json')
            self.assertEqual(result.status_code, 400)
            self.assertEqual(result.data['code'], 'IDEMPOTENCY_CONFLICT')
        stale = self.payload(key='stale')
        PatronageSettings.objects.filter(pk=1).update(day_price_yuan=200)
        result = self.client.post('/api/patronage/purchases/', stale, format='json')
        self.assertEqual(result.data['code'], 'PRICE_CHANGED')
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)

    def test_owner_only_jwt_reads_have_no_writes_or_cross_account_leaks(self):
        from rest_framework_simplejwt.tokens import AccessToken
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        PatronageSettings.objects.create(purchase_enabled=True)
        payload = self.payload()
        self.client.post('/api/patronage/purchases/', payload, format='json')
        other = get_user_model().objects.create_user('http-other')
        client = APIClient()
        self.assertEqual(client.post('/api/patronage/purchases/', payload, format='json').status_code, 401)
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(other)}')
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(client.get('/api/patronage/purchases/by-key/', {'idempotency_key': payload['idempotency_key']}).status_code, 404)
            self.assertEqual(client.get('/api/patronage/purchases/pending/').data['count'], 0)
            self.assertEqual(client.get('/api/patronage/records/').data['count'], 0)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(self.user)}')
        self.assertEqual(client.get('/api/patronage/purchases/by-key/', {'idempotency_key': payload['idempotency_key']}).data['payment_status'], 'paid')
        self.assertFalse(ClientProfile.objects.filter(user=other).exists())

    def test_pending_paginates_legacy_created_and_abnormal_active_records(self):
        from apps.patronage.purchases import prepare
        PatronageSettings.objects.create(purchase_enabled=True)
        record, _ = prepare(self.user, **self.payload())
        # Explicit corruption fixture: domain says failed but active evidence wins.
        PatronagePurchase.objects.filter(pk=record.pk).update(payment_status='failed')
        fields = {field: getattr(record, field) for field in record.IMMUTABLE
                  if field not in ('purchase_no', 'idempotency_key')}
        for i in range(3):
            PatronagePurchase.objects.create(**fields, idempotency_key=f'legacy-created-{i}')
        keys, url = [], '/api/patronage/purchases/pending/?page_size=2'
        while url:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data['count'], 4)
            keys.extend(row['idempotency_key'] for row in response.data['results'])
            url = response.data['next']
        self.assertEqual(len(set(keys)), 4)
        row = self.client.get('/api/patronage/purchases/by-key/', {'idempotency_key':record.idempotency_key}).data
        self.assertEqual(row['payment_status'], 'processing')

    def test_day_pass_bonus_renewal_and_multiple_bosses_through_http(self):
        from datetime import timedelta
        from apps.patronage.models import PlayerPatronageConfig
        PatronageSettings.objects.create(purchase_enabled=True)
        PlayerPatronageConfig.objects.create(player=self.player, hourly_rate_yuan=Decimal('50.5'))
        for key, package in (('first','day'), ('bonus','day_pass'), ('annual','year')):
            response = self.client.post('/api/patronage/purchases/', self.payload(key, package), format='json')
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data['payment_status'], 'paid')
        grants = list(CrownGrant.objects.order_by('created_at'))
        self.assertEqual(grants[1].source, 'day_pass_bonus')
        self.assertEqual(grants[1].expires_at-grants[0].expires_at, timedelta(days=7))
        self.assertEqual(grants[2].expires_at-grants[1].expires_at, timedelta(days=365))
        self.assertEqual(PatronageEarning.objects.count(), 3)
        self.assertEqual(PatronageEarning.objects.get(purchase__idempotency_key='bonus').net_amount, Decimal('2575.50'))
        other = get_user_model().objects.create_user('second-http-boss')
        profile = ClientProfile.objects.create(user=other, openid='second-boss', nickname='second-boss')
        ClientWallet.objects.update_or_create(profile=profile, defaults={'balance':Decimal('500')})
        self.client.force_authenticate(other)
        response = self.client.post('/api/patronage/purchases/', self.payload('other'), format='json')
        self.assertEqual(response.data['payment_status'], 'paid')
        crowns = self.client.get('/api/patronage/crowns/', {'player_id':self.player.pk}).data['results']
        self.assertEqual(len(crowns), 2)
        self.assertTrue(all('idempotency_key' not in row and 'price_version' not in row for row in crowns))

    def test_dispatch_rechecks_price_after_external_auth(self):
        from apps.patronage.purchases import prepare
        from apps.wallet import spend_service
        from rest_framework.exceptions import ValidationError
        PatronageSettings.objects.create(purchase_enabled=True)
        self.coin()
        record, _ = prepare(self.user, **self.payload())
        class ChangePrice(FakeWeChat):
            def authenticate(fake, attempt, code):
                session = super().authenticate(attempt, code)
                PatronageSettings.objects.filter(pk=1).update(day_price_yuan=200)
                return session
        adapter = ChangePrice()
        with self.assertRaises(ValidationError) as error:
            spend_service.execute(record.attempt_id, adapter=adapter)
        self.assertEqual(error.exception.detail['code'], 'PRICE_CHANGED')
        self.assertEqual(adapter.spends, 0)
        record.refresh_from_db()
        self.assertEqual(record.payment_status, 'failed')
        self.assertEqual(record.attempt.reserved_amount, 0)

    def test_orphan_patronage_cannot_dispatch_or_finalize_without_callback(self):
        from apps.wallet import spend_service
        from rest_framework.exceptions import ValidationError
        PatronageSettings.objects.create(purchase_enabled=True)
        attempt = spend_service.reserve(self.profile, kind='patronage', business_no='ORPHAN', key='orphan', amount=Decimal('188'), intent={})
        with self.assertRaises(ValidationError) as error:
            spend_service.execute(attempt.pk)
        self.assertEqual(error.exception.detail['code'], 'ORPHAN_BUSINESS_RECORD')
        # Separate synthetic external-success orphan verifies automatic callback is fail closed.
        second = spend_service.reserve(self.profile, kind='patronage', business_no='ORPHAN2', key='orphan2', amount=Decimal('188'), intent={})
        second.status = 'succeeded'
        second.save(update_fields=['status'])
        with self.assertRaises(ValidationError):
            spend_service.finalize(second.pk)
        second.refresh_from_db()
        self.assertEqual(second.status, 'succeeded')
        self.assertEqual(second.reserved_amount, Decimal('188'))
        self.assertFalse(ClientWalletLedger.objects.filter(entry_type='shared_spend').exists())

    def test_unknown_blocks_other_kinds_and_never_accepts_wrong_evidence(self):
        from unittest.mock import patch
        from apps.wallet import spend_service
        from rest_framework.exceptions import ValidationError
        PatronageSettings.objects.create(purchase_enabled=True)
        self.coin()
        adapter = FakeWeChat(timeout=True)
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter):
            response = self.client.post('/api/patronage/purchases/', self.payload(), format='json')
        self.assertEqual(response.data['payment_status'], 'unknown')
        attempt = WalletSpendAttempt.objects.get()
        for kind in ('gift', 'order', 'surcharge', 'patronage'):
            with self.assertRaises(ValidationError):
                spend_service.reserve(self.profile, kind=kind, business_no=kind, key=kind, amount=Decimal('1'), intent={})
        valid = {'order_id':attempt.external_id,'amount':1880,'openid':self.profile.openid,'env':1,'status':'succeeded'}
        class Query:
            def __init__(self, evidence): self.evidence=evidence
            def query(self, attempt): return self.evidence
        for field, wrong in (('order_id','wrong'),('amount',1881),('openid','wrong'),('env',0),('status','failed')):
            result = spend_service.recover(attempt.pk, adapter=Query({**valid,field:wrong}))
            self.assertEqual(result.status, 'unknown')
            self.assertEqual(result.reserved_amount, Decimal('188'))
        self.assertFalse(CrownGrant.objects.exists())
        self.assertFalse(ClientWalletLedger.objects.filter(entry_type='shared_spend').exists())

    def test_concurrent_same_key_http_charges_once_and_replays_original(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        PatronageSettings.objects.create(purchase_enabled=True)
        payload = self.payload()
        barrier = Barrier(2)
        def send():
            close_old_connections()
            try:
                client=APIClient(); client.force_authenticate(get_user_model().objects.get(pk=self.user.pk))
                barrier.wait(timeout=10)
                response=client.post('/api/patronage/purchases/', payload, format='json')
                return response.status_code, response.data
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(send) for _ in range(2)]
            results=[f.result(timeout=30) for f in futures]
        self.assertEqual([r[0] for r in results], [200,200])
        self.assertEqual(len({r[1]['purchase_no'] for r in results}), 1)
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)
        self.assertEqual(CrownGrant.objects.count(), 1)
        self.assertEqual(PatronageEarning.objects.count(), 1)
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)
        self.assertEqual(self.client.get('/api/patronage/purchases/by-key/', {'idempotency_key':payload['idempotency_key']}).data['payment_status'], 'paid')

    def test_idempotency_key_is_literal_string_never_numeric_coercion(self):
        PatronageSettings.objects.create(purchase_enabled=True)
        payload = self.payload()
        for key in (123, True, [], {}, None, ''):
            response = self.client.post('/api/patronage/purchases/', {**payload, 'idempotency_key':key}, format='json')
            self.assertEqual(response.status_code, 400, (key, response.data))
        self.assertFalse(PatronagePurchase.objects.exists())
        response = self.client.post('/api/patronage/purchases/', {**payload, 'idempotency_key':' literal key '}, format='json')
        self.assertEqual(response.data['idempotency_key'], ' literal key ')
        self.assertEqual(self.client.get('/api/patronage/purchases/by-key/', {'idempotency_key':'literal key'}).status_code, 404)

    def test_coin_capability_reflects_platform_gates_without_writing(self):
        PatronageSettings.objects.create(purchase_enabled=True)
        # Pure local balance does not require a WeChat session or external grant.
        with override_settings(WECHAT_VIRTUALPAY_ENABLED=False, SHARED_SPEND_PLATFORM_APPROVED=False):
            self.assertTrue(self.quote()['can_submit'])
        self.coin()
        with override_settings(WECHAT_VIRTUALPAY_ENABLED=False, SHARED_SPEND_PLATFORM_APPROVED=False):
            quote = self.quote()
            self.assertFalse(quote['can_submit'])
            self.assertIn('PLATFORM_DISABLED', quote['blockers'])
            self.assertIn('POLICY_UNCONFIRMED', quote['blockers'])
        with override_settings(WECHAT_VIRTUALPAY_ENABLED=True, SHARED_SPEND_PLATFORM_APPROVED=True):
            self.assertTrue(self.quote()['can_submit'])
        self.assertFalse(WalletSpendAttempt.objects.exists())

    def test_quote_blocks_unfinished_checkout_even_with_enough_free_balance(self):
        from apps.orders.models import Order
        from apps.catalog.models import Package
        from apps.wallet.models import RechargeOrder
        PatronageSettings.objects.create(purchase_enabled=True)
        order = Order.objects.create(order_no='PATRONAGE-CHECKOUT', boss_user=self.user,
            package=Package.objects.create(name='held-checkout', base_price=3), total_amount=3,
            required_players=1, status=Order.STATUS_PENDING_PAYMENT)
        RechargeOrder.objects.create(recharge_no='PATRONAGE-CHECKOUT-RC', profile=self.profile,
            amount=Decimal('3'), checkout_order_no=order.order_no, status='credited',
            notify_payload={'mode':'short_series_coin'})
        quote = self.quote()
        self.assertFalse(quote['can_submit'])
        self.assertIn('CHECKOUT_RECOVERY_PENDING', quote['blockers'])
        response = self.client.post('/api/patronage/purchases/', self.payload(), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PatronagePurchase.objects.exists())

    def test_unavailable_purchase_error_contains_code_and_detail(self):
        PatronageSettings.objects.create(purchase_enabled=True)
        payload = self.payload()
        self.player.status='disabled'; self.player.save(update_fields=['status'])
        response = self.client.post('/api/patronage/purchases/', payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'PLAYER_UNAVAILABLE')
        self.assertIn('detail', response.data)
        self.assertFalse(WalletSpendAttempt.objects.exists())

    def test_prepared_cancellation_wins_auth_race_without_external_spend(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from django.db import close_old_connections
        from apps.patronage.purchases import prepare
        from apps.wallet import spend_service
        PatronageSettings.objects.create(purchase_enabled=True)
        self.coin()
        record, _ = prepare(self.user, **self.payload())
        entered, release = Event(), Event()
        class PauseAuth(FakeWeChat):
            def authenticate(fake, attempt, code):
                session = super().authenticate(attempt, code)
                entered.set()
                if not release.wait(10): raise RuntimeError('test auth timeout')
                return session
        adapter = PauseAuth()
        def execute():
            close_old_connections()
            try: return spend_service.execute(record.attempt_id, adapter=adapter).status
            finally: close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(execute)
            try:
                self.assertTrue(entered.wait(10))
                spend_service.cancel_prepared(record.attempt_id, user=self.user)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=15), 'failed')
        self.assertEqual(adapter.spends, 0)
        record.refresh_from_db()
        self.assertEqual(record.payment_status, 'failed')
        self.assertFalse(PatronageEarning.objects.exists())

    def test_balance_and_restricted_buyer_block_http_before_reservation(self):
        PatronageSettings.objects.create(purchase_enabled=True)
        payload = self.payload()
        ClientWallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('187.90'))
        result = self.client.post('/api/patronage/purchases/', payload, format='json')
        self.assertEqual(result.data['code'], 'INSUFFICIENT_BALANCE')
        ClientWallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('1000'))
        self.profile.account_status='banned'; self.profile.save(update_fields=['account_status'])
        result = self.client.post('/api/patronage/purchases/', payload, format='json')
        self.assertEqual(result.data['code'], 'ACCOUNT_RESTRICTED')
        self.assertFalse(WalletSpendAttempt.objects.exists())
        self.assertFalse(PatronagePurchase.objects.exists())

    def test_two_gates_can_enable_real_quote_capability(self):
        config = PatronageSettings.objects.create(purchase_enabled=True)
        self.assertTrue(self.quote()['can_submit'])
        with override_settings(PATRONAGE_PURCHASE_ENABLED=False):
            self.assertFalse(self.quote()['can_submit'])
        config.purchase_enabled = False
        config.save()
        self.assertFalse(self.quote()['can_submit'])

    def test_post_then_original_key_get_proves_debit_grant_and_income(self):
        PatronageSettings.objects.create(purchase_enabled=True)
        quote = self.quote()
        payload = dict(player_id=self.player.pk, package_code='day',
            price_version=quote['price_version'], idempotency_key='http-original-key')
        response = self.client.post('/api/patronage/purchases/', payload, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['payment_status'], 'paid')
        readback = self.client.get('/api/patronage/purchases/by-key/', {'idempotency_key': payload['idempotency_key']})
        self.assertEqual(readback.status_code, 200)
        self.assertEqual(readback.data, response.data)
        self.assertEqual(readback.data['idempotency_key'], payload['idempotency_key'])
        self.assertEqual(readback.data['price_version'], payload['price_version'])
        for field in ('paid_at', 'starts_at', 'expires_at'):
            self.assertIsNotNone(readback.data[field])
        purchase = PatronagePurchase.objects.get()
        self.assertEqual(purchase.attempt.kind, 'patronage')
        self.assertEqual(purchase.attempt.status, 'completed')
        self.assertEqual(CrownGrant.objects.get().purchase_id, purchase.pk)
        self.assertEqual(PatronageEarning.objects.get().net_amount, Decimal('1410'))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('19812'))
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)
        replay = self.client.post('/api/patronage/purchases/', payload, format='json')
        self.assertEqual(replay.data, response.data)
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)
