from decimal import Decimal
from django.test import TestCase, TransactionTestCase, override_settings
from django.contrib.auth.models import User
from apps.catalog.models import Package
from apps.orders.models import Order
from apps.orders import surcharges


class SurchargeQuoteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('v2-boss')
        self.package = Package.objects.create(name='v2', base_price=10, player_count=3)
        self.order = Order.objects.create(order_no='v2-order', boss_user=self.user,
            package=self.package, required_players=3, surcharge_guarded=True)

    def quote(self, amount):
        fn = getattr(surcharges, 'quote_amount', None)
        self.assertTrue(callable(fn), 'Fixed-25 equal-split quotation is missing')
        return fn(amount, self.order.required_players)

    def test_fixed_25_equal_required_headcount_without_personal_config(self):
        from apps.earnings.models import EarningsConfig, OrderCommissionOverride
        EarningsConfig.objects.update_or_create(key='default', defaults={'default_commission_rate': Decimal('16')})
        OrderCommissionOverride.objects.create(order=self.order, commission_rate=Decimal('80'), reason='base only')
        q = self.quote(12)
        self.assertEqual(q['commission_rate'], '25.00')
        self.assertEqual(q['required_players'], 3)
        self.assertEqual(q['per_person_gross_diamonds'], '4.00')
        self.assertEqual(q['per_person_commission_diamonds'], '1.00')
        self.assertEqual(q['per_person_net_diamonds'], '3.00')
        self.assertEqual(Decimal(q['per_person_gross_diamonds']) * 3, Decimal('12'))
        self.assertEqual(Decimal(q['per_person_commission_diamonds']) + Decimal(q['per_person_net_diamonds']), Decimal('4'))
        OrderCommissionOverride.objects.filter(order=self.order).update(commission_rate=0)
        self.assertEqual(self.quote(12), q)

    def test_preorder_quote_reuses_base_pricing_and_returns_equal_split(self):
        from rest_framework.test import APIClient
        client = APIClient(); client.force_authenticate(self.user)
        response = client.post('/api/boss/order/quote', {'boss_wechat': 'v2',
            'package_id': self.package.pk, 'surcharge_diamonds': 12}, format='json')
        self.assertEqual(response.status_code, 200, getattr(response, 'data', None))
        self.assertEqual(response.data['base_amount_yuan'], '10.00')
        self.assertEqual(response.data['total_amount_yuan'], '11.20')
        self.assertEqual(response.data['surcharge']['per_person_net_diamonds'], '3.00')
        self.assertEqual(Order.objects.count(), 1)

    def test_create_surcharge_preserves_base_and_replays_one_order(self):
        from rest_framework.test import APIClient
        from django.test import override_settings
        client = APIClient(); client.force_authenticate(self.user)
        data = {'boss_wechat': 'v2', 'package_id': self.package.pk, 'surcharge_diamonds': 12}
        quote = client.post('/api/boss/order/quote', data, format='json').data
        data.update(quote_version=quote['quote_version'], idempotency_key='create-v2')
        with override_settings(ORDER_SURCHARGE_ENABLED=True):
            response = client.post('/api/boss/order', data, format='json')
            self.assertEqual(response.status_code, 200, response.data)
            self.assertIn('checkout', response.data, 'Combined checkout is not persisted')
            order = Order.objects.get(order_no=response.data['order_no'])
            self.assertEqual(order.total_amount, Decimal('10'))
            self.assertEqual(order.status, Order.STATUS_PENDING_PAYMENT)
            self.assertEqual(order.surcharges.get().amount_diamonds, 12)
            self.assertEqual(order.surcharges.get().allocation_snapshot['per_person_net_diamonds'], '3.00')
            replay = client.post('/api/boss/order', data, format='json')
            self.assertEqual(replay.data['order_no'], order.order_no)
            self.assertEqual(Order.objects.count(), 2)
            data['surcharge_diamonds'] = 15
            conflict = client.post('/api/boss/order', data, format='json')
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.data['code'], 'IDEMPOTENCY_CONFLICT')

    def test_hall_only_paid_valid_snapshot_counts_and_keeps_required_headcount(self):
        from apps.orders.surcharge_models import OrderSurcharge
        from apps.orders.serializers import AvailableOrderSerializer
        sc = OrderSurcharge.objects.create(order=self.order, boss=self.user, surcharge_no='v2-sc',
            amount_diamonds=12, amount_yuan=Decimal('1.20'), idempotency_key='hall', request_digest='a'*64,
            allocation_snapshot=self.quote(12))
        dto = AvailableOrderSerializer(self.order).data
        self.assertIn('surcharge', dto)
        self.assertEqual(dto['surcharge']['paid_diamonds'], 0)
        self.assertEqual(dto['surcharge']['per_person_net_diamonds'], '0.00')
        sc.status = 'paid'; sc.save()
        dto = AvailableOrderSerializer(self.order).data['surcharge']
        self.assertEqual(dto['paid_diamonds'], 12)
        self.assertEqual(dto['required_players'], 3)
        self.assertEqual(dto['per_person_net_diamonds'], '3.00')
        sc.status = 'unknown'; sc.save()
        self.assertEqual(AvailableOrderSerializer(self.order).data['surcharge']['per_person_net_diamonds'], '0.00')
        sc.status = 'partially_refunded'; sc.refunded_diamonds = 1; sc.save()
        self.assertEqual(AvailableOrderSerializer(self.order).data['surcharge']['per_person_net_diamonds'], '0.00')
        sc.status = 'refunded'; sc.refunded_diamonds = 12; sc.save()
        self.assertEqual(AvailableOrderSerializer(self.order).data['surcharge']['paid_diamonds'], 0)

    def test_surcharge_adapter_never_reads_player_gift_config(self):
        from apps.earnings.personal_commission import get_surcharge_commission
        self.assertEqual(get_surcharge_commission(None)['commission_rate'], '25.00')

    def test_creation_by_key_is_owner_only_and_read_only(self):
        from rest_framework.test import APIClient
        client = APIClient(); client.force_authenticate(self.user)
        from django.test import override_settings
        data = {'boss_wechat': 'v2', 'package_id': self.package.pk, 'surcharge_diamonds': 12}
        quote = client.post('/api/boss/order/quote', data, format='json').data
        data.update(idempotency_key='recover-create', quote_version=quote['quote_version'])
        with override_settings(ORDER_SURCHARGE_ENABLED=True):
            created = client.post('/api/boss/order', data, format='json')
        url = '/api/boss/order/by-key'
        response = client.get(url, {'idempotency_key': 'recover-create'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['order_no'], created.data['order_no'])
        self.assertEqual(response.data['checkout']['payment_status'], 'created')
        other = User.objects.create_user('other-v2'); client.force_authenticate(other)
        self.assertEqual(client.get(url, {'idempotency_key': 'recover-create'}).status_code, 404)
        self.assertEqual(Order.objects.count(), 2)

    def test_typed_spec_quote_matches_existing_signal_time_price(self):
        from apps.catalog.models import PlayerType, PackageSpec
        from rest_framework.test import APIClient
        client = APIClient(); client.force_authenticate(self.user)
        spec = PackageSpec.objects.create(package=self.package, name='tier', price=10,
            required_player_type=PlayerType.objects.create(name='tier', priority=1))
        data = {'boss_wechat': 'v2', 'package_id': self.package.pk, 'spec_id': spec.pk,
                'quantity': 2, 'booked_hours': 2, 'surcharge_diamonds': 12}
        q = client.post('/api/boss/order/quote', data, format='json')
        self.assertEqual(q.status_code, 200, q.data)
        self.assertEqual(q.data['base_amount_yuan'], '20.00')
        data.update(quote_version=q.data['quote_version'], idempotency_key='typed')
        with override_settings(ORDER_SURCHARGE_ENABLED=True):
            created = client.post('/api/boss/order', data, format='json')
        self.assertEqual(created.status_code, 200, created.data)
        order = Order.objects.get(order_no=created.data['order_no'])
        self.assertEqual(Decimal(str(order.total_amount)), Decimal('20.00'))
        self.assertEqual(created.data['checkout']['total_amount_yuan'], '21.20')

    def test_unsupported_designated_entry_rejects_instead_of_ignoring_surcharge(self):
        from apps.orders.listing_orders import create_listing_order
        from apps.orders.serializers import DesignatedDraftSerializer
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError) as caught:
            create_listing_order({'surcharge_diamonds': 12}, 0, self.user)
        self.assertEqual(str(caught.exception.detail['code']), 'SURCHARGE_DESIGNATED_FLOW_UNAVAILABLE')
        serializer = DesignatedDraftSerializer(data={'surcharge_diamonds': 12})
        self.assertFalse(serializer.is_valid())

    def test_stale_quote_is_rejected_and_zero_keeps_legacy_creation(self):
        from rest_framework.test import APIClient
        client = APIClient(); client.force_authenticate(self.user)
        data = {'boss_wechat': 'v2', 'package_id': self.package.pk, 'surcharge_diamonds': 12}
        q = client.post('/api/boss/order/quote', data, format='json').data
        data.update(quote_version=q['quote_version'], idempotency_key='stale')
        Package.objects.filter(pk=self.package.pk).update(base_price=11)
        with override_settings(ORDER_SURCHARGE_ENABLED=True):
            response = client.post('/api/boss/order', data, format='json')
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data['code'], 'PRICE_CHANGED')
        self.assertEqual(Order.objects.count(), 1)
        for amount in (0, None):
            plain = {'boss_wechat': 'v2', 'package_id': self.package.pk}
            if amount is not None: plain['surcharge_diamonds'] = amount
            response = client.post('/api/boss/order', plain, format='json')
            self.assertEqual(response.status_code, 200, response.data)
            order = Order.objects.get(order_no=response.data['order_no'])
            self.assertFalse(order.surcharge_guarded)
            self.assertFalse(order.surcharges.exists())
            self.assertEqual(order.status, Order.STATUS_WAITING)
            self.assertNotIn('checkout', response.data)

    def test_strict_input_and_precision(self):
        from rest_framework.exceptions import ValidationError
        self.assertEqual(self.quote(0)['per_person_net_diamonds'], '0.00')
        for value in (True, '12', 12.0, -1, 1, 99999999999999):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.quote(value)
        for count in (0, -1, True, '3'):
            with self.subTest(count=count), self.assertRaises(ValidationError):
                surcharges.quote_amount(12, count)


