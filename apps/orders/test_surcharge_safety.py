from decimal import Decimal
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient
from apps.orders.models import Order
from apps.orders.surcharge_models import OrderSurcharge
from apps.payments.models import Payment, Refund

class CancellationSafetyTests(TransactionTestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from apps.accounts.models import ClientProfile
        from apps.wallet.models import ClientWallet
        from apps.catalog.models import Package
        self.user = User.objects.create_user('cancel-safety')
        profile = ClientProfile.objects.create(user=self.user, openid='cancel-safety')
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=profile, defaults={'balance': Decimal('8')})
        self.order = Order.objects.create(order_no='cancel-safety', boss_user=self.user, package=Package.objects.create(name='test', base_price=10), required_players=1, paid=True)
        self.payment = Payment.objects.create(payment_no='cancel-safety', order=self.order, channel='balance', scene='balance', amount=Decimal('10'), status='paid')
    def add_paid_surcharge(self):
        # Marker is fixed at INSERT, never promote an already-visible old order.
        self.order = Order.objects.create(order_no='guarded-safety', boss_user=self.user,
            package=self.order.package, required_players=1, paid=True, surcharge_guarded=True)
        self.payment.order = self.order
        self.payment.save(update_fields=['order'])
        OrderSurcharge.objects.create(order=self.order, boss=self.user, amount_diamonds=20, amount_yuan=Decimal('2'), status='paid', surcharge_no='SC-safety', idempotency_key='test', request_digest='a'*64)

    def test_paid_surcharge_cannot_grab_until_lifecycle_is_implemented(self):
        self.add_paid_surcharge()
        from apps.orders.services import grab_order
        from apps.players.models import Player
        from apps.catalog.models import PlayerType
        from rest_framework.exceptions import ValidationError
        player=Player.objects.create(name='unconfigured',player_type=PlayerType.objects.create(name='type',priority=1))
        with self.assertRaises(ValidationError) as caught:
            grab_order(self.order.order_no,player)
        self.assertEqual(str(caught.exception.detail['code']),'SURCHARGE_ALLOCATION_UNCONFIRMED')
        self.assertFalse(self.order.order_players.exists())

    def test_old_order_cancel_still_refunds_once(self):
        self.order.surcharges.all().delete()
        client=APIClient(); client.force_authenticate(self.user)
        response=client.post('/api/boss/order/'+self.order.order_no+'/cancel',{},format='json')
        self.assertEqual(response.status_code,200)
        self.payment.refresh_from_db(); self.wallet.refresh_from_db(); self.order.refresh_from_db()
        self.assertEqual(self.payment.status,'refunded')
        self.assertEqual(self.wallet.balance,Decimal('18'))
        self.assertEqual(self.order.status,Order.STATUS_CANCELLED)
        self.assertEqual(Refund.objects.count(),1)
        client.post('/api/boss/order/'+self.order.order_no+'/cancel',{},format='json')
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance,Decimal('18'))

    def test_old_order_local_refund_rolls_back_if_final_status_write_fails(self):
        from unittest.mock import patch
        self.order.surcharges.all().delete()
        client=APIClient(); client.force_authenticate(self.user)
        original=Order.save
        def fail_cancel(order,*args,**kwargs):
            if order.status==Order.STATUS_CANCELLED: raise RuntimeError('simulated final write failure')
            return original(order,*args,**kwargs)
        with patch.object(Order,'save',fail_cancel),self.assertRaises(RuntimeError):
            client.post('/api/boss/order/'+self.order.order_no+'/cancel',{},format='json')
        self.payment.refresh_from_db(); self.wallet.refresh_from_db(); self.order.refresh_from_db()
        self.assertEqual(self.payment.status,'paid')
        self.assertEqual(self.wallet.balance,Decimal('8'))
        self.assertEqual(self.order.status,Order.STATUS_WAITING)
        self.assertFalse(Refund.objects.exists())

    def test_cancel_rejects_before_any_refund_with_notifications_on_or_off(self):
        self.add_paid_surcharge()
        client = APIClient(); client.force_authenticate(self.user)
        for enabled in (False, True):
            with self.subTest(enabled=enabled), override_settings(KOOK_ENABLED=enabled, KOOK_ORDER_EVENTS_ENABLED=enabled):
                response = client.post('/api/boss/order/'+self.order.order_no+'/cancel', {}, format='json')
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data['code'], 'SURCHARGE_REFUND_REQUIRES_REVIEW')
                self.payment.refresh_from_db(); self.wallet.refresh_from_db(); self.order.refresh_from_db()
                self.assertEqual(self.payment.status, 'paid')
                self.assertEqual(self.wallet.balance, Decimal('8'))
                self.assertEqual(self.order.status, Order.STATUS_WAITING)
                self.assertFalse(Refund.objects.exists())
