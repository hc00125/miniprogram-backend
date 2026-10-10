from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.db import connection, connections, close_old_connections
from django.test import TransactionTestCase
from apps.gifts.models import Gift, PlayerGiftConfig, PlayerGiftConfigAudit
from apps.players.models import Player
from apps.gifts.tests.test_images import image_file
from apps.gifts.tests import test_auto_open as fixtures


class AutoOpenConcurrencyTests(TransactionTestCase):
    setUp = fixtures.AutoOpenTests.setUp

    def parallel(self, *jobs):
        self.assertEqual(connection.vendor, 'postgresql')
        barrier = Barrier(len(jobs))
        def run(job):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout='5s'")
                    cursor.execute("SET statement_timeout='15s'")
                barrier.wait(timeout=10)
                job()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(run, job) for job in jobs]
            for future in futures:
                future.result(timeout=25)

    def test_concurrent_listing_and_approval_do_not_miss_cross_product(self):
        self.player.status = 'pending'
        self.player.save(update_fields=['status'])
        gift = Gift.objects.create(name='concurrent', image=image_file(), price_diamonds=10)
        def listing():
            obj = Gift.objects.get(pk=gift.pk)
            obj.is_active = True
            obj.save(update_fields=['is_active'])
        def approval():
            obj = Player.objects.get(pk=self.player.pk)
            obj.status = 'approved'
            obj.save(update_fields=['status'])
        self.parallel(listing, approval)
        self.assertEqual(PlayerGiftConfig.objects.count(), 1)
        self.assertEqual(PlayerGiftConfigAudit.objects.count(), 1)

    def test_simultaneous_relisting_has_one_config_and_audit(self):
        gift = Gift.objects.create(name='duplicate', image=image_file(), price_diamonds=10)
        def listing():
            obj = Gift.objects.get(pk=gift.pk)
            obj.is_active = True
            obj.save(update_fields=['is_active'])
        self.parallel(listing, listing)
        self.assertEqual(PlayerGiftConfig.objects.count(), 1)
        self.assertEqual(PlayerGiftConfigAudit.objects.count(), 1)
        self.assertEqual(PlayerGiftConfig.objects.get().commission_rate, Decimal('25.00'))

    def test_listing_and_account_reactivation(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        gift = Gift.objects.create(name='reactivating', image=image_file(), price_diamonds=10)
        def listing():
            obj = Gift.objects.get(pk=gift.pk)
            obj.is_active = True
            obj.save(update_fields=['is_active'])
        def activate():
            user = get_user_model().objects.get(pk=self.user.pk)
            user.is_active = True
            user.save(update_fields=['is_active'])
        self.parallel(listing, activate)
        self.assertEqual(PlayerGiftConfig.objects.count(), 1)
        self.assertEqual(PlayerGiftConfigAudit.objects.count(), 1)

    def test_expiry_restore_and_approval_use_same_lock_order(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.accounts.models import ClientProfile
        from apps.accounts.access import refresh_expired_account_restriction
        from apps.players.models import PlayerApplication
        from apps.players.approval import approve_player_application
        profile = ClientProfile.objects.get(user=self.user)
        profile.account_status = 'suspended'
        profile.account_suspended_until = timezone.now() - timedelta(seconds=1)
        profile.save(update_fields=['account_status', 'account_suspended_until'])
        self.player.status = 'pending'
        self.player.save(update_fields=['status'])
        Gift.objects.create(name='expiry-race', image=image_file(), price_diamonds=10, is_active=True)
        application = PlayerApplication.objects.create(user=self.user, name=self.player.name, player_type=self.kind)
        actor = get_user_model().objects.create_superuser('expiry-admin', 'expiry@example.test', 'unused')
        self.parallel(lambda: refresh_expired_account_restriction(ClientProfile.objects.get(pk=profile.pk)),
                      lambda: approve_player_application(application.pk, actor))
        self.assertEqual(PlayerGiftConfig.objects.count(), 1)
        self.assertEqual(PlayerGiftConfigAudit.objects.count(), 1)

    def test_listing_and_manual_initializer_share_lock_order(self):
        from apps.gifts.services.configuration import initialize_configs
        gift = Gift.objects.create(name='manual-race', image=image_file(), price_diamonds=10)
        actor = get_user_model().objects.create_superuser('race-admin', 'race@example.test', 'unused')
        def listing():
            obj = Gift.objects.get(pk=gift.pk)
            obj.is_active = True
            obj.save(update_fields=['is_active'])
        def manual():
            initialize_configs(actor=actor, players=[self.player], gifts=[gift], reason='manual initialization')
        self.parallel(listing, manual)
        self.assertEqual(PlayerGiftConfig.objects.count(), 1)
        self.assertEqual(PlayerGiftConfigAudit.objects.count(), 1)
