from django.contrib import admin
from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from .models import SupportChannel


class SupportCenterApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_seeded_official_customer_service_is_enabled(self):
        response = self.client.get('/api/support/customer-service/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['official_customer_service_enabled'])
        self.assertEqual(response.data['contacts'], [])

    def test_active_personal_contacts_are_sorted_and_filtered_by_audience(self):
        SupportChannel.objects.create(
            name='全部用户客服',
            channel_type=SupportChannel.TYPE_WECHAT_PERSONAL,
            wechat_id='TC-ALL',
            audience=SupportChannel.AUDIENCE_ALL,
            sort_order=20,
            is_active=True,
        )
        SupportChannel.objects.create(
            name='老板客服',
            channel_type=SupportChannel.TYPE_WECHAT_PERSONAL,
            wechat_id='TC-BOSS',
            audience=SupportChannel.AUDIENCE_BOSS,
            sort_order=10,
            is_active=True,
        )
        SupportChannel.objects.create(
            name='陪玩客服',
            channel_type=SupportChannel.TYPE_WECHAT_PERSONAL,
            wechat_id='TC-PLAYER',
            audience=SupportChannel.AUDIENCE_PLAYER,
            sort_order=5,
            is_active=True,
        )
        SupportChannel.objects.create(
            name='停用客服',
            channel_type=SupportChannel.TYPE_WECHAT_PERSONAL,
            wechat_id='TC-OFF',
            audience=SupportChannel.AUDIENCE_ALL,
            sort_order=0,
            is_active=False,
        )

        boss_response = self.client.get('/api/support/customer-service/?audience=boss')
        self.assertEqual(boss_response.status_code, 200)
        self.assertEqual(
            [item['wechat_id'] for item in boss_response.data['contacts']],
            ['TC-BOSS', 'TC-ALL'],
        )

        player_response = self.client.get('/api/support/customer-service/?audience=player')
        self.assertEqual(
            [item['wechat_id'] for item in player_response.data['contacts']],
            ['TC-PLAYER', 'TC-ALL'],
        )

    def test_inactive_official_channel_hides_wechat_contact_button(self):
        SupportChannel.objects.filter(
            channel_type=SupportChannel.TYPE_WECHAT_OFFICIAL,
        ).update(is_active=False)
        response = self.client.get('/api/support/customer-service/')
        self.assertFalse(response.data['official_customer_service_enabled'])


class SupportChannelAdminTests(TestCase):
    def test_only_superuser_can_manage_support_channels_and_delete_is_disabled(self):
        model_admin = admin.site._registry[SupportChannel]
        superuser = User.objects.create_superuser(
            username='support-admin',
            email='support@example.com',
            password='password',
        )
        normal_user = User.objects.create_user(username='normal-user', is_staff=True)

        class Request:
            pass

        request = Request()
        request.user = superuser
        self.assertTrue(model_admin.has_module_permission(request))
        self.assertTrue(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))

        request.user = normal_user
        self.assertFalse(model_admin.has_module_permission(request))
        self.assertFalse(model_admin.has_add_permission(request))
