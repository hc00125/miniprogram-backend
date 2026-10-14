"""Synthetic fixtures on isolated PostgreSQL; no production payment path."""
from decimal import Decimal
from importlib.util import find_spec
from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.wallet.models import ClientWallet, WalletSpendAttempt
from apps.wallet import spend_service
from apps.earnings.models import PlayerWallet, WalletLedger
from apps.patronage.models import PatronagePurchase


@override_settings(FISH_CRACKER_EXCHANGE_RATE=10)
class PatronageIncomeTests(TransactionTestCase):
    def setUp(self):
        self.boss = get_user_model().objects.create_user('patronage-income-boss')
        self.profile = ClientProfile.objects.create(user=self.boss, openid='offline-patronage-boss')
        self.buyer_wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile,
            defaults={'balance': Decimal('5000.00')})
        self.player = Player.objects.create(name='Synthetic patronage recipient',
            player_type=PlayerType.objects.create(name='income-type', priority=1))

    def purchase(self, key='income', amount='188.00', rate='0.25', package='day'):
        amount, rate = Decimal(amount), Decimal(rate)
        return PatronagePurchase.objects.create(boss=self.boss, player=self.player,
            player_name=self.player.name, package_code=package, package_name=package,
            amount_yuan=amount, commission_rate=rate, platform_amount_yuan=amount*rate,
            player_amount_yuan=amount*(1-rate), price_version='b'*64,
            config_snapshot={'contract_version': '2.0'}, idempotency_key=key,
            bonus_naming_days=7 if package == 'day_pass' else 0)

    def prepare(self, purchase, **overrides):
        # Unit-level evidence fixture, intentionally independent of HTTP dispatch.
        # HTTP integration tests separately exercise real reserve/execute/finalize.
        values = dict(wallet=self.buyer_wallet, kind='patronage',
            business_no=purchase.purchase_no, idempotency_key=purchase.idempotency_key,
            request_digest='c'*64, amount=purchase.amount_yuan, reserved_amount=purchase.amount_yuan,
            source_snapshot={'coin_amount': '0.00', 'local_amount': str(purchase.amount_yuan),
                'coin_units': 0, 'scale': 10, 'allocations': []},
            request_payload={}, status='succeeded', evidence={'local_only': True})
        values.update(overrides)
        attempt = WalletSpendAttempt.objects.create(**values)
        purchase.attempt = attempt
        purchase.save(update_fields=['attempt'])
        return attempt

    def mark_paid(self, purchase):
        purchase.payment_status = 'paid'
        purchase.paid_at = timezone.now()
        purchase.save(update_fields=['payment_status', 'paid_at'])
        return purchase

    def settled(self, purchase):
        attempt = self.prepare(purchase)
        spend_service.finalize(attempt.pk, apply=lambda confirmed: None)  # Synthetic debit-only fixture for isolated credit tests.
        return self.mark_paid(purchase)

    def credit(self, purchase):
        from apps.patronage.income import credit_purchase
        return credit_purchase(purchase)

    def test_invalid_exchange_rate_fails_closed_without_income(self):
        from rest_framework.exceptions import ValidationError
        purchase = self.settled(self.purchase())
        for rate in ('0', '-1', 'NaN', 'Infinity', 'not-a-rate', '1.0000000000001', '1000000000000'):
            with self.subTest(rate=rate), override_settings(FISH_CRACKER_EXCHANGE_RATE=rate):
                with self.assertRaises(ValidationError) as caught:
                    self.credit(purchase)
                self.assertEqual(str(caught.exception.detail['code']), 'INVALID_INCOME_CONFIGURATION')
                self.assertFalse(apps.get_model('patronage', 'PatronageEarning').objects.exists())
                self.assertFalse(PlayerWallet.objects.filter(player=self.player).exists())

    def test_income_receipt_cannot_be_edited_or_deleted_by_bulk_sql(self):
        from django.db import IntegrityError, transaction
        earning = self.credit(self.settled(self.purchase()))
        model = type(earning)
        for operation in (lambda: model.objects.filter(pk=earning.pk).update(snapshot={}),
                          lambda: model.objects.filter(pk=earning.pk).delete()):
            with self.assertRaises(IntegrityError), transaction.atomic():
                operation()
        self.assertEqual(model.objects.get().net_amount, Decimal('1410.00'))

    def test_zero_commission_and_actual_exchange_rate_are_frozen(self):
        from apps.patronage.models import PatronageSettings
        purchase = self.settled(self.purchase(rate='0', amount='340.00', package='day_pass'))
        with override_settings(FISH_CRACKER_EXCHANGE_RATE='7.25'):
            earning = self.credit(purchase)
        self.assertEqual(earning.commission_amount, Decimal('0.00'))
        self.assertEqual(earning.net_amount, Decimal('340.00') * Decimal('7.25'))
        self.assertEqual(earning.fish_per_yuan, Decimal('7.25'))
        original = earning.snapshot.copy()
        PatronageSettings.objects.create(commission_rate=Decimal('0.5'), day_price_yuan=999)
        with override_settings(FISH_CRACKER_EXCHANGE_RATE='invalid-now'):
            replay = self.credit(purchase)
        self.assertEqual(replay.pk, earning.pk)
        self.assertEqual(replay.snapshot, original)
        self.assertEqual(replay.fish_per_yuan, Decimal('7.25'))
        self.assertEqual(WalletLedger.objects.filter(entry_type='patronage_income').count(), 1)

    def test_gift_rounding_conserves_fractional_fish(self):
        from decimal import ROUND_HALF_UP
        for index, (amount, rate, exchange) in enumerate([
            ('0.10', '0.25', '1.25'), ('188.00', '0.2501', '7.123456789123'),
            ('0.10', '0.9999', '10'), ('0.10', '1', '10'), ('0.10', '0', '0.01'),
        ]):
            with self.subTest(amount=amount, rate=rate, exchange=exchange):
                purchase = self.settled(self.purchase(key=f'fraction-{index}', amount=amount, rate=rate))
                with override_settings(FISH_CRACKER_EXCHANGE_RATE=exchange):
                    earning = self.credit(purchase)
                gross = (Decimal(amount)*Decimal(exchange)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                commission = (gross*Decimal(rate)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                self.assertEqual(earning.gross_amount, gross)
                self.assertEqual(earning.commission_amount, commission)
                self.assertEqual(earning.net_amount, gross-commission)
                self.assertEqual(earning.net_amount, earning.credited_amount+earning.debt_offset_amount)
                if not earning.net_amount:
                    self.assertIsNone(earning.credit_ledger_id)
                    self.assertIsNone(earning.debt_ledger_id)
                earning.refresh_from_db()
                self.assertEqual(earning.fish_per_yuan, Decimal(exchange))

    def test_partial_debt_offsets_before_available_credit(self):
        PlayerWallet.objects.create(player=self.player, available_balance=Decimal('3.00'),
            pending_balance=Decimal('2.00'), debt_balance=Decimal('410.00'))
        earning = self.credit(self.settled(self.purchase()))
        wallet = PlayerWallet.objects.get(player=self.player)
        self.assertEqual(earning.debt_offset_amount, Decimal('410.00'))
        self.assertEqual(earning.credited_amount, Decimal('1000.00'))
        self.assertEqual(wallet.available_balance, Decimal('1003.00'))
        self.assertEqual(wallet.pending_balance, Decimal('2.00'))
        self.assertEqual(wallet.debt_balance, Decimal('0.00'))
        self.assertEqual(earning.debt_ledger.delta, Decimal('-410.00'))
        self.assertEqual(earning.debt_ledger.balance_after, Decimal('0.00'))
        self.assertEqual(earning.credit_ledger.balance_after, Decimal('1003.00'))

    def test_full_debt_offsets_without_available_ledger(self):
        PlayerWallet.objects.create(player=self.player, debt_balance=Decimal('2000.00'))
        earning = self.credit(self.settled(self.purchase()))
        wallet = PlayerWallet.objects.get(player=self.player)
        self.assertEqual(earning.debt_offset_amount, earning.net_amount)
        self.assertEqual(earning.credited_amount, Decimal('0.00'))
        self.assertIsNone(earning.credit_ledger_id)
        self.assertEqual(wallet.available_balance, Decimal('0.00'))
        self.assertEqual(wallet.debt_balance, Decimal('590.00'))
        self.assertEqual(WalletLedger.objects.filter(entry_type='patronage_income').count(), 1)

    def test_day_pass_bonus_never_creates_second_income_or_auto_grant(self):
        from datetime import timedelta
        from apps.patronage.models import CrownGrant, PatronageEarning
        purchase = self.settled(self.purchase(amount='340', package='day_pass'))
        earning = self.credit(purchase)
        self.assertFalse(CrownGrant.objects.exists())
        # Existing explicit grant fixture is not a proposed activation policy.
        CrownGrant.objects.create(purchase=purchase, source='day_pass_bonus', package_name='existing bonus',
            starts_at=timezone.now(), expires_at=timezone.now()+timedelta(days=7))
        self.assertEqual(self.credit(purchase).pk, earning.pk)
        self.assertEqual(PatronageEarning.objects.count(), 1)
        self.assertEqual(earning.net_amount, Decimal('2550.00'))
        purchase.refresh_from_db()
        self.assertIsNone(purchase.starts_at)
        self.assertIsNone(purchase.expires_at)

    def test_created_or_unbound_paid_purchase_never_earns(self):
        from rest_framework.exceptions import ValidationError
        purchase = self.purchase()
        purchase.payment_status = 'paid'  # Caller object's spoofed flag ignored.
        purchase.paid_at = timezone.now()
        with self.assertRaises(ValidationError):
            self.credit(purchase)
        self.mark_paid(purchase)
        with self.assertRaises(ValidationError):
            self.credit(purchase)
        self.assertFalse(PlayerWallet.objects.filter(player=self.player).exists())

    def test_nonpaid_purchase_states_reject_even_with_real_debit(self):
        from rest_framework.exceptions import ValidationError
        purchase = self.settled(self.purchase())
        for status in ('created', 'processing', 'unknown', 'failed'):
            PatronagePurchase.objects.filter(pk=purchase.pk).update(payment_status=status)
            with self.subTest(status=status), self.assertRaises(ValidationError):
                self.credit(purchase)
        self.assertFalse(PlayerWallet.objects.filter(player=self.player).exists())

    def test_unfinalized_attempt_states_reject(self):
        from rest_framework.exceptions import ValidationError
        purchase = self.purchase()
        attempt = self.prepare(purchase)
        self.mark_paid(purchase)
        from django.db import transaction
        for status in ('prepared', 'dispatching', 'unknown', 'succeeded', 'failed', 'completed'):
            # Roll back each synthetic state instead of illegally resurrecting
            # a terminal shared attempt; its production trigger stays enabled.
            with transaction.atomic():
                WalletSpendAttempt.objects.filter(pk=attempt.pk).update(status=status,
                    reserved_amount=0 if status in ('failed', 'completed') else purchase.amount_yuan)
                with self.subTest(status=status), self.assertRaises(ValidationError):
                    self.credit(purchase)
                transaction.set_rollback(True)
        self.assertFalse(PlayerWallet.objects.filter(player=self.player).exists())

    def test_mismatched_payment_identity_rejects(self):
        from rest_framework.exceptions import ValidationError
        other = get_user_model().objects.create_user('different-buyer')
        profile = ClientProfile.objects.create(user=other, openid='offline-different-buyer', nickname='different-buyer')
        wallet, _ = ClientWallet.objects.update_or_create(profile=profile, defaults={'balance': Decimal('5000')})
        for index, change in enumerate([
            {'kind': 'gift'}, {'business_no': 'unrelated'}, {'idempotency_key': 'unrelated'},
            {'amount': Decimal('1'), 'reserved_amount': Decimal('1')}, {'wallet': wallet},
        ]):
            with self.subTest(change=change):
                purchase = self.purchase(key=f'mismatch-{index}')
                attempt = self.prepare(purchase, **change)
                spend_service.finalize(attempt.pk, apply=lambda confirmed: None)  # Synthetic debit-only fixture for isolated credit tests.
                self.mark_paid(purchase)
                with self.assertRaises(ValidationError):
                    self.credit(purchase)
        self.assertFalse(PlayerWallet.objects.filter(player=self.player).exists())

    def test_debit_reference_or_amount_mismatch_rejects(self):
        from rest_framework.exceptions import ValidationError
        from apps.wallet.models import ClientWalletLedger
        purchase = self.settled(self.purchase())
        debit = ClientWalletLedger.objects.get(entry_type='shared_spend')
        for changes in ({'amount': Decimal('-1')}, {'reference_type': 'gift'}, {'reference_id': 'wrong'}):
            original = {key: getattr(debit, key) for key in changes}
            ClientWalletLedger.objects.filter(pk=debit.pk).update(**changes)
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.credit(purchase)
            ClientWalletLedger.objects.filter(pk=debit.pk).update(**original)
        self.assertFalse(PlayerWallet.objects.filter(player=self.player).exists())

    def test_attempt_binding_is_write_once_and_old_prices_still_immutable(self):
        from django.db import IntegrityError, transaction
        purchase = self.purchase()
        attempt = self.prepare(purchase)
        for change in ({'attempt_id': None}, {'amount_yuan': Decimal('200')}, {'config_snapshot': {}}):
            with self.subTest(change=change), self.assertRaises(IntegrityError), transaction.atomic():
                PatronagePurchase.objects.filter(pk=purchase.pk).update(**change)
        purchase.refresh_from_db()
        self.assertEqual(purchase.attempt_id, attempt.pk)
        self.assertEqual(purchase.amount_yuan, Decimal('188.00'))

    def test_persistent_purchase_and_payment_ledger_uniqueness(self):
        from django.db import IntegrityError, transaction
        earning = self.credit(self.settled(self.purchase()))
        values = {f.attname: getattr(earning, f.attname) for f in earning._meta.concrete_fields}
        with self.assertRaises(IntegrityError), transaction.atomic():
            type(earning).objects.create(**values)
        second = self.settled(self.purchase(key='second'))
        values['purchase_id'] = second.pk
        with self.assertRaises(IntegrityError), transaction.atomic():
            type(earning).objects.create(**values)

    def test_wallet_and_each_ledger_and_receipt_failure_roll_back(self):
        from unittest.mock import patch
        from apps.earnings import wallet as ew
        from apps.patronage.models import PatronageEarning
        purchase = self.settled(self.purchase())
        PlayerWallet.objects.create(player=self.player, debt_balance=Decimal('10.00'))
        original = ew.write_ledger
        for point in ('wallet', 'first_ledger', 'second_ledger', 'receipt'):
            calls = []
            def injected(*args, **kwargs):
                calls.append(True)
                if len(calls) == (2 if point == 'second_ledger' else 1):
                    raise RuntimeError('synthetic failure')
                return original(*args, **kwargs)
            target = ('apps.earnings.models.PlayerWallet.save' if point == 'wallet' else
                      'apps.patronage.models.PatronageEarning.objects.create' if point == 'receipt' else
                      'apps.earnings.wallet.write_ledger')
            effect = injected if 'ledger' in point else RuntimeError('synthetic failure')
            with self.subTest(point=point), patch(target, side_effect=effect):
                with self.assertRaises(RuntimeError):
                    self.credit(purchase)
            wallet = PlayerWallet.objects.get(player=self.player)
            self.assertEqual(wallet.debt_balance, Decimal('10.00'))
            self.assertEqual(wallet.available_balance, Decimal('0.00'))
            self.assertFalse(PatronageEarning.objects.exists())
            self.assertFalse(WalletLedger.objects.filter(entry_type='patronage_income').exists())
        earning = self.credit(purchase)
        self.assertEqual(earning.credited_amount, earning.net_amount-Decimal('10.00'))

    def test_failure_inside_real_finalize_rolls_back_debit_and_paid_state(self):
        from unittest.mock import patch
        from apps.wallet.models import ClientWalletLedger
        purchase = self.purchase()
        attempt = self.prepare(purchase)
        def apply(completed):
            self.assertEqual(completed.status, 'completed')
            self.credit(self.mark_paid(PatronagePurchase.objects.get(pk=purchase.pk)))
        with patch('apps.earnings.wallet.write_ledger', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                spend_service.finalize(attempt.pk, apply=apply)
        attempt.refresh_from_db()
        purchase.refresh_from_db()
        self.buyer_wallet.refresh_from_db()
        self.assertEqual(attempt.status, 'succeeded')
        self.assertEqual(attempt.reserved_amount, purchase.amount_yuan)
        self.assertEqual(purchase.payment_status, 'created')
        self.assertIsNone(purchase.paid_at)
        self.assertEqual(self.buyer_wallet.balance, Decimal('5000.00'))
        self.assertFalse(ClientWalletLedger.objects.filter(entry_type='shared_spend').exists())
        self.assertFalse(PlayerWallet.objects.filter(player=self.player).exists())
        spend_service.finalize(attempt.pk, apply=apply)
        self.assertEqual(PlayerWallet.objects.get(player=self.player).available_balance, Decimal('1410.00'))
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)

    def test_unknown_fake_external_query_recovery_credits_only_after_confirmation(self):
        from rest_framework.exceptions import ValidationError
        purchase = self.purchase()
        attempt = self.prepare(purchase, status='unknown', evidence={},
            source_snapshot={'coin_units': 1880}, request_payload={'openid': 'offline', 'env': 1})
        def apply(completed):
            self.credit(self.mark_paid(PatronagePurchase.objects.get(pk=purchase.pk)))
        class FakeQuery:
            def __init__(self, amount):
                self.amount = amount
            def query(self, current):
                return {'order_id': current.external_id, 'amount': self.amount,
                    'openid': 'offline', 'env': 1, 'status': 'succeeded'}
        spend_service.recover(attempt.pk, adapter=FakeQuery(1), apply=apply)
        with self.assertRaises(ValidationError):
            self.credit(purchase)
        self.assertFalse(PlayerWallet.objects.filter(player=self.player).exists())
        spend_service.recover(attempt.pk, adapter=FakeQuery(1880), apply=apply)
        spend_service.recover(attempt.pk, adapter=FakeQuery(1880), apply=apply)
        self.assertEqual(PlayerWallet.objects.get(player=self.player).available_balance, Decimal('1410.00'))
        self.assertEqual(WalletLedger.objects.filter(entry_type='patronage_income').count(), 1)

    def test_existing_withdrawal_return_and_payout_without_fabricated_order(self):
        from apps.earnings.models import EarningsConfig, Withdrawal, WithdrawalAllocation
        from apps.earnings.withdrawals import create_withdrawal, return_withdrawal, approve_withdrawal, mark_withdrawal_paid
        self.credit(self.settled(self.purchase()))
        EarningsConfig.objects.update_or_create(key='default', defaults={'min_withdrawal_amount': Decimal('10')})
        def withdraw():
            return create_withdrawal(self.player, Decimal('1410'), Withdrawal.METHOD_WECHAT,
                'Synthetic recipient', 'offline-account')
        return_withdrawal(withdraw(), 'synthetic rejection')
        self.assertEqual(PlayerWallet.objects.get(player=self.player).available_balance, Decimal('1410'))
        withdrawal = approve_withdrawal(withdraw())
        withdrawal.transfer_no = 'offline-proof'
        withdrawal.save(update_fields=['transfer_no'])
        mark_withdrawal_paid(withdrawal)
        wallet = PlayerWallet.objects.get(player=self.player)
        self.assertEqual(wallet.available_balance, Decimal('0'))
        self.assertEqual(wallet.withdrawing_balance, Decimal('0'))
        self.assertEqual(wallet.withdrawn_total, Decimal('1410'))
        self.assertFalse(WithdrawalAllocation.objects.exists())

    def test_parallel_replay_creates_exactly_one_income(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections, connection
        from apps.patronage.models import PatronageEarning
        purchase = self.settled(self.purchase())
        barrier = Barrier(3)
        def worker():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                barrier.wait(timeout=10)
                return self.credit(PatronagePurchase(pk=purchase.pk)).pk
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _: worker(), range(3)))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(PatronageEarning.objects.count(), 1)
        self.assertEqual(WalletLedger.objects.filter(entry_type='patronage_income').count(), 1)
        self.assertEqual(PlayerWallet.objects.get(player=self.player).available_balance, Decimal('1410.00'))

    def test_parallel_distinct_purchases_share_player_wallet_without_lost_credit(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections, connection
        first = self.settled(self.purchase(key='parallel-1'))
        other = get_user_model().objects.create_user('parallel-boss')
        profile = ClientProfile.objects.create(user=other, openid='offline-parallel', nickname='parallel')
        other_wallet, _ = ClientWallet.objects.update_or_create(profile=profile, defaults={'balance': Decimal('5000')})
        self.boss, self.buyer_wallet = other, other_wallet
        second = self.settled(self.purchase(key='parallel-2'))
        barrier = Barrier(2)
        def worker(pk):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                barrier.wait(timeout=10)
                return self.credit(PatronagePurchase(pk=pk)).pk
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(worker, [first.pk, second.pk]))
        self.assertEqual(len(set(results)), 2)
        self.assertEqual(PlayerWallet.objects.filter(player=self.player).count(), 1)
        self.assertEqual(PlayerWallet.objects.get(player=self.player).available_balance, Decimal('1410')*2)

    def test_unrepresentable_fish_amount_is_explicitly_rejected(self):
        from rest_framework.exceptions import ValidationError
        purchase = self.settled(self.purchase())
        with override_settings(FISH_CRACKER_EXCHANGE_RATE='999999999999'):
            with self.assertRaises(ValidationError) as caught:
                self.credit(purchase)
        self.assertEqual(str(caught.exception.detail['code']), 'INCOME_AMOUNT_UNSUPPORTED')
        self.assertFalse(PlayerWallet.objects.filter(player=self.player).exists())

    def test_income_source_admin_is_read_only(self):
        from django.contrib import admin
        from apps.patronage.models import PatronageEarning
        self.assertIn(PatronageEarning, admin.site._registry)
        record_admin = admin.site._registry[PatronageEarning]
        self.assertFalse(record_admin.has_add_permission(None))
        self.assertFalse(record_admin.has_change_permission(None))
        self.assertFalse(record_admin.has_delete_permission(None))

    def test_confirmed_purchase_credits_fish_with_real_source(self):
        self.assertIsNotNone(find_spec('apps.patronage.income'), 'Missing independent income service')
        earning = self.credit(self.settled(self.purchase()))
        self.assertEqual(earning.gross_amount, Decimal('1880.00'))
        self.assertEqual(earning.commission_amount, Decimal('470.00'))
        self.assertEqual(earning.net_amount, Decimal('1410.00'))
        self.assertEqual(earning.credited_amount, Decimal('1410.00'))
        self.assertEqual(earning.fish_per_yuan, Decimal('10'))
        self.assertEqual(earning.amount_yuan, Decimal('188.00'))
        self.assertEqual(earning.commission_rate, Decimal('0.25'))
        self.assertIsNotNone(earning.release_at)
        wallet = PlayerWallet.objects.get(player=self.player)
        self.assertEqual(wallet.available_balance, Decimal('1410.00'))
        self.assertEqual(wallet.pending_balance, Decimal('0.00'))
        ledger = WalletLedger.objects.get(reference_type='patronage_earning')
        self.assertEqual(ledger.entry_type, 'patronage_income')
        self.assertEqual(ledger.reference_id, str(earning.pk))
        self.assertEqual(earning.credit_ledger_id, ledger.pk)
        self.buyer_wallet.refresh_from_db()
        self.assertEqual(self.buyer_wallet.balance, Decimal('4812.00'))
        self.assertFalse(apps.get_model('gifts', 'GiftEarning').objects.exists())
        self.assertFalse(apps.get_model('earnings', 'PlayerEarning').objects.exists())
        self.assertFalse(apps.get_model('orders', 'Order').objects.exists())
