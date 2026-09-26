from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.catalog.models import Package, PlayerType
from apps.orders.models import Order, OrderPlayer

from .models import Player, PlayerProfileUpdateRequest


class PlayerP1Tests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='p1-player', password='test-pass')
        self.player_type = PlayerType.objects.create(name='P1技术陪', priority=1, price_extra=0)
        self.player = Player.objects.create(
            user=self.user,
            name='P1陪玩',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )
        self.package = Package.objects.create(name='P1测试套餐', base_price=15, player_count=1)
        self.order = Order.objects.create(
            order_no='P1ROOM000000000001',
            boss_user=self.user,
            boss_wechat='p1-boss-openid',
            package=self.package,
            required_players=1,
            status=Order.STATUS_PENDING_PAYMENT,
            total_price_per_hour=15,
            total_amount=15,
        )
        self.relation = OrderPlayer.objects.create(order=self.order, player=self.player)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_player_permissions_default_to_enabled(self):
        self.assertTrue(self.player.can_accept_orders)
        self.assertTrue(self.player.can_be_designated)
        self.assertTrue(self.player.is_publicly_visible)
        self.assertTrue(self.player.can_withdraw)

    def test_profile_update_stays_pending_and_does_not_replace_public_profile(self):
        self.player.bio = '当前公开简介'
        self.player.save(update_fields=['bio'])

        response = self.client.post('/api/player/profile-settings', {
            'bio': '新的待审核简介',
            'audio_intro_url': 'https://example.com/intro.mp3',
            'audio_intro_title': '新的语音介绍',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        update = PlayerProfileUpdateRequest.objects.get(player=self.player)
        self.assertEqual(update.status, PlayerProfileUpdateRequest.STATUS_PENDING)
        self.assertEqual(update.bio, '新的待审核简介')
        self.player.refresh_from_db()
        self.assertEqual(self.player.bio, '当前公开简介')

    def test_repeated_profile_update_reuses_pending_request(self):
        first = self.client.post('/api/player/profile-settings', {'bio': '第一稿'}, format='json')
        second = self.client.post('/api/player/profile-settings', {'bio': '第二稿'}, format='json')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(PlayerProfileUpdateRequest.objects.filter(player=self.player).count(), 1)
        self.assertEqual(PlayerProfileUpdateRequest.objects.get(player=self.player).bio, '第二稿')

    def test_order_player_gets_ten_minute_room_deadline(self):
        seconds = (self.relation.room_join_deadline - self.relation.grab_time).total_seconds()
        self.assertGreaterEqual(seconds, 599)
        self.assertLessEqual(seconds, 601)
        self.assertEqual(self.relation.room_join_status, OrderPlayer.ROOM_ENTRY_PENDING)

    def test_room_entry_confirmation_on_time(self):
        response = self.client.post(f'/api/player/order/{self.order.order_no}/room-entry/confirm', {}, format='json')

        self.assertEqual(response.status_code, 200)
        self.relation.refresh_from_db()
        self.assertEqual(self.relation.room_join_status, OrderPlayer.ROOM_ENTRY_CONFIRMED)
        self.assertIsNotNone(self.relation.room_join_confirmed_at)

    def test_room_entry_confirmation_after_deadline_is_late(self):
        self.relation.room_join_deadline = timezone.now() - timedelta(seconds=1)
        self.relation.save(update_fields=['room_join_deadline'])

        response = self.client.post(f'/api/player/order/{self.order.order_no}/room-entry/confirm', {}, format='json')

        self.assertEqual(response.status_code, 200)
        self.relation.refresh_from_db()
        self.assertEqual(self.relation.room_join_status, OrderPlayer.ROOM_ENTRY_LATE_CONFIRMED)
        self.assertTrue(response.data['was_late'])

    def test_disabled_order_permission_hides_available_orders_and_blocks_grab(self):
        self.player.can_accept_orders = False
        self.player.save(update_fields=['can_accept_orders'])

        response = self.client.get('/api/player/available-orders')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

        grab_response = self.client.post('/api/player/grab', {
            'order_no': self.order.order_no,
            'player_id': self.player.id,
        }, format='json')
        self.assertEqual(grab_response.status_code, 403)
