from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package, PlayerType
from apps.orders.models import Order
from apps.players.models import Player

from .models import PlayerEarning
from .serializers import PlayerEarningSerializer


class EarningBossVisibilityTests(TestCase):
    def test_earning_serializer_includes_boss_nickname(self):
        boss_user = User.objects.create_user(username='earning-boss-visible')
        ClientProfile.objects.create(
            user=boss_user,
            openid='earning-boss-visible-openid',
            nickname='工资老板',
            nickname_customized=True,
        )
        player_user = User.objects.create_user(username='earning-player-visible')
        player_type = PlayerType.objects.create(name='工资昵称测试陪玩', priority=1)
        player = Player.objects.create(
            user=player_user,
            name='工资昵称测试打手',
            player_type=player_type,
            status=Player.STATUS_APPROVED,
        )
        package = Package.objects.create(name='工资昵称测试套餐', player_count=1, base_price=30)
        order = Order.objects.create(
            order_no='EARNBOSS001',
            boss_user=boss_user,
            boss_wechat='earning-boss-wechat',
            package=package,
            package_name_snapshot='工资昵称测试套餐',
            required_players=1,
            total_price_per_hour=30,
            total_amount=30,
            status=Order.STATUS_COMPLETED,
            paid=True,
        )
        earning = PlayerEarning.objects.create(
            order=order,
            player=player,
            gross_amount=Decimal('300.00'),
            commission_rate=Decimal('16.00'),
            commission_amount=Decimal('48.00'),
            net_amount=Decimal('252.00'),
            review_until=timezone.now() + timedelta(days=8),
        )

        payload = PlayerEarningSerializer(earning).data

        self.assertEqual(payload['boss_name'], '工资老板')
        self.assertEqual(payload['order_no'], order.order_no)
        self.assertEqual(payload['package_name'], '工资昵称测试套餐')
