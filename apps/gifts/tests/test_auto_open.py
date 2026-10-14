from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.gifts.models import Gift, PlayerGiftConfig
from apps.gifts.tests.test_images import image_file


class AutoOpenTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='auto-player')
        ClientProfile.objects.get_or_create(user=self.user)
        self.kind = PlayerType.objects.create(name='auto-kind', priority=1)
        self.player = Player.objects.create(user=self.user, name='auto-player', player_type=self.kind)

    def test_approval_after_listing_through_real_admin_batch(self):
        from django.contrib import admin
        from django.test import RequestFactory
        from unittest.mock import patch
        from apps.players.models import PlayerApplication
        from apps.players.admin import PlayerApplicationAdmin
        gift = Gift.objects.create(name='before-approval', image=image_file(), price_diamonds=10, is_active=True)
        other = get_user_model().objects.create_user(username='pending')
        ClientProfile.objects.create(user=other, openid='pending', nickname='pending')
        application = PlayerApplication.objects.create(user=other, name='pending', player_type=self.kind)
        operator = get_user_model().objects.create_superuser('operator', 'operator@example.test', 'unused')
        request = RequestFactory().post('/admin/players/playerapplication/')
        request.user = operator
        model_admin = PlayerApplicationAdmin(PlayerApplication, admin.site)
        with patch.object(model_admin, 'message_user'):
            model_admin.approve_applications(request, PlayerApplication.objects.filter(pk=application.pk))
        player = Player.objects.get(user=other)
        self.assertTrue(PlayerGiftConfig.objects.filter(player=player, gift=gift, is_enabled=True).exists())

    def test_account_reactivation_and_late_profile_create(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        gift = Gift.objects.create(name='reactivate', image=image_file(), price_diamonds=10, is_active=True)
        self.assertFalse(PlayerGiftConfig.objects.filter(gift=gift).exists())
        self.user.is_active = True
        self.user.save(update_fields=['is_active'])
        self.assertTrue(PlayerGiftConfig.objects.filter(gift=gift, player=self.player).exists())
        other = get_user_model().objects.create_user(username='late-profile')
        player = Player.objects.create(user=other, name='late-profile', player_type=self.kind)
        self.assertFalse(PlayerGiftConfig.objects.filter(gift=gift, player=player).exists())
        ClientProfile.objects.create(user=other, openid='late-profile', nickname='late-profile')
        self.assertTrue(PlayerGiftConfig.objects.filter(gift=gift, player=player).exists())

    def test_profile_reactivation(self):
        profile = ClientProfile.objects.get(user=self.user)
        profile.account_status = 'banned'
        profile.save(update_fields=['account_status'])
        gift = Gift.objects.create(name='profile', image=image_file(), price_diamonds=10, is_active=True)
        self.assertFalse(PlayerGiftConfig.objects.filter(gift=gift).exists())
        profile.account_status = 'active'
        profile.save(update_fields=['account_status'])
        self.assertTrue(PlayerGiftConfig.objects.filter(gift=gift, player=self.player).exists())

    def test_admin_batch_listing_uses_validated_atomic_path(self):
        from django.contrib import admin
        from apps.gifts.admin import GiftAdmin
        first = Gift.objects.create(name='batch-one', image=image_file(), price_diamonds=10)
        second = Gift.objects.create(name='batch-two', image=image_file(), price_diamonds=20)
        from django.test import RequestFactory
        request = RequestFactory().post('/admin/gifts/gift/')
        request.user = get_user_model().objects.create_superuser('batch-admin', 'batch@example.test', 'unused')
        GiftAdmin(Gift, admin.site).publish_selected(request, Gift.objects.filter(pk__in=[first.pk, second.pk]))
        self.assertEqual(PlayerGiftConfig.objects.filter(player=self.player).count(), 2)
        self.assertFalse(Gift.objects.filter(pk__in=[first.pk, second.pk], send_enabled=False).exists())

    def test_listing_real_save_opens_reception_atomically(self):
        gift = Gift.objects.create(name='auto-gift', image=image_file(), price_diamonds=10, is_active=True)
        gift.refresh_from_db()
        self.assertTrue(gift.send_enabled)
        config = PlayerGiftConfig.objects.get(player=self.player, gift=gift)
        self.assertEqual(config.commission_rate, Decimal('25.00'))
        self.assertTrue(config.is_enabled)
        self.assertEqual(config.audits.count(), 1)
        self.assertIsNone(config.audits.get().actor_id)
        self.assertIn('system:gift-auto-open:v1', config.audits.get().reason)
