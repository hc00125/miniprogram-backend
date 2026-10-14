from decimal import Decimal
from unittest.mock import patch
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.test import TestCase, RequestFactory
from apps.accounts.models import ClientProfile
from apps.players.models import Player, PlayerApplication
from apps.players.approval import approve_player_application
from apps.gifts.models import Gift, PlayerGiftConfig, PlayerGiftConfigAudit
from apps.gifts.admin import GiftAdmin
from apps.gifts.services.configuration import update_config
from apps.gifts.tests.test_images import image_file
from apps.gifts.tests import test_auto_open as fixtures


class AutoOpenBoundaryTests(TestCase):
    setUp = fixtures.AutoOpenTests.setUp

    def gift(self, **kwargs):
        return Gift.objects.create(name='edge', image=image_file(), price_diamonds=10, is_active=True, **kwargs)

    def operator(self):
        return get_user_model().objects.create_superuser('edge-admin', 'edge@example.test', 'unused')

    def test_zero_special_rates_and_disabled_are_never_overwritten(self):
        actor = self.operator()
        for rate, enabled in [('0.00', True), ('12.34', True), ('0.00', False), ('37.00', False)]:
            gift = self.gift()
            config = PlayerGiftConfig.objects.get(gift=gift, player=self.player)
            update_config(actor=actor, config_id=config.pk, commission_rate=Decimal(rate), is_enabled=enabled,
                          reason='manual override', expected_version=1)
            before = list(PlayerGiftConfig.objects.filter(pk=config.pk).values())
            audits = list(config.audits.values())
            gift.save()
            gift.is_active = False
            gift.save(update_fields=['is_active'])
            gift.is_active = True
            gift.save(update_fields=['is_active'])
            self.player.save()
            self.assertEqual(list(PlayerGiftConfig.objects.filter(pk=config.pk).values()), before)
            self.assertEqual(list(config.audits.values()), audits)

    def test_manual_gift_send_switch_survives_edit_until_relisting(self):
        gift = self.gift()
        gift.send_enabled = False
        gift.save(update_fields=['send_enabled'])
        gift.name = 'edited'
        gift.save()
        gift.refresh_from_db()
        self.assertFalse(gift.send_enabled)
        gift.is_active = False
        gift.save(update_fields=['is_active'])
        gift.is_active = True
        gift.save(update_fields=['is_active'])
        gift.refresh_from_db()
        self.assertTrue(gift.send_enabled)
        self.assertEqual(PlayerGiftConfigAudit.objects.count(), 1)

    def test_partial_save_does_not_accidentally_list(self):
        gift = Gift.objects.create(name='partial', price_diamonds=1, image=image_file())
        gift.is_active = True
        gift.name = 'only-name'
        gift.save(update_fields=['name'])
        gift.refresh_from_db()
        self.assertFalse(gift.is_active)
        self.assertFalse(gift.send_enabled)
        self.assertFalse(PlayerGiftConfig.objects.exists())

    def test_listing_audit_failure_rolls_back_gift_and_configs(self):
        gift = Gift.objects.create(name='rollback', price_diamonds=1, image=image_file())
        with patch('apps.gifts.services.configuration.PlayerGiftConfigAudit.objects.create', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                gift.is_active = True
                gift.save(update_fields=['is_active'])
        gift.refresh_from_db()
        self.assertFalse(gift.is_active)
        self.assertFalse(gift.send_enabled)
        self.assertFalse(PlayerGiftConfig.objects.exists())

    def test_outer_transaction_rolls_back_listing_audits(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.gift()
            raise RuntimeError('outer rollback')
        self.assertFalse(Gift.objects.exists())
        self.assertFalse(PlayerGiftConfigAudit.objects.exists())

    def test_approval_audit_failure_rolls_back_real_approval(self):
        self.player.status = 'pending'
        self.player.save(update_fields=['status'])
        self.gift()
        application = PlayerApplication.objects.create(user=self.user, name=self.player.name, player_type=self.kind)
        with patch('apps.gifts.services.configuration.PlayerGiftConfigAudit.objects.create', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                approve_player_application(application.pk, self.operator())
        self.player.refresh_from_db()
        application.refresh_from_db()
        self.assertEqual(self.player.status, 'pending')
        self.assertEqual(application.status, 'pending')
        self.assertFalse(PlayerGiftConfig.objects.exists())

    def test_eligibility_matrix_and_public_restoration(self):
        from apps.common.player_model_version import archive_filter
        actor = self.operator()
        self.player.is_publicly_visible = False
        self.player.save(update_fields=['is_publicly_visible'])
        gift = self.gift()
        self.assertFalse(PlayerGiftConfig.objects.exists())
        self.player.is_publicly_visible = True
        self.player.save(update_fields=['is_publicly_visible'])
        self.assertTrue(PlayerGiftConfig.objects.filter(gift=gift).exists())
        if archive_filter(Player):
            from apps.players.archive import archive_players, restore_players
            archive_players([self.player.pk], actor=actor, reason='test')
            archived_gift = self.gift()
            self.assertFalse(PlayerGiftConfig.objects.filter(gift=archived_gift).exists())
            restore_players([self.player.pk], actor=actor, reason='test')
            self.assertFalse(PlayerGiftConfig.objects.filter(gift=archived_gift).exists())
            self.player.refresh_from_db()
            self.player.is_publicly_visible = True
            self.player.save(update_fields=['is_publicly_visible'])
            self.assertTrue(PlayerGiftConfig.objects.filter(gift=archived_gift).exists())
        for state in ['pending', 'rejected', 'disabled']:
            self.player.status = state
            self.player.save(update_fields=['status'])
            blocked = self.gift()
            self.assertFalse(PlayerGiftConfig.objects.filter(gift=blocked).exists())
        self.player.status = 'approved'
        self.player.save(update_fields=['status'])
        self.assertEqual(PlayerGiftConfig.objects.count(), Gift.objects.count())
        self.assertFalse(self.player.is_online)  # Offline/resting is eligible.

    def test_internal_and_unlisted_never_auto_created(self):
        hidden = Gift.objects.create(name='hidden', price_diamonds=1)
        internal = Gift.objects.create(name='internal', kind='internal_surcharge', code='order_surcharge', price_diamonds=None)
        self.player.save()
        self.assertFalse(PlayerGiftConfig.objects.filter(gift__in=[hidden, internal]).exists())

    def test_batch_invalid_item_rolls_back_entire_batch(self):
        first = Gift.objects.create(name='valid', price_diamonds=1, image=image_file())
        invalid = Gift.objects.create(name='invalid', price_diamonds=1)
        request = RequestFactory().post('/admin/gifts/gift/')
        request.user = self.operator()
        with self.assertRaises(ValidationError):
            GiftAdmin(Gift, admin.site).publish_selected(request, Gift.objects.filter(pk__in=[first.pk, invalid.pk]))
        self.assertFalse(Gift.objects.filter(is_active=True).exists())
        self.assertFalse(PlayerGiftConfig.objects.exists())

    def test_batch_cannot_be_invoked_by_unprivileged_user(self):
        gift = Gift.objects.create(name='valid', price_diamonds=1, image=image_file())
        request = RequestFactory().post('/admin/gifts/gift/')
        request.user = self.user
        with self.assertRaises(PermissionDenied):
            GiftAdmin(Gift, admin.site).publish_selected(request, Gift.objects.filter(pk=gift.pk))
        gift.refresh_from_db()
        self.assertFalse(gift.is_active)

    def test_self_gifting_and_recipient_allowlist_still_rejected(self):
        from apps.gifts.services.purchases import recipient_for
        from rest_framework.exceptions import ValidationError as APIError
        self.gift()
        with self.assertRaises(APIError):
            recipient_for(self.user, self.player.pk, {'recipient_ids': None})
        with self.assertRaises(APIError):
            recipient_for(self.operator(), self.player.pk, {'recipient_ids': []})

    def test_repeat_approval_and_saves_have_no_duplicate_audit(self):
        self.gift()
        application = PlayerApplication.objects.create(user=self.user, name=self.player.name, player_type=self.kind)
        actor = self.operator()
        for _ in range(2):
            approve_player_application(application.pk, actor)
            self.player.refresh_from_db()
            self.player.save()
        self.assertEqual(PlayerGiftConfig.objects.count(), 1)
        self.assertEqual(PlayerGiftConfigAudit.objects.count(), 1)

    def test_profile_change_rolls_back_when_auto_audit_fails(self):
        profile = ClientProfile.objects.get(user=self.user)
        profile.account_status = 'banned'
        profile.save(update_fields=['account_status'])
        self.gift()
        with patch('apps.gifts.services.configuration.PlayerGiftConfigAudit.objects.create', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                profile.account_status = 'active'
                profile.save(update_fields=['account_status'])
        profile.refresh_from_db()
        self.assertEqual(profile.account_status, 'banned')

    def test_expired_suspension_real_service_backfills(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.accounts.access import refresh_expired_account_restriction
        profile = ClientProfile.objects.get(user=self.user)
        profile.account_status = 'suspended'
        profile.account_suspended_until = timezone.now() - timedelta(seconds=1)
        profile.save(update_fields=['account_status', 'account_suspended_until'])
        gift = self.gift()
        refresh_expired_account_restriction(profile)
        self.assertTrue(PlayerGiftConfig.objects.filter(player=self.player, gift=gift).exists())

    def test_admin_profile_restore_backfills(self):
        from apps.accounts.admin import ClientProfileAdmin
        profile = ClientProfile.objects.get(user=self.user)
        profile.account_status = 'banned'
        profile.save(update_fields=['account_status'])
        gift = self.gift()
        profile.account_status = 'active'
        request = RequestFactory().post('/admin/accounts/clientprofile/')
        request.user = self.operator()
        ClientProfileAdmin(ClientProfile, admin.site).save_model(request, profile, None, True)
        self.assertTrue(PlayerGiftConfig.objects.filter(player=self.player, gift=gift).exists())

    def test_admin_single_application_save_backfills(self):
        from types import SimpleNamespace
        from apps.players.admin import PlayerApplicationAdmin
        self.player.status = 'pending'
        self.player.save(update_fields=['status'])
        gift = self.gift()
        application = PlayerApplication.objects.create(user=self.user, name=self.player.name, player_type=self.kind)
        application.status = 'approved'
        request = RequestFactory().post('/admin/players/playerapplication/')
        request.user = self.operator()
        PlayerApplicationAdmin(PlayerApplication, admin.site).save_model(
            request, application, SimpleNamespace(changed_data=['status']), True)
        self.assertTrue(PlayerGiftConfig.objects.filter(player=self.player, gift=gift).exists())

    def test_admin_api_approval_backfills(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from apps.admin_api.views import approve_application
        self.player.status = 'pending'
        self.player.save(update_fields=['status'])
        gift = self.gift()
        application = PlayerApplication.objects.create(user=self.user, name=self.player.name, player_type=self.kind)
        request = APIRequestFactory().post('/api/admin/player-applications/approve', {}, format='json')
        force_authenticate(request, user=self.operator())
        response = approve_application(request, application_id=application.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(PlayerGiftConfig.objects.filter(player=self.player, gift=gift).exists())

    def test_registered_user_admin_reactivation_is_atomic(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.gift()
        request = RequestFactory().post('/admin/auth/user/')
        request.user = self.operator()
        with patch('apps.gifts.services.configuration.PlayerGiftConfigAudit.objects.create', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                self.user.is_active = True
                admin.site._registry[get_user_model()].save_model(request, self.user, None, True)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_signal_sync_failure_is_fail_closed_not_fallback_rate(self):
        from apps.gifts.services.configuration import resolve_commission
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        gift = self.gift()
        with patch('apps.gifts.services.configuration.PlayerGiftConfigAudit.objects.create', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                self.user.is_active = True
                self.user.save(update_fields=['is_active'])
        with self.assertRaises(ValidationError):
            resolve_commission(self.player.pk, gift.pk)
