from unittest.mock import Mock, patch

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import RequestFactory, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package, PlayerType
from apps.orders.models import Order, Rating

from .admin import PlayerApplicationAdmin
from .approval import approve_player_application
from .models import Player, PlayerApplication
from .serializers import PlayerApplicationCreateSerializer, PlayerSerializer


class UpdateOnlineStatusTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.player_type = PlayerType.objects.create(name='测试类型', priority=0, is_active=True)
        self.user = User.objects.create_user(username='testplayer', password='testpass123')
        self.player = Player.objects.create(
            user=self.user,
            name='测试陪玩',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
            is_online=False,
        )
        self.url = '/api/player/online-status'

    def test_approved_player_set_online(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {'is_online': True}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['is_online'], True)
        self.assertEqual(response.data['status'], '在线')
        self.player.refresh_from_db()
        self.assertTrue(self.player.is_online)

    def test_approved_player_set_offline(self):
        self.player.is_online = True
        self.player.save()
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {'is_online': False}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['is_online'], False)
        self.assertEqual(response.data['status'], '离线')
        self.player.refresh_from_db()
        self.assertFalse(self.player.is_online)

    def test_unauthenticated_user_blocked(self):
        response = self.client.post(self.url, {'is_online': True}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_non_approved_player_blocked(self):
        self.player.status = Player.STATUS_PENDING
        self.player.save()
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {'is_online': True}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_invalid_is_online_type(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {'is_online': 'yes'}, format='json')
        self.assertEqual(response.status_code, 400)


class PlayerRatingApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.player_type = PlayerType.objects.create(name='评分类型', priority=0, is_active=True)
        self.player_user = User.objects.create_user(username='rated-player', password='password')
        self.player = Player.objects.create(
            user=self.player_user,
            name='评分陪玩',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
            total_orders=3,
            total_rating=9,
            rating_count=2,
        )
        self.boss_user = User.objects.create_user(username='rating-boss', password='password')
        self.package = Package.objects.create(name='评分测试套餐', base_price=15, player_count=1)
        self.order = Order.objects.create(
            order_no='RATING000001',
            boss_user=self.boss_user,
            boss_wechat='boss-wechat',
            package=self.package,
            package_name_snapshot='四套四弹娱乐陪',
            required_players=1,
            status=Order.STATUS_COMPLETED,
            total_amount=15,
            paid=True,
        )
        Rating.objects.create(order=self.order, player=self.player, rating=5, comment='技术很好')
        second_order = Order.objects.create(
            order_no='RATING000002',
            boss_user=self.boss_user,
            boss_wechat='boss-wechat',
            package=self.package,
            required_players=1,
            status=Order.STATUS_COMPLETED,
            total_amount=15,
            paid=True,
        )
        Rating.objects.create(order=second_order, player=self.player, rating=4, comment='沟通顺畅')

    def test_public_player_ratings_returns_summary_and_recent_reviews(self):
        response = self.client.get(f'/api/player/{self.player.id}/ratings')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['summary']['average_rating'], 4.5)
        self.assertEqual(response.data['summary']['rating_count'], 2)
        self.assertEqual(response.data['summary']['total_orders'], 3)
        self.assertEqual(len(response.data['results']), 2)
        self.assertEqual(response.data['results'][1]['package_name'], '四套四弹娱乐陪')
        self.assertNotIn('boss_user', response.data['results'][0])

    def test_player_can_view_own_ratings(self):
        self.client.force_authenticate(user=self.player_user)
        response = self.client.get('/api/player/ratings/me')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['player_id'], self.player.id)
        self.assertEqual(response.data['summary']['rating_count'], 2)

    def test_non_approved_player_cannot_view_personal_ratings(self):
        self.player.status = Player.STATUS_PENDING
        self.player.save(update_fields=['status'])
        self.client.force_authenticate(user=self.player_user)
        response = self.client.get('/api/player/ratings/me')
        self.assertEqual(response.status_code, 403)