@override_settings(ORDER_SURCHARGE_ENABLED=True, WECHAT_VIRTUALPAY_ENV=1)
class CheckoutPaymentTests(TransactionTestCase):
    def setUp(self):
        from apps.accounts.models import ClientProfile
        from apps.wallet.models import ClientWallet
        from rest_framework.test import APIClient
        self.user = User.objects.create_user('checkout-v2')
        self.profile = ClientProfile.objects.create(user=self.user, openid='checkout-v2-offline')
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile, defaults={'balance': Decimal('30')})
        package = Package.objects.create(name='checkout', base_price=10, player_count=3)
        self.client = APIClient(); self.client.force_authenticate(self.user)
        data = {'boss_wechat': 'checkout', 'package_id': package.pk, 'surcharge_diamonds': 12}
        quote = self.client.post('/api/boss/order/quote', data, format='json').data
        data.update(quote_version=quote['quote_version'], idempotency_key='checkout')
        r = self.client.post('/api/boss/order', data, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.order = Order.objects.get(order_no=r.data['order_no'])

    def test_one_confirmation_pays_exact_total_and_preserves_separate_base(self):
        from apps.wallet.models import WalletSpendAttempt, ClientWalletLedger
        from apps.payments.models import Payment
        response = self.client.post('/api/pay/balance/create', {'order_no': self.order.order_no}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['amount'], '11.20')
        self.wallet.refresh_from_db(); self.order.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('18.80'))
        self.assertTrue(self.order.paid)
        self.assertEqual(self.order.status, Order.STATUS_WAITING)
        self.assertEqual(Payment.objects.get(order=self.order).amount, Decimal('10'))
        self.assertEqual(self.order.surcharges.get().status, 'paid')
        self.assertEqual(WalletSpendAttempt.objects.get().amount, Decimal('11.20'))
        replay = self.client.post('/api/pay/balance/create', {'order_no': self.order.order_no}, format='json')
        self.assertEqual(replay.status_code, 200, replay.data)
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('18.80'))
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)

    def add_coin_source(self):
        from apps.wallet.models import RechargeOrder, ClientWalletLedger
        RechargeOrder.objects.create(recharge_no='v2-coin', profile=self.profile, amount=Decimal('30'),
            status='credited', notify_payload={'mode': 'short_series_coin', 'wechat_coin_units_per_yuan': 10})
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='recharge', amount=Decimal('30'),
            balance_after=Decimal('30'), reference_id='v2-coin')

    def test_unknown_replays_original_attempt_without_redebit_or_cancel_half_refund(self):
        from apps.orders.checkout_payment import pay, fulfill
        from apps.wallet import spend_service as spend
        from apps.wallet.models import WalletSpendAttempt
        from apps.payments.models import Payment, Refund
        self.add_coin_source()
        class Adapter:
            calls = 0
            def authenticate(self, attempt, code): return 'ephemeral'
            def spend(self, attempt, session):
                self.calls += 1
                raise TimeoutError('synthetic lost response')
            def query(self, attempt): return None
        adapter = Adapter()
        self.assertEqual(pay(self.order.order_no, self.user, adapter=adapter)['status'], 'unknown')
        attempt = WalletSpendAttempt.objects.get()
        original = (attempt.external_id, attempt.request_payload, attempt.source_snapshot)
        for _ in range(2):
            self.assertEqual(pay(self.order.order_no, self.user, adapter=adapter)['status'], 'unknown')
            spend.recover(attempt.pk, adapter=adapter, apply=fulfill)
        attempt.refresh_from_db()
        self.assertEqual((attempt.external_id, attempt.request_payload, attempt.source_snapshot), original)
        self.assertEqual(attempt.reserved_amount, Decimal('11.20'))
        self.assertEqual(adapter.calls, 1)
        response = self.client.post('/api/boss/order/' + self.order.order_no + '/cancel', {}, format='json')
        self.assertEqual(response.status_code, 409, response.data)
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('30'))
        self.assertFalse(Payment.objects.exists()); self.assertFalse(Refund.objects.exists())

    def test_remote_success_local_failure_management_recovery_finishes_once(self):
        from apps.orders.checkout_payment import pay
        from apps.wallet.models import WalletSpendAttempt, ClientWalletLedger
        from apps.payments.models import Payment
        from django.core.management import call_command
        from unittest.mock import patch
        self.add_coin_source()
        class Adapter:
            calls = 0
            def authenticate(self, attempt, code): return 'ephemeral'
            def spend(self, attempt, session):
                self.calls += 1
                return {'errcode': 0, 'order_id': attempt.external_id}
        adapter = Adapter()
        with patch.object(Payment.objects, 'create', side_effect=RuntimeError('local failure')):
            with self.assertRaises(RuntimeError): pay(self.order.order_no, self.user, adapter=adapter)
        attempt = WalletSpendAttempt.objects.get()
        self.assertEqual(attempt.status, 'succeeded')
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('30'))
        call_command('reconcile_wallet_spends', apply=True, attempt_id=attempt.pk)
        call_command('reconcile_wallet_spends', apply=True, attempt_id=attempt.pk)
        attempt.refresh_from_db(); self.assertEqual(attempt.status, 'completed')
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('18.80'))
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)

    def test_base_refund_service_cannot_half_refund_a_bundle(self):
        from apps.orders.checkout_payment import pay
        from apps.payments.models import Payment, Refund
        from apps.payments.refund_integrity import ensure_payment_refund
        from rest_framework.exceptions import ValidationError
        pay(self.order.order_no, self.user)
        payment = Payment.objects.get(order=self.order)
        with self.assertRaises(ValidationError) as caught:
            ensure_payment_refund(payment.payment_no, Decimal('10'))
        self.assertEqual(str(caught.exception.detail['code']), 'SURCHARGE_REFUND_REQUIRES_REVIEW')
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('18.80'))
        self.assertFalse(Refund.objects.exists())

    def test_manual_confirmation_cannot_bypass_combined_checkout(self):
        with override_settings(ENABLE_MOCK_PAYMENT=True):
            response = self.client.post('/api/boss/order/' + self.order.order_no + '/self-confirm-payment', {}, format='json')
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data['code'], 'SURCHARGE_USE_COMBINED_CHECKOUT')
        self.order.refresh_from_db(); self.assertFalse(self.order.paid)
        self.assertEqual(self.order.status, Order.STATUS_PENDING_PAYMENT)
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('30'))

    def test_failed_prepared_is_not_displayed_as_pending_reward(self):
        from apps.orders.checkout_payment import prepare
        from apps.wallet.spend_service import cancel_prepared
        from apps.orders.serializers import AvailableOrderSerializer
        item = prepare(self.order.order_no, self.user)
        cancel_prepared(item.attempt_id)
        dto = AvailableOrderSerializer(self.order).data['surcharge']
        self.assertEqual(dto['pending_diamonds'], 0)
        self.assertEqual(dto['paid_diamonds'], 0)
        self.assertEqual(dto['per_person_net_diamonds'], '0.00')

    def test_other_payment_channels_cannot_charge_base_only(self):
        from apps.payments.services import create_payment
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError) as caught:
            create_payment(self.order.order_no, 'wechat')
        self.assertEqual(str(caught.exception.detail['code']), 'SURCHARGE_USE_COMBINED_CHECKOUT')
