from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase

from apps.catalog.models import Package
from apps.orders.models import Order
from apps.payments.models import Payment, Refund

from .models import BossConsumptionLedger, ClientProfile, VipTier
from .serializers import ClientProfileSerializer
from .vip import create_manual_consumption_adjustment


class BossConsumptionVipTests(TestCase):
    def setUp(self):
        self.member, _ = VipTier.objects.update_or_create(
            code='member',
            defaults={
                'name': '普通会员',
                'min_consumption': Decimal('0.00'),
                'benefits': ['会员标识'],
                'is_active': True,
            },
        )
        self.silver, _ = VipTier.objects.update_or_create(
            code='silver',
            defaults={
                'name': '白银VIP',
                'min_consumption': Decimal('300.00'),
                'benefits': ['优先客服'],
                'is_active': True,
            },
        )
        self.user = User.objects.create_user(username='vip-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='vip-openid',
            nickname='VIP老板',
            vip_tier=self.member,
        )
        self.package = Package.objects.create(name='VIP测试套餐', base_price=350, player_count=1)
        self.order = Order.objects.create(
            order_no='VIPORDER001',
            boss_user=self.user,
            boss_wechat=self.profile.openid,
            package=self.package,
            required_players=1,
            total_price_per_hour=350,
            total_amount=350,
            status=Order.STATUS_IN_PROGRESS,
            paid=True,
        )

    def complete_order(self):
        self.order.status = Order.STATUS_COMPLETED
        self.order.save(update_fields=['status'])
        self.profile.refresh_from_db()

    def test_completed_order_creates_ledger_and_upgrades_vip(self):
        self.complete_order()
        self.assertEqual(self.profile.cumulative_consumption, Decimal('350.00'))
        self.assertEqual(self.profile.vip_tier, self.silver)
        ledger = BossConsumptionLedger.objects.get(reference_id=self.order.order_no)
        self.assertEqual(ledger.amount, Decimal('350.00'))
        self.assertEqual(ledger.balance_after, Decimal('350.00'))

    def test_completed_order_signal_is_idempotent(self):
        self.complete_order()
        self.order.save(update_fields=['status'])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.cumulative_consumption, Decimal('350.00'))
        self.assertEqual(BossConsumptionLedger.objects.filter(reference_id=self.order.order_no).count(), 1)

    def test_successful_refund_reduces_consumption_and_vip(self):
        self.complete_order()
        payment = Payment.objects.create(
            payment_no='VIPPAY001',
            order=self.order,
            channel='wechat',
            scene='virtual',
            amount=350,
            status='paid',
        )
        Refund.objects.create(
            refund_no='VIPREF001',
            payment=payment,
            order=self.order,
            amount=100,
            status=Refund.STATUS_SUCCEEDED,
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.cumulative_consumption, Decimal('250.00'))
        self.assertEqual(self.profile.vip_tier, self.member)
        self.assertEqual(BossConsumptionLedger.objects.get(reference_id='VIPREF001').amount, Decimal('-100.00'))

    def test_manual_adjustment_is_auditable(self):
        operator = User.objects.create_user(username='ops', is_staff=True)
        entry = create_manual_consumption_adjustment(
            profile=self.profile,
            amount=Decimal('88.00'),
            reason='线下活动补录',
            operator=operator,
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.cumulative_consumption, Decimal('88.00'))
        self.assertEqual(entry.operator, operator)
        self.assertEqual(entry.reason, '线下活动补录')

    def test_profile_serializer_exposes_vip_progress(self):
        self.complete_order()
        payload = ClientProfileSerializer(self.profile).data
        self.assertEqual(payload['vip']['current_tier']['code'], 'silver')
        self.assertEqual(payload['cumulative_consumption'], '350.00')
        self.assertIn('progress_percent', payload['vip'])


class AdminRoleBootstrapTests(TestCase):
    def test_roles_are_created_idempotently(self):
        call_command('bootstrap_admin_roles')
        call_command('bootstrap_admin_roles')
        expected = {'运营管理员', '财务管理员', '客服管理员', '内容管理员', '只读审计'}
        self.assertEqual(set(Group.objects.filter(name__in=expected).values_list('name', flat=True)), expected)
        self.assertTrue(Group.objects.get(name='财务管理员').permissions.exists())
