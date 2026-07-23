from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.catalog.models import Package, PackageSpec, PlayerType
from apps.payments.models import Payment, VirtualProductBinding
from apps.payments.services import mark_payment_paid
from apps.players.models import Player, PlayerServiceListing

from .models import Order, OrderDesignation


@override_settings(PLAYER_SERVICE_LISTING_AUTO_APPROVE=False)
class SharedPlayerServiceListingTests(TestCase):
    def setUp(self):
        self.boss_user = User.objects.create_user(username='shared-listing-boss')
        self.player_user = User.objects.create_user(username='shared-listing-player')
        self.player_type = PlayerType.objects.create(
            name='共享服务技术陪',
            priority=10,
            price_extra=0,
        )
        self.player = Player.objects.create(
            user=self.player_user,
            name='共享服务陪玩师',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
            can_accept_orders=True,
            can_be_designated=True,
            is_publicly_visible=True,
        )
        self.package = Package.objects.create(
            name='共享四套四弹',
            product_type=Package.PRODUCT_TYPE_NORMAL,
            selling_mode=Package.SELLING_MODE_PUBLIC,
            owner_player=None,
            player_count=1,
            base_price=25,
            is_active=True,
        )
        self.spec = PackageSpec.objects.create(
            package=self.package,
            name='技术陪练25元',
            price=25,
            required_player_type=self.player_type,
            is_active=True,
        )
        VirtualProductBinding.objects.create(
            spec=self.spec,
            product_id='shared_service_25',
            goods_price_fen=2500,
            is_active=True,
        )
        self.player_client = APIClient()
        self.player_client.force_authenticate(self.player_user)

    def test_player_can_submit_bound_shared_spec_for_review(self):
        response = self.player_client.get('/api/player/service-listings')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item['id'] for item in response.data['available_specs']],
            [self.spec.id],
        )

        response = self.player_client.post(
            '/api/player/service-listings',
            {'spec_id': self.spec.id, 'custom_description': '熟悉地图和任务流程'},
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        listing = PlayerServiceListing.objects.get(player=self.player, spec=self.spec)
        self.assertEqual(listing.status, PlayerServiceListing.STATUS_PENDING)
        self.assertEqual(listing.custom_description, '熟悉地图和任务流程')

    @override_settings(PLAYER_SERVICE_LISTING_AUTO_APPROVE=True)
    def test_eligible_standard_service_can_auto_approve(self):
        response = self.player_client.post(
            '/api/player/service-listings',
            {'spec_id': self.spec.id},
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        listing = PlayerServiceListing.objects.get(player=self.player, spec=self.spec)
        self.assertEqual(listing.status, PlayerServiceListing.STATUS_APPROVED)

    def test_boss_contract_and_order_use_listing_without_changing_package_owner(self):
        listing = PlayerServiceListing.objects.create(
            player=self.player,
            spec=self.spec,
            status=PlayerServiceListing.STATUS_APPROVED,
            is_available=True,
            custom_description='共享规格个人展示说明',
        )

        response = APIClient().get(f'/api/catalog/players/{self.player.id}/products')
        self.assertEqual(response.status_code, 200)
        product = response.data['products'][0]
        returned_spec = product['specs'][0]
        self.assertEqual(product['id'], self.package.id)
        self.assertEqual(product['selling_mode'], Package.SELLING_MODE_PLAYER_DESIGNATED)
        self.assertEqual(product['owner_player_id'], self.player.id)
        self.assertEqual(returned_spec['listing_id'], listing.id)
        self.assertEqual(returned_spec['listing_description'], '共享规格个人展示说明')
        self.package.refresh_from_db()
        self.assertIsNone(self.package.owner_player_id)

        boss_client = APIClient()
        boss_client.force_authenticate(self.boss_user)
        response = boss_client.post(
            '/api/boss/listing-order',
            {
                'listing_id': listing.id,
                'boss_wechat': 'shared-listing-openid',
                'game_id': 'SHARED-LISTING-ROOM',
                'package_id': self.package.id,
                'spec_id': self.spec.id,
                'quantity': 3,
                'booked_hours': 3,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(order_no=response.data['order_no'])
        self.assertEqual(order.fulfillment_mode, Order.FULFILLMENT_MODE_TARGETED)
        self.assertEqual(order.target_player, self.player)
        self.assertEqual(order.package, self.package)
        self.assertEqual(order.spec_id, self.spec.id)
        self.assertEqual(order.total_price_per_hour, 25)
        self.assertEqual(order.total_amount, 75)
        self.assertEqual(order.booked_hours, 3)
        self.assertFalse(order.designations.exists())

        payment = Payment.objects.create(
            payment_no='SHARED_LISTING_PAY',
            order=order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=order.total_amount,
            status='paying',
        )
        with patch('apps.orders.targeted_notifications.notify_paid_targeted_order'):
            with self.captureOnCommitCallbacks(execute=True):
                mark_payment_paid(payment, third_trade_no='wx-shared-listing-paid')
        invitation = OrderDesignation.objects.get(order=order, player=self.player)
        self.assertEqual(invitation.status, OrderDesignation.STATUS_PENDING)

    def test_listing_order_rejects_mismatched_spec(self):
        listing = PlayerServiceListing.objects.create(
            player=self.player,
            spec=self.spec,
            status=PlayerServiceListing.STATUS_APPROVED,
            is_available=True,
        )
        other_spec = PackageSpec.objects.create(
            package=self.package,
            name='错误规格',
            price=30,
            is_active=True,
        )
        boss_client = APIClient()
        boss_client.force_authenticate(self.boss_user)
        response = boss_client.post(
            '/api/boss/listing-order',
            {
                'listing_id': listing.id,
                'boss_wechat': 'shared-listing-openid',
                'package_id': self.package.id,
                'spec_id': other_spec.id,
                'quantity': 1,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('不一致', str(response.data))
