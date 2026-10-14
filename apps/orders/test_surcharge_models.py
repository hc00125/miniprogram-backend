from decimal import Decimal
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from apps.catalog.models import Package
from apps.orders.models import Order


class SurchargeModelTests(TestCase):
    def setUp(self):
        self.boss = get_user_model().objects.create_user('boss')
        self.order = Order.objects.create(order_no='o1', boss_user=self.boss, boss_wechat='synthetic',
            package=Package.objects.create(name='test', base_price=10), required_players=1, surcharge_guarded=True)

    def test_surcharge_financial_admin_read_only(self):
        staff = get_user_model().objects.create_superuser('auditor', password='test')
        self.client.force_login(staff)
        self.assertEqual(self.client.get('/admin/orders/ordersurcharge/').status_code, 200)
        self.assertEqual(self.client.get('/admin/orders/ordersurcharge/add/').status_code, 403)

    def test_record_money_bounds_idempotency_and_no_original_order_mutation(self):
        self.assertTrue(any(m.__name__ == 'OrderSurcharge' for m in apps.get_app_config('orders').get_models()))
        Surcharge = apps.get_model('orders', 'OrderSurcharge')
        data = dict(surcharge_no='s1', order=self.order, boss=self.boss, amount_diamonds=10,
                    amount_yuan=Decimal('1.00'), idempotency_key='key', request_digest='a'*64)
        surcharge = Surcharge.objects.create(**data)
        self.assertEqual(surcharge.status, 'created')
        self.assertIsNone(surcharge.attempt_reference)
        self.order.refresh_from_db()
        self.assertFalse(self.order.paid)
        self.assertIsNone(self.order.total_amount)
        for changes in ({'amount_diamonds': 0}, {'amount_yuan': 2}, {'refunded_diamonds': 11}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Surcharge.objects.filter(pk=surcharge.pk).update(**changes)
        data['surcharge_no'] = 's2'
        with self.assertRaises(IntegrityError), transaction.atomic():
            Surcharge.objects.bulk_create([Surcharge(**data)])
        for amount in (True, 1.5):
            surcharge.amount_diamonds = amount
            with self.assertRaises(ValidationError):
                surcharge.save()
