"""Real PG fulfillment tests, fake payment attempts and no real network."""
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from importlib.util import find_spec

from django.test import TransactionTestCase, override_settings
from apps.patronage.tests import test_income as income_fixtures
from apps.patronage.models import CrownGrant, PatronagePurchase, PatronageEarning
from apps.earnings.models import PlayerWallet
from apps.wallet import spend_service


@override_settings(FISH_CRACKER_EXCHANGE_RATE=10)
class FulfillmentTests(TransactionTestCase):
    # Reuse ONLY fixture helpers, not inherited test methods/discovery entries.
    setUp = income_fixtures.PatronageIncomeTests.setUp
    purchase = income_fixtures.PatronageIncomeTests.purchase
    prepare = income_fixtures.PatronageIncomeTests.prepare
    mark_paid = income_fixtures.PatronageIncomeTests.mark_paid
    settled = income_fixtures.PatronageIncomeTests.settled
    credit = income_fixtures.PatronageIncomeTests.credit

    def fulfill(self, purchase, when=None):
        from apps.patronage.fulfillment import fulfill
        attempt = self.prepare(purchase)
        when = when or datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        spend_service.finalize(attempt.pk, apply=lambda a: fulfill(a, confirmed_at=when))
        purchase.refresh_from_db()
        return purchase

    def test_active_renewal_and_bonus_add_days_once_and_display_one_boss(self):
        from django.utils import timezone
        from rest_framework.test import APIClient
        from apps.patronage.fulfillment import fulfill
        now = timezone.now()
        first = self.fulfill(self.purchase(key='week', package='week'), now)
        second = self.fulfill(self.purchase(key='year', package='year'), now)
        self.assertEqual(second.expires_at, first.expires_at+timedelta(days=365))
        bonus = self.fulfill(self.purchase(key='bonus', package='day_pass'), now)
        self.assertEqual(bonus.expires_at, second.expires_at+timedelta(days=7))
        grant = CrownGrant.objects.get(purchase=bonus)
        self.assertEqual(grant.extension_starts_at, second.expires_at)
        self.assertEqual(grant.duration_days, 7)
        self.assertEqual(grant.source, 'day_pass_bonus')
        self.assertEqual(fulfill(bonus.attempt, confirmed_at=now+timedelta(days=30)).expires_at, bonus.expires_at)
        self.assertEqual(CrownGrant.objects.count(), 3)
        self.assertEqual(PatronageEarning.objects.count(), 3)
        rows = APIClient().get('/api/patronage/crowns/', {'player_id': self.player.pk}).json()['results']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], grant.pk)
        self.assertEqual(rows[0]['source'], 'day_pass_bonus')

    def test_delivered_crown_snapshot_cannot_be_rewritten_or_deleted(self):
        from django.db import IntegrityError, transaction
        purchase = self.fulfill(self.purchase())
        grant = CrownGrant.objects.get(purchase=purchase)
        for operation in (
            lambda: CrownGrant.objects.filter(pk=grant.pk).update(duration_days=2),
            lambda: CrownGrant.objects.filter(pk=grant.pk).update(expires_at=grant.expires_at+timedelta(days=1)),
            lambda: CrownGrant.objects.filter(pk=grant.pk).delete(),
        ):
            with self.assertRaises(IntegrityError), transaction.atomic():
                operation()

    def test_expired_term_restarts_at_new_payment_and_old_receipt_is_unchanged(self):
        at = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        first = self.fulfill(self.purchase(key='expired'), at)
        old_end = first.expires_at
        later = at+timedelta(days=10)
        second = self.fulfill(self.purchase(key='new-week', package='week'), later)
        self.assertEqual(second.starts_at, later)
        self.assertEqual(second.expires_at, later+timedelta(days=7))
        first.refresh_from_db()
        self.assertEqual(first.expires_at, old_end)
        self.assertEqual(CrownGrant.objects.get(purchase=first).expires_at, old_end)

    def test_all_approved_durations_accumulate_without_service_timers(self):
        at = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        end = at
        for package, days in [('day', 1), ('week', 7), ('month', 30), ('quarter', 90), ('year', 365), ('day_pass', 7)]:
            with self.subTest(package=package):
                purchase = self.fulfill(self.purchase(key=package, package=package), at)
                end += timedelta(days=days)
                self.assertEqual(purchase.expires_at, end)
                self.assertEqual(CrownGrant.objects.get(purchase=purchase).duration_days, days)
        from django.apps import apps
        self.assertFalse(apps.get_model('orders', 'Order').objects.exists())

    def test_different_bosses_and_players_do_not_share_terms(self):
        from django.contrib.auth import get_user_model
        from apps.accounts.models import ClientProfile
        from apps.wallet.models import ClientWallet
        from apps.players.models import Player
        from rest_framework.test import APIClient
        from django.utils import timezone
        at = timezone.now()
        first_player = self.player
        first = self.fulfill(self.purchase(key='first', package='month'), at)
        original_boss, original_wallet = self.boss, self.buyer_wallet
        self.boss = get_user_model().objects.create_user('other-crown-boss')
        profile = ClientProfile.objects.create(user=self.boss, openid='offline-crown-other', nickname='other-crown')
        self.buyer_wallet, _ = ClientWallet.objects.update_or_create(profile=profile, defaults={'balance': Decimal('5000')})
        other = self.fulfill(self.purchase(key='other', package='week'), at)
        self.assertEqual(other.expires_at, at+timedelta(days=7))
        rows = APIClient().get('/api/patronage/crowns/', {'player_id': self.player.pk}).json()['results']
        self.assertEqual(len(rows), 2)
        self.boss, self.buyer_wallet = original_boss, original_wallet
        self.player = Player.objects.create(name='independent player', player_type=first_player.player_type)
        independent = self.fulfill(self.purchase(key='other-player'), at)
        self.assertEqual(independent.expires_at, at+timedelta(days=1))
        first.refresh_from_db()
        self.assertEqual(first.expires_at, at+timedelta(days=30))

    def test_unconfirmed_and_in_memory_success_never_grants(self):
        from rest_framework.exceptions import ValidationError
        from apps.patronage.fulfillment import fulfill
        purchase = self.purchase()
        attempt = self.prepare(purchase, status='unknown', evidence={})
        attempt.status = 'completed'
        with self.assertRaises(ValidationError):
            fulfill(attempt)
        self.assertFalse(CrownGrant.objects.exists())
        self.assertFalse(PatronageEarning.objects.exists())
        purchase.refresh_from_db()
        self.assertEqual(purchase.payment_status, 'created')

    def test_invalid_confirmation_time_rolls_back_real_finalize(self):
        from rest_framework.exceptions import ValidationError
        from apps.patronage.fulfillment import fulfill
        purchase = self.purchase()
        attempt = self.prepare(purchase)
        with self.assertRaises(ValidationError):
            spend_service.finalize(attempt.pk, apply=lambda a: fulfill(a, confirmed_at=datetime(2026, 1, 1)))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'succeeded')
        self.assertFalse(CrownGrant.objects.exists())
        self.assertFalse(PatronageEarning.objects.exists())

    def test_grant_or_income_failure_rolls_back_payment_and_extension(self):
        from unittest.mock import patch
        from apps.patronage.fulfillment import fulfill
        from apps.wallet.models import ClientWalletLedger
        at = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        first = self.fulfill(self.purchase(key='first'), at)
        next_purchase = self.purchase(key='second', package='week')
        attempt = self.prepare(next_purchase)
        balance = PlayerWallet.objects.get(player=self.player).available_balance
        for target in ('apps.patronage.fulfillment.CrownGrant.objects.create',
                       'apps.patronage.income.PatronageEarning.objects.create'):
            with self.subTest(target=target), patch(target, side_effect=RuntimeError('injected failure')):
                with self.assertRaises(RuntimeError):
                    spend_service.finalize(attempt.pk, apply=lambda a: fulfill(a, confirmed_at=at))
            next_purchase.refresh_from_db()
            attempt.refresh_from_db()
            self.assertEqual(next_purchase.payment_status, 'created')
            self.assertIsNone(next_purchase.starts_at)
            self.assertIsNone(next_purchase.expires_at)
            self.assertEqual(attempt.status, 'succeeded')
            self.assertEqual(CrownGrant.objects.count(), 1)
            self.assertEqual(PatronageEarning.objects.count(), 1)
            self.assertEqual(PlayerWallet.objects.get(player=self.player).available_balance, balance)
            self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)
        spend_service.finalize(attempt.pk, apply=lambda a: fulfill(a, confirmed_at=at))
        next_purchase.refresh_from_db()
        self.assertEqual(next_purchase.expires_at, first.expires_at+timedelta(days=7))

    def run_parallel(self, attempt_ids, callback):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections, connection
        from apps.wallet.models import WalletSpendAttempt
        barrier = Barrier(len(attempt_ids))
        def worker(pk):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                barrier.wait(timeout=10)
                return callback(WalletSpendAttempt.objects.get(pk=pk))
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=len(attempt_ids)) as executor:
            return list(executor.map(worker, attempt_ids))

    def test_parallel_replay_grants_and_credits_only_once(self):
        from apps.patronage.fulfillment import fulfill
        purchase = self.settled(self.purchase(package='week'))
        results = self.run_parallel([purchase.attempt_id]*3, lambda a: fulfill(a).pk)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(CrownGrant.objects.count(), 1)
        self.assertEqual(PatronageEarning.objects.count(), 1)
        grant = CrownGrant.objects.get()
        self.assertEqual(grant.expires_at, purchase.paid_at+timedelta(days=7))

    def test_parallel_distinct_purchases_do_not_lose_bonus_or_paid_extension(self):
        from apps.patronage.fulfillment import fulfill
        first = self.settled(self.purchase(key='first', package='month'))
        second = self.settled(self.purchase(key='second', package='day_pass'))
        # Trusted synthetic simultaneous confirmations; not a production write.
        PatronagePurchase.objects.filter(pk=second.pk).update(paid_at=first.paid_at)
        self.run_parallel([first.attempt_id, second.attempt_id], lambda a: fulfill(a).pk)
        grants = list(CrownGrant.objects.order_by('expires_at'))
        self.assertEqual(len(grants), 2)
        self.assertEqual(grants[-1].expires_at, first.paid_at+timedelta(days=37))
        self.assertEqual(grants[-1].extension_starts_at, grants[0].expires_at)
        self.assertEqual(PatronageEarning.objects.count(), 2)
        self.assertEqual(PlayerWallet.objects.get(player=self.player).available_balance, Decimal('1410')*2)

    def test_parallel_shared_finalize_and_income_replay_have_consistent_lock_order(self):
        from apps.patronage.fulfillment import fulfill
        at = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        first = self.fulfill(self.purchase(key='first', package='week'), at)
        second = self.purchase(key='second', package='month')
        attempt = self.prepare(second)
        def apply(current):
            if current.pk == attempt.pk:
                return spend_service.finalize(current.pk, apply=lambda a: fulfill(a, confirmed_at=at)).pk
            return self.credit(PatronagePurchase(pk=first.pk)).pk
        self.run_parallel([first.attempt_id, attempt.pk], apply)
        second.refresh_from_db()
        self.assertEqual(second.expires_at, first.expires_at+timedelta(days=30))
        self.assertEqual(CrownGrant.objects.count(), 2)
        self.assertEqual(PatronageEarning.objects.count(), 2)

    def test_confirmed_payment_atomically_fulfills_income_and_365_day_crown(self):
        self.assertIsNotNone(find_spec('apps.patronage.fulfillment'), 'Missing fulfillment service')
        at = datetime(2024, 2, 29, 12, tzinfo=dt_timezone.utc)
        purchase = self.fulfill(self.purchase(package='year'), at)
        grant = CrownGrant.objects.get(purchase=purchase)
        self.assertEqual(purchase.payment_status, 'paid')
        self.assertEqual(purchase.paid_at, at)
        self.assertEqual(grant.starts_at, at)
        self.assertEqual(grant.expires_at, at+timedelta(days=365))
        self.assertEqual(purchase.starts_at, grant.starts_at)
        self.assertEqual(purchase.expires_at, grant.expires_at)
        self.assertEqual(grant.source, 'purchase')
        self.assertEqual(PatronageEarning.objects.get().purchase_id, purchase.pk)
        self.assertEqual(PlayerWallet.objects.get(player=self.player).available_balance, Decimal('1410.00'))
