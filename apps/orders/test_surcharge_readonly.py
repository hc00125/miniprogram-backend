from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from apps.catalog.models import Package, PlayerType
from apps.players.models import Player
from apps.orders.models import Order, OrderPlayer
from apps.orders.surcharge_models import OrderSurcharge


class SurchargeReadOnlyTests(TestCase):
    def setUp(self):
        self.boss = get_user_model().objects.create_user('boss')
        self.other = get_user_model().objects.create_user('other')
        self.order = Order.objects.create(order_no='o1', boss_user=self.boss, boss_wechat='synthetic',
            package=Package.objects.create(name='test', base_price=10), required_players=1)
        self.url = '/api/boss/orders/o1/surcharge/'
        self.client = APIClient()

    def login(self, user):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(AccessToken.for_user(user)))

    def test_owner_only_real_jwt_no_writes_no_payment_endpoint(self):
        self.assertIn(self.client.get(self.url).status_code, (401, 403))
        self.login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.login(self.boss)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        data = response.json()
        self.assertFalse(data['eligible'])
        self.assertFalse(data['can_submit'])
        self.assertTrue(data['structurally_eligible'])
        self.assertIn('POLICY_UNCONFIRMED', data['blockers'])
        self.assertEqual(data['amount_options_diamonds'], [])
        self.assertEqual(self.client.post(self.url, {'amount_diamonds': 100}).status_code, 400)
        self.assertEqual(self.client.post(self.url, {'amount_diamonds': 100, 'idempotency_key': 'blocked'}, format='json').status_code, 403)

    def test_summaries_only_include_confirmed_paid_no_commission_leak(self):
        # Current admission only permits surcharges on a marked NEW parent;
        # the other tests deliberately retain an ordinary, unmarked lineup.
        self.order = Order.objects.create(order_no='summary', boss_user=self.boss,
            package=self.order.package, required_players=1, surcharge_guarded=True)
        self.url = '/api/boss/orders/summary/surcharge/'
        for no, status, diamonds, refunded in [('a', 'paid', 100, 0), ('b', 'unknown', 50, 0), ('c', 'partially_refunded', 80, 30)]:
            OrderSurcharge.objects.create(surcharge_no=no, order=self.order, boss=self.boss, status=status,
                amount_diamonds=diamonds, amount_yuan=Decimal(diamonds) / 10, refunded_diamonds=refunded,
                idempotency_key=no, request_digest='a'*64)
        self.login(self.boss)
        data = self.client.get(self.url).json()
        self.assertEqual(data['paid_diamonds'], 150)
        self.assertEqual(data['processing_diamonds'], 50)
        self.assertEqual(data['refunded_diamonds'], 30)
        self.assertIn('PAYMENT_PENDING', data['blockers'])
        self.assertNotIn('commission', str(data))
        self.assertNotIn('request_digest', str(data))

    @override_settings(ORDER_SURCHARGE_ENABLED=True)
    def test_unapproved_enable_flag_does_not_open_sending_and_structural_rules(self):
        self.login(self.boss)
        self.assertFalse(self.client.get(self.url).json()['can_submit'])
        player = Player.objects.create(name='p', player_type=PlayerType.objects.create(name='t', priority=1))
        OrderPlayer.objects.create(order=self.order, player=player)
        data = self.client.get(self.url).json()
        self.assertFalse(data['structurally_eligible'])
        self.assertIn('ORDER_ALREADY_ACCEPTED', data['blockers'])
        self.order.status = Order.STATUS_CANCELLED
        self.order.save(update_fields=['status'])
        self.assertIn('ORDER_NOT_WAITING', self.client.get(self.url).json()['blockers'])
