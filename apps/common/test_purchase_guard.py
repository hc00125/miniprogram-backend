from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient


@override_settings(IOS_PURCHASE_ENABLED=False)
class IOSPurchaseGuardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ios-purchase-guard-user')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def assert_ios_blocked(self, path):
        response = self.client.post(
            path,
            {},
            format='json',
            HTTP_X_CLIENT_PLATFORM='ios',
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(str(response.data.get('code')), 'IOS_PURCHASE_DISABLED')
        self.assertEqual(str(response.data.get('detail')), 'iOS端当前暂不提供在线购买')

    def test_ios_cannot_create_orders_recharges_or_external_payments(self):
        paths = [
            '/api/boss/order',
            '/api/boss/listing-order',
            '/api/boss/orders/batch',
            '/api/boss/order/TEST-ORDER/renew',
            '/api/client/wallet/recharge/create',
            '/api/pay/create',
            '/api/pay/wechat/miniprogram/create',
            '/api/pay/wechat/virtual/create',
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assert_ios_blocked(path)

    def test_ios_existing_diamond_balance_payment_is_not_blocked_by_purchase_guard(self):
        response = self.client.post(
            '/api/pay/balance/create',
            {},
            format='json',
            HTTP_X_CLIENT_PLATFORM='ios',
        )
        # The endpoint may reject the empty request for business reasons, but it must
        # not be rejected by the iOS external-purchase guard.
        self.assertNotEqual(response.status_code, 403, response.data)
        self.assertNotEqual(str(response.data.get('code')), 'IOS_PURCHASE_DISABLED')

    def test_iphone_user_agent_is_blocked_for_older_clients(self):
        response = self.client.post(
            '/api/pay/wechat/virtual/create',
            {},
            format='json',
            HTTP_USER_AGENT='Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) MicroMessenger',
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(str(response.data.get('code')), 'IOS_PURCHASE_DISABLED')

    def test_iphone_user_agent_overrides_android_header(self):
        response = self.client.post(
            '/api/pay/wechat/virtual/create',
            {},
            format='json',
            HTTP_X_CLIENT_PLATFORM='android',
            HTTP_USER_AGENT='Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) MicroMessenger',
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(str(response.data.get('code')), 'IOS_PURCHASE_DISABLED')

    def test_ios_can_still_read_existing_orders(self):
        response = self.client.get(
            '/api/boss/orders/me',
            HTTP_X_CLIENT_PLATFORM='ios',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, [])

    def test_android_is_not_blocked_by_ios_guard(self):
        response = self.client.post(
            '/api/pay/wechat/virtual/create',
            {},
            format='json',
            HTTP_X_CLIENT_PLATFORM='android',
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertNotEqual(str(response.data.get('code')), 'IOS_PURCHASE_DISABLED')

    @override_settings(IOS_PURCHASE_ENABLED=True)
    def test_switch_can_restore_ios_purchase(self):
        response = self.client.post(
            '/api/pay/wechat/virtual/create',
            {},
            format='json',
            HTTP_X_CLIENT_PLATFORM='ios',
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertNotEqual(str(response.data.get('code')), 'IOS_PURCHASE_DISABLED')
