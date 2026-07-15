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
        self.mouse = VipTier.objects.get(code='mouse')
        self.bronze = VipTier.objects.get(code='bronze_mouse')
        self.silver = VipTier.objects.get(code='silver_mouse')
        self.user = User.objects.create_user(username='vip-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='vip-openid',
            nickname='VIP老板',
            vip_tier=self.mouse,
        )
        self.package = Package.objects.create(name='VIP测试套餐', base_price=2500, player_count=1)
        self.order = Order.objects.create(
            order_no='VIPORDER001',
            boss_user=self.user,
            boss_wechat=self.profile.openid,
            package=self.package,
            required_players=1,
            total_price_per_hour=2500,
            total_amount=2500,
            status=Order.STATUS_IN_PROGRESS,
            paid=True,
        )

    def complete_order(self):
        self.order.status = Order.STATUS_COMPLETED
        self.order.save(update_fields=['status'])
        self.profile.refresh_from_db()

    def test_mouse_vip_tiers_are_seeded(self):
        expected = [
            ('mouse', '鼠鼠', Decimal('0.00')),
            ('bronze_mouse', '青铜鼠鼠', Decimal('1.00')),
            ('silver_mouse', '白银鼠鼠', Decimal('2000.00')),
            ('gold_mouse', '黄金鼠鼠', Decimal('5000.00')),
            ('platinum_mouse', '铂金鼠鼠', Decimal('10000.00')),
            ('emerald_mouse', '翡翠鼠鼠', Decimal('50000.00')),
            ('diamond_mouse', '钻石鼠鼠', Decimal('100000.00')),
            ('glory_mouse', '荣耀鼠鼠', Decimal('200000.00')),
            ('brilliant_mouse', '璀璨鼠鼠', Decimal('350000.00')),
            ('dream_mouse', '梦幻鼠鼠', Decimal('500000.00')),
            ('elegant_mouse', '绮丽鼠鼠', Decimal('750000.00')),
            ('supreme_mouse', '至臻鼠鼠', Decimal('1000000.00')),
        ]
        actual = list(
            VipTier.objects
            .filter(is_active=True)
            .order_by('min_consumption', 'sort_order', 'id')
            .values_list('code', 'name', 'min_consumption')
        )
        self.assertEqual(actual, expected)

    def test_completed_order_creates_ledger_and_upgrades_vip(self):
        self.complete_order()
        self.assertEqual(self.profile.cumulative_consumption, Decimal('2500.00'))
        self.assertEqual(self.profile.vip_tier, self.silver)
        ledger = BossConsumptionLedger.objects.get(reference_id=self.order.order_no)
        self.assertEqual(ledger.amount, Decimal('2500.00'))
        self.assertEqual(ledger.balance_after, Decimal('2500.00'))

    def test_completed_order_signal_is_idempotent(self):
        self.complete_order()
        self.order.save(update_fields=['status'])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.cumulative_consumption, Decimal('2500.00'))
        self.assertEqual(BossConsumptionLedger.objects.filter(reference_id=self.order.order_no).count(), 1)

    def test_successful_refund_reduces_consumption_and_vip(self):
        self.complete_order()
        payment = Payment.objects.create(
            payment_no='VIPPAY001',
            order=self.order,
            channel='wechat',
            scene='virtual',
            amount=2500,
            status='paid',
        )
        Refund.objects.create(
            refund_no='VIPREF001',
            payment=payment,
            order=self.order,
            amount=1000,
            status=Refund.STATUS_SUCCEEDED,
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.cumulative_consumption, Decimal('1500.00'))
        self.assertEqual(self.profile.vip_tier, self.bronze)
        self.assertEqual(BossConsumptionLedger.objects.get(reference_id='VIPREF001').amount, Decimal('-1000.00'))

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
        self.assertEqual(self.profile.vip_tier, self.bronze)
        self.assertEqual(entry.operator, operator)
        self.assertEqual(entry.reason, '线下活动补录')

    def test_profile_serializer_exposes_vip_progress(self):
        self.complete_order()
        payload = ClientProfileSerializer(self.profile).data
        self.assertEqual(payload['vip']['current_tier']['code'], 'silver_mouse')
        self.assertEqual(Decimal(str(payload['cumulative_consumption'])), Decimal('2500.00'))
        self.assertIn('progress_percent', payload['vip'])


class AdminRoleBootstrapTests(TestCase):
    def test_roles_are_created_idempotently(self):
        call_command('bootstrap_admin_roles')
        call_command('bootstrap_admin_roles')
        expected = {'运营管理员', '财务管理员', '客服管理员', '内容管理员', '只读审计'}
        self.assertEqual(set(Group.objects.filter(name__in=expected).values_list('name', flat=True)), expected)
        self.assertTrue(Group.objects.get(name='财务管理员').permissions.exists())
