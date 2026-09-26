from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.catalog.models import Package, PackageSpec, PlayerType
from apps.orders.batch_views import create_cart_order_batch
from apps.orders.models import CartItem, Order


class CartBatchOrderTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(username='batch-owner', password='pass')
        self.male_type = PlayerType.objects.create(name='男陪', priority=1)
        self.female_type = PlayerType.objects.create(name='女陪', priority=1)
        self.male_package = Package.objects.create(name='男陪订单', player_count=1, base_price=20)
        self.female_package = Package.objects.create(name='女陪订单', player_count=1, base_price=30)
        self.male_spec = PackageSpec.objects.create(
            package=self.male_package,
            name='男陪规格',
            price=20,
            required_player_type=self.male_type,
        )
        self.female_spec = PackageSpec.objects.create(
            package=self.female_package,
            name='女陪规格',
            price=30,
            required_player_type=self.female_type,
        )

    def add_cart_item(self, package, spec, quantity=1):
        return CartItem.objects.create(
            user=self.user,
            package=package,
            spec=spec,
            spec_id_snapshot=str(spec.id),
            spec_name=spec.name,
            spec_display_name=spec.name,
            price=spec.price,
            quantity=quantity,
        )

    def post_batch(self, item_ids):
        request = self.factory.post('/api/boss/orders/batch', {
            'boss_wechat': 'batch-openid',
            'game_id': 'owner-game-id',
            'cart_item_ids': item_ids,
            'boss_note': '请一起安排',
        }, format='json')
        force_authenticate(request, user=self.user)
        return create_cart_order_batch(request)

    def test_two_different_types_create_two_independent_orders(self):
        male_item = self.add_cart_item(self.male_package, self.male_spec)
        female_item = self.add_cart_item(self.female_package, self.female_spec)

        response = self.post_batch([male_item.id, female_item.id])

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['order_count'], 2)
        self.assertEqual(len(response.data['order_nos']), 2)
        orders = list(Order.objects.filter(boss_user=self.user).order_by('id'))
        self.assertEqual(len(orders), 2)
        self.assertEqual({order.package_id for order in orders}, {self.male_package.id, self.female_package.id})
        self.assertEqual(
            {order.designated_types[0]['type_id'] for order in orders},
            {self.male_type.id, self.female_type.id},
        )
        self.assertTrue(all(order.status == Order.STATUS_WAITING for order in orders))
        self.assertTrue(all(order.items.count() == 1 for order in orders))
        self.assertTrue(all(order.items.get().quantity == 1 for order in orders))
        self.assertEqual(CartItem.objects.filter(user=self.user).count(), 0)

    def test_cart_quantity_becomes_single_order_duration(self):
        item = self.add_cart_item(self.male_package, self.male_spec, quantity=2)

        response = self.post_batch([item.id])

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['order_count'], 1)
        orders = Order.objects.filter(boss_user=self.user, package=self.male_package)
        self.assertEqual(orders.count(), 1)
        order = orders.get()
        self.assertEqual(order.booked_hours, 2)
        self.assertEqual(order.total_price_per_hour, 20)
        self.assertEqual(order.total_amount, 40)
        self.assertEqual(order.items.get().quantity, 2)
        self.assertEqual(order.items.get().amount, 40)

    def test_existing_active_order_blocks_new_batch(self):
        existing = self.add_cart_item(self.male_package, self.male_spec)
        Order.objects.create(
            order_no='ACTIVEBATCH001',
            boss_user=self.user,
            boss_wechat='batch-openid',
            package=self.male_package,
            required_players=1,
            total_price_per_hour=20,
            total_amount=20,
            status=Order.STATUS_WAITING,
        )

        response = self.post_batch([existing.id])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.filter(boss_user=self.user).count(), 1)
        self.assertTrue(CartItem.objects.filter(id=existing.id).exists())
