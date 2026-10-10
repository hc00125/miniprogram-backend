from decimal import Decimal
from django.test import TestCase
from apps.catalog.models import Package, PlayerType
from apps.orders.models import Order, OrderPlayer
from apps.players.models import Player
from apps.earnings.models import OrderCommissionOverride, PlayerEarning


class ConfirmedCommissionTests(TestCase):
    def test_new_mixed_roster_uses_16_only_for_mini_and_freezes_level(self):
        package = Package.objects.create(name='普通陪玩', player_count=2, base_price=100)
        order = Order.objects.create(order_no='new-policy', boss_wechat='offline', package=package, required_players=2, total_amount=100, paid=True)
        for name in ['娱乐mini','技术pro']:
            player = Player.objects.create(name=name, player_type=PlayerType.objects.create(name=name, priority=1))
            OrderPlayer.objects.create(order=order, player=player)
            if name == '娱乐mini':
                player.player_type = PlayerType.objects.create(name='明星陪', priority=6); player.save()
        order.status = Order.STATUS_COMPLETED; order.save()
        rates = list(PlayerEarning.objects.filter(order=order).order_by('player_id').values_list('commission_rate',flat=True))
        self.assertEqual(rates, [Decimal('16'), Decimal('25')])

    def test_mini_discount_excludes_nonordinary_and_other_businesses(self):
        from apps.earnings.commission_policy import ordinary_rate
        from apps.earnings.personal_commission import get_surcharge_commission
        from apps.patronage.models import PatronageSettings, PlayerPatronageConfig
        from apps.patronage.pricing import catalog_data
        player=Player.objects.create(name='mini',player_type=PlayerType.objects.create(name='技术mini',priority=3))
        package=Package.objects.create(name='nonordinary',player_count=1,base_price=100,product_type='guarantee')
        order=Order.objects.create(order_no='nonordinary',boss_wechat='offline',package=package,required_players=1)
        self.assertEqual(ordinary_rate(order,player),25)
        self.assertEqual(get_surcharge_commission(player)['commission_rate'],'25.00')
        PlayerPatronageConfig.objects.create(player=player,hourly_rate_yuan=40)
        self.assertEqual(catalog_data(player)['commission_rate'],'0.25')
        self.assertEqual(catalog_data(player)['packages'][-1]['kind'],'day_pass')

    def test_legacy_order_keeps_original_default_and_earnings_remain_idempotent(self):
        from apps.earnings.settlements import create_order_earnings
        package=Package.objects.create(name='historical',player_count=1,base_price=100)
        player=Player.objects.create(name='old-pro',player_type=PlayerType.objects.create(name='技术pro',priority=4))
        order=Order.objects.create(order_no='old-policy',boss_wechat='offline',package=package,required_players=1,total_amount=100,paid=True,commission_policy='')
        OrderPlayer.objects.create(order=order,player=player)
        order.status=Order.STATUS_COMPLETED;order.save()
        before=PlayerEarning.objects.get(order=order)
        self.assertEqual(before.commission_rate,16)
        from apps.earnings.models import EarningsConfig
        EarningsConfig.objects.all().update(default_commission_rate=25)
        create_order_earnings(order);before.refresh_from_db()
        self.assertEqual(before.commission_rate,16)

    def test_overview_advertises_new_default_without_mutating_legacy_config(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from apps.earnings.wallet import get_earnings_config
        user=get_user_model().objects.create_user('overview-new')
        Player.objects.create(user=user,name='overview-new',player_type=PlayerType.objects.create(name='技术mini',priority=3))
        config=get_earnings_config();before=config.default_commission_rate
        c=APIClient();c.force_authenticate(user)
        r=c.get('/api/player/earnings/overview')
        self.assertEqual(r.status_code,200)
        self.assertEqual(Decimal(str(r.data['default_commission_rate'])),25)
        self.assertEqual(r.data['commission_policy'],'standard25-mini16-v1')
        config.refresh_from_db();self.assertEqual(config.default_commission_rate,before)

    def test_explicit_zero_override_is_not_silently_replaced(self):
        package = Package.objects.create(name='专属', player_count=1, base_price=100)
        player = Player.objects.create(name='pro', player_type=PlayerType.objects.create(name='技术pro', priority=4))
        order = Order.objects.create(order_no='explicit-zero', boss_wechat='offline', package=package, required_players=1, total_amount=100, paid=True)
        OrderPlayer.objects.create(order=order, player=player)
        OrderCommissionOverride.objects.create(order=order, commission_rate=0, reason='个人商品明确例外')
        order.status=Order.STATUS_COMPLETED; order.save()
        self.assertEqual(PlayerEarning.objects.get(order=order).commission_rate, 0)
