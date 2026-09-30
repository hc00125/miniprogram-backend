from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import Package
from apps.orders.models import Order
from apps.payments.models import Payment, Refund

from .models import BossConsumptionLedger, ClientProfile, ClientVipKookRoom, VipTier
from .serializers import ClientProfileSerializer
from .vip import PRIVATE_KOOK_ROOM_FEATURE, create_manual_consumption_adjustment


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
        self.assertNotIn(PRIVATE_KOOK_ROOM_FEATURE, self.bronze.feature_codes)
        self.assertIn(PRIVATE_KOOK_ROOM_FEATURE, self.silver.feature_codes)
        self.assertIn('专属KOOK房间', self.silver.benefits)

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

    def test_profile_serializer_exposes_vip_progress_and_room_state(self):
        self.complete_order()
        payload = ClientProfileSerializer(self.profile).data
        self.assertEqual(payload['vip']['current_tier']['code'], 'silver_mouse')
        self.assertEqual(Decimal(str(payload['cumulative_consumption'])), Decimal('2500.00'))
        self.assertIn('progress_percent', payload['vip'])
        self.assertEqual(payload['vip']['private_kook_room']['status'], 'pending_configuration')
        self.assertTrue(payload['vip']['private_kook_room']['unlocked'])


class VipKookRoomTests(TestCase):
    def setUp(self):
        self.bronze = VipTier.objects.get(code='bronze_mouse')
        self.silver = VipTier.objects.get(code='silver_mouse')
        self.user = User.objects.create_user(username='vip-room-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='vip-room-openid',
            nickname='专属房老板',
            cumulative_consumption=Decimal('2500.00'),
            vip_tier=self.silver,
        )
        self.superuser = User.objects.create_superuser(
            username='vip-room-admin',
            email='admin@example.com',
            password='test-password',
        )
        self.package = Package.objects.create(name='专属房测试套餐', base_price=100, player_count=1)
        self.room = ClientVipKookRoom.objects.create(
            profile=self.profile,
            kook_room_number='TC-VIP-20000',
            is_active=True,
            assigned_by=self.superuser,
            assigned_at=timezone.now(),
        )

    def create_order(self, order_no):
        return Order.objects.create(
            order_no=order_no,
            boss_user=self.user,
            boss_wechat=self.profile.openid,
            package=self.package,
            required_players=1,
            total_price_per_hour=100,
            total_amount=100,
            status=Order.STATUS_WAITING,
            paid=False,
        )

    def test_eligible_new_order_copies_dedicated_room_snapshot(self):
        order = self.create_order('VIPROOMORDER001')
        order.refresh_from_db()
        self.assertEqual(order.kook_room_number, 'TC-VIP-20000')
        self.assertEqual(order.kook_room_updated_by, self.superuser)
        self.assertIsNotNone(order.kook_room_updated_at)

        self.room.kook_room_number = 'TC-VIP-NEW'
        self.room.save(update_fields=['kook_room_number', 'updated_at'])
        order.refresh_from_db()
        self.assertEqual(order.kook_room_number, 'TC-VIP-20000')

    def test_room_record_is_retained_but_new_orders_stop_using_it_after_downgrade(self):
        create_manual_consumption_adjustment(
            profile=self.profile,
            amount=Decimal('-1000.00'),
            reason='退款后降级测试',
            operator=self.superuser,
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.vip_tier, self.bronze)
        self.assertTrue(ClientVipKookRoom.objects.filter(profile=self.profile).exists())

        order = self.create_order('VIPROOMORDER002')
        order.refresh_from_db()
        self.assertEqual(order.kook_room_number, '')

        room_payload = ClientProfileSerializer(self.profile).data['vip']['private_kook_room']
        self.assertEqual(room_payload['status'], 'locked')
        self.assertEqual(room_payload['room_number'], 'TC-VIP-20000')

    def test_inactive_room_is_visible_but_not_applied(self):
        self.room.is_active = False
        self.room.save(update_fields=['is_active', 'updated_at'])

        order = self.create_order('VIPROOMORDER003')
        order.refresh_from_db()
        self.assertEqual(order.kook_room_number, '')

        room_payload = ClientProfileSerializer(self.profile).data['vip']['private_kook_room']
        self.assertEqual(room_payload['status'], 'disabled')
        self.assertTrue(room_payload['configured'])
        self.assertFalse(room_payload['available'])


class AdminRoleBootstrapTests(TestCase):
    def test_roles_are_created_idempotently(self):
        call_command('bootstrap_admin_roles')
        call_command('bootstrap_admin_roles')
        expected = {'运营管理员', '财务管理员', '客服管理员', '内容管理员', '只读审计'}
        self.assertEqual(set(Group.objects.filter(name__in=expected).values_list('name', flat=True)), expected)
        self.assertTrue(Group.objects.get(name='财务管理员').permissions.exists())
