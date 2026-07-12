from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.catalog.models import Package, PlayerType
from apps.orders.models import Order, OrderPlayer, Rating
from apps.orders.rating_views import order_ratings
from apps.players.models import Player


class OrderRatingStatusTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.owner = User.objects.create_user(username='rating-status-owner')
        self.other = User.objects.create_user(username='rating-status-other')
        self.admin = User.objects.create_user(username='rating-status-admin', is_staff=True)
        self.package = Package.objects.create(name='评价状态测试套餐', player_count=2, base_price=30)
        self.player_type = PlayerType.objects.create(name='评价状态陪玩', priority=1)
        self.players = [
            Player.objects.create(name='评价陪玩A', player_type=self.player_type),
            Player.objects.create(name='评价陪玩B', player_type=self.player_type),
        ]
        self.order = Order.objects.create(
            order_no='RATINGSTATUS001',
            boss_user=self.owner,
            boss_wechat='rating-owner',
            package=self.package,
            required_players=2,
            total_price_per_hour=30,
            total_amount=30,
            status=Order.STATUS_COMPLETED,
            paid=True,
        )
        for player in self.players:
            OrderPlayer.objects.create(order=self.order, player=player)

    def get_response(self, user):
        request = self.factory.get('/api/boss/order/RATINGSTATUS001/ratings')
        force_authenticate(request, user=user)
        return order_ratings(request, self.order.order_no)

    def test_owner_sees_existing_and_remaining_ratings(self):
        Rating.objects.create(order=self.order, player=self.players[0], rating=5, comment='很好')

        response = self.get_response(self.owner)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['rated_player_ids'], [self.players[0].id])
        self.assertEqual(response.data['remaining_player_ids'], [self.players[1].id])
        self.assertFalse(response.data['all_rated'])
        self.assertEqual(response.data['results'][0]['rating'], 5)
        self.assertEqual(response.data['results'][0]['comment'], '很好')

    def test_all_rated_is_true_after_every_player_has_rating(self):
        for player in self.players:
            Rating.objects.create(order=self.order, player=player, rating=5)

        response = self.get_response(self.owner)

        self.assertTrue(response.data['all_rated'])
        self.assertEqual(response.data['remaining_player_ids'], [])

    def test_other_user_cannot_read_rating_status(self):
        response = self.get_response(self.other)
        self.assertEqual(response.status_code, 403)

    def test_admin_can_read_rating_status(self):
        response = self.get_response(self.admin)
        self.assertEqual(response.status_code, 200)
