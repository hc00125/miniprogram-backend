from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings

from apps.catalog.models import Package, PackageSpec, PlayerType
from apps.players.escort_models import OrderEscortRequirementSnapshot
from apps.players.models import Player

from .admin import OrderAdmin, OrderDesignationAdmin
from .models import Order, OrderDesignation


class OrderAdminDeletePermissionTests(TestCase):
    """订单物理删除只用于开发测试，生产环境必须完全关闭。"""

    def setUp(self):
        self.site = AdminSite()
        self.order_admin = OrderAdmin(Order, self.site)
        self.designation_admin = OrderDesignationAdmin(OrderDesignation, self.site)
        self.factory = RequestFactory()
        self.superuser = User.objects.create_superuser(
            username='order-delete-admin',
            email='admin@example.com',
            password='test-password',
        )
        self.staff = User.objects.create_user(
            username='order-delete-staff',
            password='test-password',
            is_staff=True,
        )
        self.player_type = PlayerType.objects.create(name='删除测试娱乐陪', priority=801)
        self.package = Package.objects.create(
            name='订单删除测试商品',
            base_price=15,
            player_count=1,
            is_active=True,
        )
        self.spec = PackageSpec.objects.create(
            package=self.package,
            name='订单删除测试规格',
            price=15,
            required_player_type=self.player_type,
            is_active=True,
        )
        player_user = User.objects.create_user(username='order-delete-player')
        self.player = Player.objects.create(
            user=player_user,
            name='订单删除测试陪玩',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )

    def request_for(self, user):
        request = self.factory.get('/admin/orders/order/')
        request.user = user
        return request

    def create_order_with_internal_records(self, order_no):
        order = Order.objects.create(
            order_no=order_no,
            boss_wechat='delete-test-openid',
            package=self.package,
            spec_id=self.spec.id,
            required_players=1,
            total_price_per_hour=15,
            total_amount=15,
            status=Order.STATUS_WAITING,
            paid=False,
        )
        snapshot = OrderEscortRequirementSnapshot.objects.create(
            order=order,
            requires_escort_qualification=False,
            source_package_id=self.package.id,
            source_package_name=self.package.name,
        )
        designation = OrderDesignation.objects.create(
            order=order,
            player=self.player,
            expires_at=order.created_at,
        )
        return order, snapshot, designation

    @override_settings(ADMIN_ORDER_DELETE_ENABLED=True)
    def test_superuser_can_delete_order_and_internal_records_when_switch_enabled(self):
        request = self.request_for(self.superuser)
        order, snapshot, designation = self.create_order_with_internal_records('ADMINDELETE001')

        self.assertTrue(self.order_admin.has_delete_permission(request, order))
        self.assertTrue(self.designation_admin.has_delete_permission(request, designation))
        self.assertIn('delete_selected', self.order_admin.get_actions(request))

        self.order_admin.delete_queryset(request, Order.objects.filter(pk=order.pk))

        self.assertFalse(Order.objects.filter(pk=order.pk).exists())
        self.assertFalse(OrderEscortRequirementSnapshot.objects.filter(pk=snapshot.pk).exists())
        self.assertFalse(OrderDesignation.objects.filter(pk=designation.pk).exists())

    @override_settings(ADMIN_ORDER_DELETE_ENABLED=False)
    def test_superuser_cannot_delete_order_when_switch_disabled(self):
        request = self.request_for(self.superuser)
        order, _, designation = self.create_order_with_internal_records('ADMINDELETE002')

        self.assertFalse(self.order_admin.has_delete_permission(request, order))
        self.assertFalse(self.designation_admin.has_delete_permission(request, designation))
        self.assertNotIn('delete_selected', self.order_admin.get_actions(request))

    @override_settings(ADMIN_ORDER_DELETE_ENABLED=True)
    def test_non_superuser_cannot_delete_even_when_switch_enabled(self):
        request = self.request_for(self.staff)
        order, _, designation = self.create_order_with_internal_records('ADMINDELETE003')

        self.assertFalse(self.order_admin.has_delete_permission(request, order))
        self.assertFalse(self.designation_admin.has_delete_permission(request, designation))
        self.assertNotIn('delete_selected', self.order_admin.get_actions(request))
