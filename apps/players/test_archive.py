from django.test import TestCase
from django.contrib.auth import get_user_model
from apps.catalog.models import PlayerType
from apps.players.models import Player


class PlayerArchiveTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username='archive-admin')
        self.player = Player.objects.create(name='原昵称', player_type=PlayerType.objects.create(name='普通', priority=1), is_online=True)

    def test_archived_discovery_detail_and_new_business_gates(self):
        from rest_framework.test import APIClient
        from apps.patronage.pricing import player_is_available
        from apps.orders.discipline import discipline_block_reason
        Player.objects.filter(pk=self.player.pk).update(is_archived=True)
        self.player.refresh_from_db()
        self.assertFalse(player_is_available(self.player))
        self.assertIn('已离开', discipline_block_reason(self.player))
        client = APIClient()
        self.assertEqual(client.get('/api/player/list').json(), [])
        response = client.get('/api/player/%s/detail' % self.player.pk)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['is_archived'])
        self.assertFalse(response.json()['can_be_designated'])

    def test_admin_preview_requires_confirmation_and_reason(self):
        from django.contrib import admin
        from apps.players.admin import PlayerAdmin
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        model_admin = PlayerAdmin(Player, admin.site)
        self.assertIn('archive_selected', model_admin.actions)
        self.actor.is_superuser = True
        self.actor.is_staff = True
        self.actor.save()
        def request(data):
            req = RequestFactory().post('/admin/players/player/', data)
            req.user = self.actor
            req.session = {}
            req._messages = FallbackStorage(req)
            return req
        qs = Player.objects.filter(pk=self.player.pk)
        response = model_admin.archive_selected(request({}), qs)
        response.render()
        self.assertContains(response, '原昵称')
        self.assertContains(response, '未结订单')
        self.assertContains(response, '待处理提现')
        self.assertContains(response, '有效冠名')
        model_admin.archive_selected(request({'confirm': 'yes', 'reason': ''}), qs)
        self.player.refresh_from_db()
        self.assertFalse(self.player.is_archived)

    def test_stale_player_cannot_take_new_order_or_invitation(self):
        from apps.catalog.models import Package
        from apps.orders.models import Order, OrderPlayer, OrderDesignation
        from django.utils import timezone
        from datetime import timedelta
        from rest_framework.exceptions import ValidationError
        from apps.players.archive import archive_players
        package = Package.objects.create(name='测试商品', base_price=10, player_count=1)
        order = Order.objects.create(order_no='archive-order', package=package, boss_wechat='fixture', required_players=1, total_price_per_hour=10)
        archive_players([self.player.pk], actor=self.actor, reason='结束合作')
        with self.assertRaises(ValidationError):
            OrderPlayer.objects.create(order=order, player=self.player)
        with self.assertRaises(ValidationError):
            OrderDesignation.objects.create(order=order, player=self.player, expires_at=timezone.now()+timedelta(minutes=10))
        self.assertFalse(order.order_players.exists())
        self.assertFalse(order.designations.exists())

    def test_history_uses_original_departed_name_and_keeps_fulfillment(self):
        from apps.catalog.models import Package
        from apps.orders.models import Order, OrderPlayer
        from apps.orders.serializers import OrderPlayerSerializer
        from apps.players.archive import archive_players
        package = Package.objects.create(name='历史商品', base_price=10, player_count=1)
        order = Order.objects.create(order_no='history-order', package=package, boss_wechat='fixture', required_players=1, total_price_per_hour=10)
        relation = OrderPlayer.objects.create(order=order, player=self.player)
        archive_players([self.player.pk], actor=self.actor, reason='结束合作')
        Player.objects.filter(pk=self.player.pk).update(name='后来账号昵称')
        relation = OrderPlayer.objects.select_related('player').get(pk=relation.pk)
        self.assertEqual(OrderPlayerSerializer(relation).data['name'], '原昵称（已离开）')
        relation.room_join_status = OrderPlayer.ROOM_ENTRY_CONFIRMED
        relation.save(update_fields=['room_join_status'])
        self.assertEqual(OrderPlayer.objects.get(pk=relation.pk).room_join_status, 'confirmed')

    def test_targeted_order_history_labels_snapshot_without_rewriting_it(self):
        from apps.catalog.models import Package
        from apps.orders.models import Order
        from apps.orders.serializers import BossOrderListSerializer, BossOrderDetailSerializer
        from apps.players.archive import archive_players
        package = Package.objects.create(name='历史指定商品', base_price=10, player_count=1)
        order = Order.objects.create(order_no='target-history', package=package, boss_wechat='fixture', required_players=1, total_price_per_hour=10, target_player=self.player, target_player_name_snapshot='下单时昵称')
        archive_players([self.player.pk], actor=self.actor, reason='结束合作')
        order.refresh_from_db()
        for serializer in (BossOrderListSerializer, BossOrderDetailSerializer):
            self.assertEqual(serializer(order).data['target_player_name_snapshot'], '下单时昵称（已离开）')
        order.refresh_from_db()
        self.assertEqual(order.target_player_name_snapshot, '下单时昵称')

    def test_stale_profile_save_cannot_unarchive_or_enable_selling(self):
        from apps.players.archive import archive_players
        archive_players([self.player.pk], actor=self.actor, reason='结束合作')
        self.player.bio = '仍可维护资料'
        self.player.save()
        self.player.refresh_from_db()
        self.assertTrue(self.player.is_archived)
        self.assertFalse(self.player.is_online)
        self.assertFalse(self.player.can_be_designated)

    def test_archived_target_cannot_receive_new_order(self):
        from apps.catalog.models import Package
        from apps.orders.models import Order
        from apps.players.archive import archive_players
        from rest_framework.exceptions import ValidationError
        package = Package.objects.create(name='目标商品', base_price=10, player_count=1)
        archive_players([self.player.pk], actor=self.actor, reason='结束合作')
        with self.assertRaises(ValidationError):
            Order.objects.create(order_no='target-after-archive', package=package, target_player=self.player, boss_wechat='fixture', required_players=1, total_price_per_hour=10)

    def test_archive_hides_legacy_online_discovery_and_heartbeat(self):
        from rest_framework.test import APIClient
        self.player.user = self.actor
        self.player.save()
        Player.objects.filter(pk=self.player.pk).update(is_archived=True)
        client = APIClient()
        self.assertEqual(client.get('/api/boss/online-players').json(), [])
        client.force_authenticate(self.actor)
        self.assertEqual(client.post('/api/player/presence/heartbeat').json(), {'tracked': False})

    def test_old_patronage_link_returns_departed_catalog_without_purchase(self):
        from rest_framework.test import APIClient
        from apps.players.archive import archive_players
        archive_players([self.player.pk], actor=self.actor, reason='结束合作')
        response = APIClient().get('/api/patronage/catalog/', {'player_id': self.player.pk})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['player']['is_archived'])
        self.assertIn('已离开', response.json()['player']['name'])
        self.assertTrue(all(not p['available'] for p in response.json()['packages']))

    def test_admin_signed_bulk_archive_filter_and_restore(self):
        from django.contrib import admin
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        from apps.players.admin import PlayerAdmin
        from apps.earnings.models import PlayerWallet, Withdrawal
        from apps.players.archive_admin import preview
        self.actor.is_staff = self.actor.is_superuser = True
        self.actor.save()
        other = Player.objects.create(name='另一陪玩', player_type=self.player.player_type)
        wallet, _ = PlayerWallet.objects.update_or_create(player=self.player, defaults={'available_balance': 100, 'withdrawing_balance': 20})
        withdrawal = Withdrawal.objects.create(player=self.player, wallet=wallet, withdrawal_no='pending-archive', amount=20, account_name='测试', account_no='fixture')
        ma = PlayerAdmin(Player, admin.site)
        def req(data=None):
            request = RequestFactory().post('/admin/players/player/', data or {})
            request.user = self.actor
            request.session = {}
            request._messages = FallbackStorage(request)
            return request
        qs = Player.objects.filter(pk__in=[self.player.pk, other.pk])
        response = ma.archive_selected(req(), qs)
        token = response.context_data['preview_token']
        response = ma.archive_selected(req({'confirm': 'yes', 'reason': '批量结束合作', 'preview_token': token}), qs)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Player.objects.filter(is_archived=True).count(), 2)
        wallet.refresh_from_db(); withdrawal.refresh_from_db()
        self.assertEqual(wallet.available_balance, 100)
        self.assertEqual(withdrawal.status, 'pending_review')
        self.assertEqual(preview([self.player])[0]['withdrawals'][0]['id'], withdrawal.pk)
        get = RequestFactory().get('/admin/players/player/'); get.user = self.actor
        self.assertFalse(ma.get_changelist_instance(get).queryset.exists())
        get = RequestFactory().get('/admin/players/player/', {'archive': 'archived'}); get.user = self.actor
        self.assertEqual(ma.get_changelist_instance(get).queryset.count(), 2)
        response = ma.restore_selected(req(), qs)
        token = response.context_data['preview_token']
        response = ma.restore_selected(req({'confirm': 'yes', 'reason': '恢复资料', 'preview_token': token}), qs)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Player.objects.filter(is_archived=True).exists())
        self.assertFalse(Player.objects.filter(is_online=True).exists())
        self.assertFalse(ma.has_delete_permission(get, self.player))

    def test_public_detail_does_not_expose_restricted_active_account(self):
        from rest_framework.test import APIClient
        self.player.user = self.actor
        self.player.save()
        self.actor.is_active = False
        self.actor.save()
        response = APIClient().get(f'/api/player/{self.player.pk}/detail')
        self.assertEqual(response.status_code, 404)

    def test_archive_preserves_identity_and_withdrawal(self):
        self.assertTrue(hasattr(self.player, 'is_archived'), 'Missing distinct archive state')
        from apps.players.archive import archive_players, restore_players
        archive_players([self.player.pk], actor=self.actor, reason='结束合作')
        self.player.refresh_from_db()
        self.assertTrue(self.player.is_archived)
        self.assertEqual(self.player.name, '原昵称')
        self.assertEqual(self.player.status, Player.STATUS_APPROVED)
        self.assertTrue(self.player.can_withdraw)
        self.assertFalse(self.player.is_online)
        self.assertFalse(self.player.can_accept_orders)
        self.assertEqual(self.player.archived_by, self.actor)
        self.assertEqual(self.player.archive_reason, '结束合作')
        restore_players([self.player.pk], actor=self.actor, reason='恢复资料')
        self.player.refresh_from_db()
        self.assertFalse(self.player.is_archived)
        self.assertFalse(self.player.is_online)
        self.assertFalse(self.player.is_publicly_visible)
        self.assertFalse(self.player.can_be_designated)
        self.assertEqual(self.player.archive_events.count(), 2)
