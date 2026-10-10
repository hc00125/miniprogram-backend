"""PostgreSQL two-connection serialization at the late commerce boundaries."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

from django.db import connections, transaction
from django.test import TransactionTestCase
from rest_framework.exceptions import ValidationError

from apps.players.archive import archive_players
from apps.players.models import PlayerServiceListing
from apps.players import test_archive_blockers as blockers


class DispatchArchiveLockTests(TransactionTestCase):
    setUp = blockers.FirstDispatchArchiveTests.setUp
    prepare = blockers.FirstDispatchArchiveTests.prepare

    def test_archive_first_blocks_dispatch_until_rejection(self):
        from apps.wallet.order_spend import authorize_dispatch
        attempt = self.prepare('target')
        started = Event()

        def authorize():
            try:
                with transaction.atomic():
                    started.set()
                    authorize_dispatch(attempt)
                return 'authorized'
            except ValidationError as exc:
                return str(exc.detail['code'])
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                archive_players([self.player.pk], actor=self.user, reason='lock test')
                future = pool.submit(authorize)
                self.assertTrue(started.wait(3))
                with self.assertRaises(TimeoutError):
                    future.result(timeout=0.2)
            self.assertEqual(future.result(timeout=5), 'PLAYER_ARCHIVED')

    def test_dispatch_authorization_holds_player_until_decision_commit(self):
        from apps.wallet.order_spend import authorize_dispatch
        attempt = self.prepare('target')
        started = Event()

        def archive():
            try:
                started.set()
                archive_players([self.player.pk], actor=self.user, reason='lock test')
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                authorize_dispatch(attempt)
                attempt.status = 'dispatching'
                attempt.save(update_fields=['status'])
                future = pool.submit(archive)
                self.assertTrue(started.wait(3))
                with self.assertRaises(TimeoutError):
                    future.result(timeout=0.2)
            future.result(timeout=5)
        self.player.refresh_from_db()
        attempt.refresh_from_db()
        self.assertTrue(self.player.is_archived)
        self.assertEqual(attempt.status, 'dispatching')


class ListingArchiveLockTests(TransactionTestCase):
    def setUp(self):
        from apps.orders.test_shared_player_service_listings import SharedPlayerServiceListingTests
        SharedPlayerServiceListingTests.setUp(self)
        self.listing = PlayerServiceListing.objects.create(player=self.player, spec=self.spec, status='approved')

    def test_archive_first_blocks_patch_then_rejects(self):
        started = Event()

        def patch_listing():
            from rest_framework.test import APIClient
            try:
                client = APIClient()
                client.force_authenticate(self.player.user)
                started.set()
                return client.patch('/api/player/service-listings/%s' % self.listing.pk,
                    {'is_available': True}, format='json').status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                archive_players([self.player.pk], actor=self.boss_user, reason='lock test')
                future = pool.submit(patch_listing)
                self.assertTrue(started.wait(3))
                with self.assertRaises(TimeoutError):
                    future.result(timeout=0.2)
            self.assertEqual(future.result(timeout=5), 400)
        self.listing.refresh_from_db()
        self.assertFalse(self.listing.is_available)

    def test_patch_first_keeps_player_lock_until_listing_commit(self):
        started = Event()

        def archive():
            try:
                started.set()
                archive_players([self.player.pk], actor=self.boss_user, reason='lock test')
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                response = self.player_client.patch('/api/player/service-listings/%s' % self.listing.pk,
                    {'is_available': True}, format='json')
                self.assertEqual(response.status_code, 200)
                future = pool.submit(archive)
                self.assertTrue(started.wait(3))
                with self.assertRaises(TimeoutError):
                    future.result(timeout=0.2)
            future.result(timeout=5)
        self.listing.refresh_from_db()
        self.assertFalse(self.listing.is_available)