class PlayerApplicationSerializerTests(TestCase):
    def setUp(self):
        self.player_type = PlayerType.objects.create(name='审核类型', priority=0, is_active=True)
        self.base_payload = {
            'name': '公开昵称',
            'real_name': '张三',
            'type_id': self.player_type.id,
            'contact_wechat': 'test-wechat',
            'bio': '测试申请',
        }

    def test_real_name_is_required(self):
        payload = dict(self.base_payload)
        payload.pop('real_name')
        serializer = PlayerApplicationCreateSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn('real_name', serializer.errors)

    def test_real_name_is_trimmed_and_validated(self):
        payload = dict(self.base_payload, real_name='  欧阳 娜娜  ')
        serializer = PlayerApplicationCreateSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['real_name'], '欧阳 娜娜')

    def test_real_name_rejects_digits(self):
        payload = dict(self.base_payload, real_name='张三123')
        serializer = PlayerApplicationCreateSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn('real_name', serializer.errors)

    def test_name_rejects_existing_player_case_insensitively(self):
        owner = User.objects.create_user(username='existing-player')
        Player.objects.create(
            user=owner,
            name='公开昵称',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )
        serializer = PlayerApplicationCreateSerializer(
            data=dict(self.base_payload, name='  公开昵称  '),
        )
        self.assertFalse(serializer.is_valid())
        self.assertEqual(str(serializer.errors['name'][0]), '该昵称已被其他陪玩师使用，请换一个')

    def test_name_rejects_pending_or_approved_application(self):
        other_user = User.objects.create_user(username='reserved-name')
        PlayerApplication.objects.create(
            user=other_user,
            name='公开昵称',
            real_name='李四',
            player_type=self.player_type,
            contact_wechat='other-wechat',
            status=PlayerApplication.STATUS_PENDING,
        )
        serializer = PlayerApplicationCreateSerializer(data=self.base_payload)
        self.assertFalse(serializer.is_valid())
        self.assertEqual(str(serializer.errors['name'][0]), '该昵称正在被其他申请人使用，请换一个')

    def test_rejected_application_does_not_reserve_name(self):
        other_user = User.objects.create_user(username='rejected-name')
        PlayerApplication.objects.create(
            user=other_user,
            name='公开昵称',
            real_name='李四',
            player_type=self.player_type,
            contact_wechat='other-wechat',
            status=PlayerApplication.STATUS_REJECTED,
        )
        serializer = PlayerApplicationCreateSerializer(data=self.base_payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_real_name_is_not_exposed_by_public_player_serializer(self):
        user = User.objects.create_user(username='privacy-test')
        player = Player.objects.create(
            user=user,
            name='公开昵称',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )
        serialized = PlayerSerializer(player).data
        self.assertNotIn('real_name', serialized)
        self.assertIn('rating_count', serialized)


class PlayerApplicationApprovalTests(TestCase):
    def setUp(self):
        self.player_type = PlayerType.objects.create(name='审批事务类型', priority=1, is_active=True)
        self.admin_user = User.objects.create_superuser(
            username='approval-admin',
            email='admin@example.com',
            password='password',
        )
        self.applicant = User.objects.create_user(username='approval-applicant', password='password')
        self.profile = ClientProfile.objects.create(
            user=self.applicant,
            openid='approval-openid',
            nickname='审批用户',
            player_status=ClientProfile.PLAYER_STATUS_PENDING,
        )
        self.application = PlayerApplication.objects.create(
            user=self.applicant,
            name='待审批陪玩',
            real_name='张三',
            player_type=self.player_type,
            contact_wechat='approval-wechat',
            bio='审批测试',
            status=PlayerApplication.STATUS_PENDING,
        )

    def test_approval_creates_player_and_syncs_all_statuses(self):
        player, application = approve_player_application(self.application.id, self.admin_user)

        self.profile.refresh_from_db()
        application.refresh_from_db()
        player.refresh_from_db()

        self.assertEqual(application.status, PlayerApplication.STATUS_APPROVED)
        self.assertEqual(application.reviewed_by, self.admin_user)
        self.assertIsNotNone(application.reviewed_at)
        self.assertEqual(self.profile.player_status, ClientProfile.PLAYER_STATUS_APPROVED)
        self.assertEqual(player.user, self.applicant)
        self.assertEqual(player.name, '待审批陪玩')
        self.assertEqual(player.status, Player.STATUS_APPROVED)

    def test_duplicate_name_leaves_application_and_profile_pending(self):
        other_user = User.objects.create_user(username='duplicate-owner')
        Player.objects.create(
            user=other_user,
            name=self.application.name,
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )

        with self.assertRaises(ValidationError):
            approve_player_application(self.application.id, self.admin_user)

        self.application.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(self.application.status, PlayerApplication.STATUS_PENDING)
        self.assertEqual(self.profile.player_status, ClientProfile.PLAYER_STATUS_PENDING)
        self.assertFalse(Player.objects.filter(user=self.applicant).exists())

    def test_database_integrity_error_rolls_back_approval(self):
        with patch('apps.players.approval.Player.objects.update_or_create', side_effect=IntegrityError('duplicate')):
            with self.assertRaises(ValidationError):
                approve_player_application(self.application.id, self.admin_user)

        self.application.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(self.application.status, PlayerApplication.STATUS_PENDING)
        self.assertEqual(self.profile.player_status, ClientProfile.PLAYER_STATUS_PENDING)
        self.assertFalse(Player.objects.filter(user=self.applicant).exists())


class PlayerApplicationAdminTests(TestCase):
    def test_masked_real_name(self):
        application = PlayerApplication(name='测试陪玩', real_name='王小明')
        model_admin = PlayerApplicationAdmin(PlayerApplication, admin.site)
        self.assertEqual(model_admin.masked_real_name(application), '王*明')

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
            real_name='张三',
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

    def test_bulk_approval_duplicate_name_keeps_failed_application_pending(self):
        player_type = PlayerType.objects.create(name='批量审批类型', priority=2, is_active=True)
        admin_user = User.objects.create_superuser(
            username='bulk-admin',
            email='bulk@example.com',
            password='password',
        )
        existing_user = User.objects.create_user(username='bulk-existing')
        Player.objects.create(
            user=existing_user,
            name='重复昵称',
            player_type=player_type,
            status=Player.STATUS_APPROVED,
        )
        applicant = User.objects.create_user(username='bulk-applicant')
        profile = ClientProfile.objects.create(
            user=applicant,
            openid='bulk-openid',
            nickname='批量申请人',
            player_status=ClientProfile.PLAYER_STATUS_PENDING,
        )
        application = PlayerApplication.objects.create(
            user=applicant,
            name='重复昵称',
            real_name='李四',
            player_type=player_type,
            contact_wechat='bulk-wechat',
            status=PlayerApplication.STATUS_PENDING,
        )

        request = RequestFactory().post('/admin/players/playerapplication/')
        request.user = admin_user
        model_admin = PlayerApplicationAdmin(PlayerApplication, admin.site)
        model_admin.message_user = Mock()
        model_admin.approve_applications(
            request,
            PlayerApplication.objects.filter(pk=application.pk),
        )

        application.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(application.status, PlayerApplication.STATUS_PENDING)
        self.assertEqual(profile.player_status, ClientProfile.PLAYER_STATUS_PENDING)
        self.assertFalse(Player.objects.filter(user=applicant).exists())
        self.assertTrue(any(call.kwargs.get('level') for call in model_admin.message_user.call_args_list))
