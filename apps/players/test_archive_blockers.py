"""Archive/payment boundaries reproduced independently; all transports offline."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Package, PlayerType
from apps.orders.models import Order, OrderDesignation
from apps.payments.models import Payment
from apps.payments.services import mark_payment_paid
from apps.players.archive import archive_players, restore_players
from apps.players.models import Player, PlayerServiceListing


class ConfirmedTargetArchiveTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('archive-blocker-boss')
        self.player = Player.objects.create(name='archive-blocker-player',
            player_type=PlayerType.objects.create(name='archive-blocker-type', priority=1), is_online=True)
        self.package = Package.objects.create(name='archive-blocker-package', base_price=6, player_count=1)
        self.order = Order.objects.create(order_no='ARCHIVE-PAID', package=self.package,
            boss_user=self.user, boss_wechat='offline', required_players=1,
            total_price_per_hour=6, total_amount=6, target_player=self.player,
            fulfillment_mode=Order.FULFILLMENT_MODE_TARGETED, status=Order.STATUS_PENDING_PAYMENT)
        self.payment = Payment.objects.create(payment_no='ARCHIVE-PAY', order=self.order,
            channel='wechat_virtual', scene='short_series_goods', amount=6, status='paying')

    def test_confirmed_payment_finishes_once_without_reopening_player(self):
        archive_players([self.player.pk], actor=self.user, reason='offline test')
        with patch('apps.orders.targeted_notifications.notify_paid_targeted_order') as notify:
            with self.captureOnCommitCallbacks(execute=True):
                mark_payment_paid(self.payment, third_trade_no='offline-confirmed')
                mark_payment_paid(self.payment, third_trade_no='offline-confirmed')
            self.assertEqual(notify.call_count, 1)
        self.payment.refresh_from_db()
        self.order.refresh_from_db()
        self.player.refresh_from_db()
        self.assertEqual(self.payment.status, 'paid')
        self.assertTrue(self.order.paid)
        self.assertEqual(self.order.status, Order.STATUS_WAITING)
        self.assertEqual(self.order.designations.count(), 1)
        invitation = self.order.designations.get()
        self.assertEqual(invitation.player_id, self.player.pk)
        self.assertEqual(invitation.status, OrderDesignation.STATUS_PENDING)
        self.assertFalse(self.player.can_accept_orders)
        with self.assertRaises(ValidationError), transaction.atomic():
            invitation.status = OrderDesignation.STATUS_ACCEPTED
            invitation.save()

    def test_archive_still_rejects_unconfirmed_new_invitation(self):
        from apps.orders.designations import create_targeted_designation
        archive_players([self.player.pk], actor=self.user, reason='offline test')
        with self.assertRaises(ValidationError), transaction.atomic():
            create_targeted_designation(self.order)
        self.assertFalse(self.order.designations.exists())


class FirstDispatchArchiveTests(TransactionTestCase):
    def setUp(self):
        from apps.accounts.models import ClientProfile
        from apps.wallet.models import ClientWallet
        self.user = get_user_model().objects.create_user('archive-dispatch-boss')
        self.profile = ClientProfile.objects.create(user=self.user, openid='offline-archive-dispatch')
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile, defaults={'balance': Decimal('100')})
        self.player = Player.objects.create(name='dispatch-player',
            player_type=PlayerType.objects.create(name='dispatch-type', priority=1))
        self.package = Package.objects.create(name='dispatch-package', base_price=6, player_count=1)

    def prepare(self, mode):
        from apps.wallet.order_spend import prepare
        values = {}
        if mode == 'target':
            values = {'target_player': self.player, 'fulfillment_mode': Order.FULFILLMENT_MODE_TARGETED}
        elif mode == 'snapshot':
            values = {'designated_players': [self.player.pk]}
        self.order = Order.objects.create(order_no='DISPATCH-' + mode, package=self.package,
            boss_user=self.user, required_players=1, total_price_per_hour=6,
            total_amount=6, status=Order.STATUS_PENDING_PAYMENT, **values)
        if mode == 'invitation':
            OrderDesignation.objects.create(order=self.order, player=self.player,
                expires_at=timezone.now() + timedelta(minutes=10))
        return prepare(self.order.order_no, self.user)

    def assert_archived_dispatch_rejected(self, mode):
        from apps.wallet.order_spend import authorize_dispatch
        attempt = self.prepare(mode)
        archive_players([self.player.pk], actor=self.user, reason='offline archive')
        with self.assertRaises(ValidationError), transaction.atomic():
            authorize_dispatch(attempt)

    def test_first_dispatch_rejects_archived_target(self):
        self.assert_archived_dispatch_rejected('target')

    def test_first_dispatch_rejects_archived_snapshot_player(self):
        self.assert_archived_dispatch_rejected('snapshot')

    def test_first_dispatch_rejects_archived_invited_player(self):
        self.assert_archived_dispatch_rejected('invitation')

    def test_rejected_dispatch_releases_reservation_without_capture(self):
        from apps.wallet import spend_service
        from unittest.mock import Mock
        attempt = self.prepare('target')
        archive_players([self.player.pk], actor=self.user, reason='offline archive')
        adapter = Mock()
        with self.assertRaises(ValidationError):
            spend_service.execute(attempt.pk, adapter=adapter)
        attempt.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(attempt.status, 'failed')
        self.assertEqual(attempt.reserved_amount, Decimal('0'))
        self.assertEqual(self.wallet.balance, Decimal('100'))
        adapter.spend.assert_not_called()
        self.assertFalse(Payment.objects.filter(order=self.order).exists())

    def test_unknown_confirmed_recovery_finishes_without_new_authorization(self):
        from apps.wallet import spend_service
        from unittest.mock import Mock
        attempt = self.prepare('target')
        attempt.status = 'unknown'
        attempt.save(update_fields=['status'])
        archive_players([self.player.pk], actor=self.user, reason='offline archive')
        adapter = Mock()
        adapter.query.return_value = {'order_id': attempt.external_id,
            'amount': attempt.source_snapshot['coin_units'],
            'openid': attempt.request_payload['openid'], 'env': attempt.request_payload['env'],
            'status': 'succeeded'}
        with patch('apps.orders.targeted_notifications.notify_paid_targeted_order'):
            spend_service.recover(attempt.pk, adapter=adapter)
            spend_service.recover(attempt.pk, adapter=adapter)
        self.order.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertTrue(self.order.paid)
        self.assertEqual(self.wallet.balance, Decimal('94'))
        self.assertEqual(self.order.designations.count(), 1)
        adapter.spend.assert_not_called()


@override_settings(PLAYER_SERVICE_LISTING_AUTO_APPROVE=False)
class ListingArchiveWriteTests(TestCase):
    def setUp(self):
        from apps.orders.test_shared_player_service_listings import SharedPlayerServiceListingTests
        SharedPlayerServiceListingTests.setUp(self)

    def archive(self):
        archive_players([self.player.pk], actor=self.boss_user, reason='offline listing test')

    def test_archived_cannot_submit_pending_listing(self):
        self.archive()
        response = self.player_client.post('/api/player/service-listings', {'spec_id': self.spec.pk}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PlayerServiceListing.objects.filter(player=self.player).exists())

    def test_archived_cannot_post_resubmit_listing(self):
        listing = PlayerServiceListing.objects.create(player=self.player, spec=self.spec, status='offline')
        self.archive()
        response = self.player_client.post('/api/player/service-listings', {'spec_id': self.spec.pk}, format='json')
        self.assertEqual(response.status_code, 400)
        listing.refresh_from_db()
        self.assertEqual(listing.status, 'offline')
        self.assertFalse(listing.is_available)

    def test_archived_cannot_patch_restore_resubmit_or_availability(self):
        listing = PlayerServiceListing.objects.create(player=self.player, spec=self.spec, status='approved')
        self.archive()
        for payload in ({'is_available': True}, {'action': 'restore'}, {'action': 'resubmit'}):
            with self.subTest(payload=payload):
                response = self.player_client.patch('/api/player/service-listings/%s' % listing.pk, payload, format='json')
                self.assertEqual(response.status_code, 400)
                listing.refresh_from_db()
                self.assertFalse(listing.is_available)
                self.assertEqual(listing.status, 'approved')

    def test_archived_retains_listing_read_access(self):
        PlayerServiceListing.objects.create(player=self.player, spec=self.spec, status='approved')
        self.archive()
        response = self.player_client.get('/api/player/service-listings')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['listings']), 1)


class StaleRestoreSaveTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('stale-save-boss')
        self.player = Player.objects.create(name='stale-save-player', is_online=True,
            player_type=PlayerType.objects.create(name='stale-save-type', priority=1),
            presence_seen_at=timezone.now())

    def cycle(self):
        archive_players([self.player.pk], actor=self.user, reason='offline archive')
        restore_players([self.player.pk], actor=self.user, reason='offline restore')

    def assert_closed(self, player):
        player.refresh_from_db()
        for field in ('is_online', 'can_accept_orders', 'can_be_designated', 'is_publicly_visible'):
            self.assertFalse(getattr(player, field), field)
        self.assertIsNone(player.presence_seen_at)

    def test_prearchive_full_save_only_merges_dirty_profile_fields(self):
        stale = Player.objects.get(pk=self.player.pk)
        self.cycle()
        Player.objects.filter(pk=self.player.pk).update(total_orders=17, contact_wechat='new-contact')
        stale.bio = 'delayed bio'
        stale.save()
        stale.audio_intro_title = 'second delayed edit'
        stale.save()
        self.assert_closed(stale)
        self.assertEqual(stale.bio, 'delayed bio')
        self.assertEqual(stale.total_orders, 17)
        self.assertEqual(stale.contact_wechat, 'new-contact')

    def test_unsaved_profile_edit_survives_an_unrelated_partial_save(self):
        stale = Player.objects.get(pk=self.player.pk)
        stale.bio = 'edit still awaiting save'
        stale.save(update_fields=['contact_wechat'])
        self.cycle()
        stale.save()
        self.assert_closed(stale)
        self.assertEqual(stale.bio, 'edit still awaiting save')

    def test_stale_explicit_flags_do_not_reopen_after_restore(self):
        stale = Player.objects.get(pk=self.player.pk)
        self.cycle()
        stale.bio = 'explicit delayed bio'
        stale.save(update_fields=['bio', 'is_online', 'can_accept_orders', 'can_be_designated', 'is_publicly_visible', 'presence_seen_at'])
        self.assert_closed(stale)
        self.assertEqual(stale.bio, 'explicit delayed bio')

    def test_repeated_archive_cycles_fence_previously_restored_instance(self):
        self.cycle()
        stale = Player.objects.get(pk=self.player.pk)
        stale.is_online = stale.can_accept_orders = stale.can_be_designated = stale.is_publicly_visible = True
        stale.save()
        self.cycle()
        stale.bio = 'old generation'
        stale.save()
        self.assert_closed(stale)

    def test_fresh_restored_instance_can_explicitly_reenable(self):
        self.cycle()
        self.player.refresh_from_db()
        self.player.is_online = True
        self.player.can_accept_orders = True
        self.player.save(update_fields=['is_online', 'can_accept_orders'])
        self.player.refresh_from_db()
        self.assertTrue(self.player.is_online)
        self.assertTrue(self.player.can_accept_orders)

    def test_deferred_profile_save_does_not_clobber_restored_flags(self):
        stale = Player.objects.only('id', 'bio').get(pk=self.player.pk)
        self.cycle()
        stale.bio = 'deferred profile'
        stale.save()
        self.assert_closed(stale)
        self.assertEqual(stale.bio, 'deferred profile')


class ArchiveEpochClockTests(TestCase):
    setUp = StaleRestoreSaveTests.setUp
    cycle = StaleRestoreSaveTests.cycle
    assert_closed = StaleRestoreSaveTests.assert_closed
    # Test the extra ABA edge without inheriting the rest of the suite.
    def test_same_clock_value_still_fences_distinct_archive_cycles(self):
        fixed = timezone.now()
        with patch('apps.players.archive.timezone.now', return_value=fixed):
            StaleRestoreSaveTests.test_repeated_archive_cycles_fence_previously_restored_instance(self)
