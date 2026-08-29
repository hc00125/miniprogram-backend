from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package, PlayerType
from apps.orders.models import Order, OrderPlayer

from .models import Player


class PlayerOrderBossNameTests(TestCase):
    def test_my_orders_includes_boss_nickname(self):
        boss_user = User.objects.create_user(username='boss-visible')
        ClientProfile.objects.create(
            user=boss_user,
            openid='boss-visible-openid',
            nickname='萤老板',
            nickname_customized=True,
        )
        player_user = User.objects.create_user(username='player-visible')
        player_type = PlayerType.objects.create(name='老板昵称测试陪玩', priority=1)
        player = Player.objects.create(
            user=player_user,
            name='昵称测试打手',
            player_type=player_type,
            status=Player.STATUS_APPROVED,
        )
        package = Package.objects.create(name='老板昵称测试套餐', player_count=1, base_price=20)
        order = Order.objects.create(
            order_no='BOSSNAME001',
            boss_user=boss_user,
            boss_wechat='boss-visible-wechat',
            package=package,
            required_players=1,
            total_price_per_hour=20,
            total_amount=20,
            status=Order.STATUS_READY_TO_START,
            paid=True,
        )
        OrderPlayer.objects.create(order=order, player=player)

        client = APIClient()
        client.force_authenticate(player_user)
        response = client.get('/api/player/my-orders')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]['order_no'], order.order_no)
        self.assertEqual(response.data[0]['boss_name'], '萤老板')
