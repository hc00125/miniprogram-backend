from unittest.mock import Mock

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from apps.accounts.models import ClientProfile

from .admin import PlayerApplicationAdmin
from .models import PlayerApplication


class PlayerApplicationAdminTests(TestCase):
    def test_reject_applications_syncs_client_profile_status(self):
        admin_user = User.objects.create_superuser(
            username='admin',
            email='admin@example.com',
            password='password',
        )
        applicant = User.objects.create_user(
            username='applicant',
            password='password',
        )
        profile = ClientProfile.objects.create(
            user=applicant,
            openid='test-openid',
            nickname='测试用户',
            player_status=ClientProfile.PLAYER_STATUS_PENDING,
        )
        application = PlayerApplication.objects.create(
            user=applicant,
            name='测试陪玩',
            contact_wechat='test-wechat',
            status=PlayerApplication.STATUS_PENDING,
        )

        request = RequestFactory().post('/admin/players/playerapplication/')
        request.user = admin_user
        model_admin = PlayerApplicationAdmin(PlayerApplication, admin.site)
        model_admin.message_user = Mock()

        model_admin.reject_applications(
            request,
            PlayerApplication.objects.filter(pk=application.pk),
        )

        application.refresh_from_db()
        profile.refresh_from_db()

        self.assertEqual(application.status, PlayerApplication.STATUS_REJECTED)
        self.assertEqual(profile.player_status, ClientProfile.PLAYER_STATUS_REJECTED)
        self.assertEqual(application.reviewed_by, admin_user)
        self.assertIsNotNone(application.reviewed_at)
