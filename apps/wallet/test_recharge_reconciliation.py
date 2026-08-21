from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.payments.virtualpay import VirtualPaymentAPIError

from .coin_recharge import reconcile_coin_recharge_order
from .models import RechargeOrder


VIRTUAL_SETTINGS = {
    'WECHAT_VIRTUALPAY_ENABLED': True,
    'WECHAT_APP_ID': 'wx_test_appid',
    'WECHAT_APP_SECRET': 'test_secret',
    'WECHAT_VIRTUALPAY_OFFER_ID': 'test_offer',
    'WECHAT_VIRTUALPAY_APP_KEY': 'test_app_key',
    'WECHAT_VIRTUALPAY_ENV': 0,
    'WECHAT_VIRTUALPAY_HTTP_TIMEOUT': 10,
    'WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN': 100,
    'ENABLE_MOCK_PAYMENT': False,
}


@override_settings(**VIRTUAL_SETTINGS)
class RechargeReconciliationSafetyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='recharge-reconcile-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_recharge_reconcile',
            nickname='充值对账测试',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _recharge(self, number, *, expired):
        now = timezone.now()
        return RechargeOrder.objects.create(
            recharge_no=number,
            profile=self.profile,
            amount=Decimal('12.35'),
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_PAYING,
            expires_at=(now - timedelta(minutes=1)) if expired else (now + timedelta(minutes=5)),
            notify_payload={
                'mode': 'short_series_coin',
                'wechat_coin_units_per_yuan': 100,
            },
        )

    def test_fresh_paying_recharge_is_never_closed_by_reconcile_helper(self):
        recharge = self._recharge('RCGFRESHSAFE001', expired=False)
        with patch('apps.wallet.coin_recharge.xpay_post') as xpay:
            outcome = reconcile_coin_recharge_order(recharge)
        self.assertEqual(outcome, 'skipped')
        xpay.assert_not_called()
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, RechargeOrder.STATUS_PAYING)

    def test_expired_remote_unpaid_recharge_can_close_after_successful_query(self):
        recharge = self._recharge('RCGEXPIREDUNPAID1', expired=True)
        with patch(
            'apps.wallet.coin_recharge.xpay_post',
            return_value={'errcode': 0, 'order': {'status': 0}},
        ):
            outcome = reconcile_coin_recharge_order(recharge)
        self.assertEqual(outcome, 'closed')
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, RechargeOrder.STATUS_CLOSED)

    def test_expired_recharge_stays_paying_when_remote_state_is_unknown(self):
        recharge = self._recharge('RCGEXPIREDUNKNOWN', expired=True)
        with patch(
            'apps.wallet.coin_recharge.xpay_post',
            side_effect=VirtualPaymentAPIError('network_error', 'temporary failure'),
        ):
            outcome = reconcile_coin_recharge_order(recharge)
        self.assertEqual(outcome, 'skipped')
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, RechargeOrder.STATUS_PAYING)

    def test_user_cancel_does_not_close_when_remote_query_fails(self):
        recharge = self._recharge('RCGCANCELUNKNOWN1', expired=True)
        with patch(
            'apps.wallet.recharge_actions._query_current_status',
            side_effect=VirtualPaymentAPIError('network_error', 'temporary failure'),
        ):
            response = self.client.post(
                f'/api/client/wallet/recharge/cancel/{recharge.recharge_no}',
                {},
                format='json',
            )

        self.assertGreaterEqual(response.status_code, 400)
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, RechargeOrder.STATUS_PAYING)
        self.assertTrue(recharge.notify_payload['reconciliation_required'])
