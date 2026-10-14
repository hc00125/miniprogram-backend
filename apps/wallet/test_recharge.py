from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.payments.virtualpay import VirtualPaymentAPIError, VirtualPaymentError

from .models import ClientWallet, ClientWalletLedger, RechargeOrder, RechargeProduct
from .services import (
    RECHARGE_EXPIRE_MINUTES,
    create_recharge,
    credit_recharge,
    get_or_lock_wallet,
    mark_recharge_paid,
    query_recharge,
    write_wallet_ledger,
)


VIRTUAL_SETTINGS = {
    'WECHAT_VIRTUALPAY_ENABLED': True,
    'WECHAT_APP_ID': 'wx_test_appid',
    'WECHAT_APP_SECRET': 'test_secret',
    'WECHAT_VIRTUALPAY_OFFER_ID': 'test_offer',
    'WECHAT_VIRTUALPAY_APP_KEY': 'test_app_key',
    'WECHAT_VIRTUALPAY_ENV': 0,
    'WECHAT_VIRTUALPAY_HTTP_TIMEOUT': 10,
}

OPENID = 'openid_recharge_boss'


def paid_query_response(fee, order_id='WX_RCG_001'):
    return {
        'errcode': 0,
        'errmsg': 'OK',
        'order': {'status': 3, 'order_fee': fee, 'paid_fee': fee, 'wx_order_id': order_id},
    }


def unpaid_query_response():
    return {
        'errcode': 0,
        'errmsg': 'OK',
        'order': {'status': 1, 'order_fee': 3000, 'paid_fee': 0},
    }


@override_settings(**VIRTUAL_SETTINGS)
class RechargeCreditTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='recharge-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid=OPENID,
            nickname='充值测试老板',
        )
        self.product = RechargeProduct.objects.create(
            amount=Decimal('30.00'),
            product_id='test_rcg_30',
            goods_price_fen=3000,
            is_active=True,
        )

    def _create_recharge(self):
        with patch(
            'apps.wallet.services.exchange_code_for_session',
            return_value=(OPENID, 'session-key'),
        ):
            return create_recharge(self.user, self.product.id, 'test-code')

    def test_create_then_query_paid_credits_exactly_once(self):
        recharge, payload = self._create_recharge()
        self.assertEqual(recharge.status, RechargeOrder.STATUS_PAYING)
        self.assertEqual(payload['payment_no'], recharge.recharge_no)
        self.assertEqual(payload['recharge_no'], recharge.recharge_no)
        self.assertEqual(payload['amount'], '30.00')
        self.assertEqual(payload['product_id'], 'test_rcg_30')
        self.assertTrue(recharge.recharge_no.startswith('RCG'))

        with patch(
            'apps.wallet.services.xpay_post',
            side_effect=[paid_query_response(3000), {'errcode': 0, 'errmsg': 'OK'}],
        ) as mocked_xpay:
            synced = query_recharge(recharge.recharge_no, self.user)

        self.assertEqual(synced.status, RechargeOrder.STATUS_CREDITED)
        self.assertIsNotNone(synced.credited_at)
        self.assertEqual(synced.third_trade_no, 'WX_RCG_001')
        self.assertEqual(mocked_xpay.call_count, 2)

        wallet = ClientWallet.objects.get(profile=self.profile)
        self.assertEqual(wallet.balance, Decimal('30.00'))
        self.assertEqual(wallet.recharged_total, Decimal('30.00'))
        self.assertEqual(ClientWalletLedger.objects.count(), 1)
        entry = ClientWalletLedger.objects.get()
        self.assertEqual(entry.entry_type, ClientWalletLedger.TYPE_RECHARGE)
        self.assertEqual(entry.amount, Decimal('30.00'))
        self.assertEqual(entry.balance_after, Decimal('30.00'))
        self.assertEqual(entry.reference_id, recharge.recharge_no)

        # 回调重放模拟：再次查询已入账的充值单，不触发微信查单，也不重复入账。
        with patch('apps.wallet.services.xpay_post') as mocked_replay:
            replayed = query_recharge(recharge.recharge_no, self.user)
        self.assertEqual(replayed.status, RechargeOrder.STATUS_CREDITED)
        mocked_replay.assert_not_called()
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal('30.00'))
        self.assertEqual(ClientWalletLedger.objects.count(), 1)

    def test_create_reuses_unexpired_paying_recharge(self):
        recharge, _payload = self._create_recharge()
        reused, _payload2 = self._create_recharge()
        self.assertEqual(reused.pk, recharge.pk)
        self.assertEqual(RechargeOrder.objects.count(), 1)

    def test_expired_recharge_closed_only_after_remote_confirms_unpaid(self):
        recharge, _payload = self._create_recharge()
        RechargeOrder.objects.filter(pk=recharge.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        with patch(
            'apps.wallet.services.xpay_post',
            return_value=unpaid_query_response(),
        ):
            replacement, _payload2 = self._create_recharge()

        recharge.refresh_from_db()
        self.assertEqual(recharge.status, RechargeOrder.STATUS_CLOSED)
        self.assertNotEqual(replacement.pk, recharge.pk)
        self.assertEqual(replacement.status, RechargeOrder.STATUS_PAYING)

    def test_concurrent_ledger_write_hits_unique_constraint_credits_once(self):
        with transaction.atomic():
            wallet = get_or_lock_wallet(self.profile)
            entry1, created1 = write_wallet_ledger(
                wallet,
                ClientWalletLedger.TYPE_RECHARGE,
                Decimal('30.00'),
                reference_type='recharge_order',
                reference_id='RCGRACE0001',
            )
            self.assertTrue(created1)

            # 模拟并发竞态：绕过判重预检查，让第二次写入直接撞唯一约束。
            with patch(
                'apps.wallet.services._existing_ledger_entry',
                side_effect=[None, entry1],
            ):
                entry2, created2 = write_wallet_ledger(
                    wallet,
                    ClientWalletLedger.TYPE_RECHARGE,
                    Decimal('30.00'),
                    reference_type='recharge_order',
                    reference_id='RCGRACE0001',
                )
            self.assertFalse(created2)
            self.assertEqual(entry2.pk, entry1.pk)

        wallet = ClientWallet.objects.get(profile=self.profile)
        self.assertEqual(wallet.balance, Decimal('30.00'))
        self.assertEqual(ClientWalletLedger.objects.count(), 1)

    def test_credit_replay_after_bypassing_marker_does_not_double_credit(self):
        recharge = RechargeOrder.objects.create(
            recharge_no='RCGREPLAY0001',
            profile=self.profile,
            product=self.product,
            amount=Decimal('30.00'),
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_PAYING,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        mark_recharge_paid(recharge, 'WX_RCG_REPLAY')

        # 绕过 credited_at once-only 标记，直接再次入账。
        RechargeOrder.objects.filter(pk=recharge.pk).update(
            credited_at=None,
            status=RechargeOrder.STATUS_PAID,
        )
        recharge.refresh_from_db()
        with transaction.atomic():
            credit_recharge(recharge)

        wallet = ClientWallet.objects.get(profile=self.profile)
        self.assertEqual(wallet.balance, Decimal('30.00'))
        self.assertEqual(wallet.recharged_total, Decimal('30.00'))
        self.assertEqual(ClientWalletLedger.objects.count(), 1)

    def test_amount_mismatch_rejects_credit(self):
        recharge, _payload = self._create_recharge()

        with patch(
            'apps.wallet.services.xpay_post',
            return_value=paid_query_response(9999),
        ):
            with self.assertRaises(VirtualPaymentError):
                query_recharge(recharge.recharge_no, self.user)

        recharge.refresh_from_db()
        self.assertEqual(recharge.status, RechargeOrder.STATUS_PAYING)
        self.assertIsNone(recharge.credited_at)
        self.assertEqual(ClientWalletLedger.objects.count(), 0)
        self.assertFalse(
            ClientWallet.objects.filter(profile=self.profile, balance__gt=0).exists()
        )


@override_settings(**VIRTUAL_SETTINGS)
class ReconcileRechargesCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='reconcile-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid=OPENID,
            nickname='对账测试老板',
        )
        self.product = RechargeProduct.objects.create(
            amount=Decimal('30.00'),
            product_id='test_rcg_30',
            goods_price_fen=3000,
            is_active=True,
        )

    def _expired_recharge(self, recharge_no):
        return RechargeOrder.objects.create(
            recharge_no=recharge_no,
            profile=self.profile,
            product=self.product,
            amount=Decimal('30.00'),
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_PAYING,
            expires_at=timezone.now() - timedelta(minutes=RECHARGE_EXPIRE_MINUTES),
            notify_payload={'product_id': 'test_rcg_30', 'goods_price_fen': 3000},
        )

    def test_confirmed_unpaid_recharge_is_closed(self):
        recharge = self._expired_recharge('RCGRECON0001')
        with patch(
            'apps.wallet.services.xpay_post',
            return_value=unpaid_query_response(),
        ):
            call_command('reconcile_recharges')
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, RechargeOrder.STATUS_CLOSED)
        self.assertEqual(ClientWalletLedger.objects.count(), 0)

    def test_query_error_never_blind_closes(self):
        recharge = self._expired_recharge('RCGRECON0002')
        with patch(
            'apps.wallet.services.xpay_post',
            side_effect=VirtualPaymentAPIError('network_error', '微信虚拟支付接口暂不可用'),
        ):
            call_command('reconcile_recharges')
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, RechargeOrder.STATUS_PAYING)

    def test_paid_recharge_is_credited_and_delivered(self):
        recharge = self._expired_recharge('RCGRECON0003')
        with patch(
            'apps.wallet.services.xpay_post',
            side_effect=[paid_query_response(3000, 'WX_RCG_RECON'), {'errcode': 0, 'errmsg': 'OK'}],
        ) as mocked_xpay:
            call_command('reconcile_recharges')
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, RechargeOrder.STATUS_CREDITED)
        self.assertEqual(recharge.third_trade_no, 'WX_RCG_RECON')
        self.assertEqual(mocked_xpay.call_count, 2)
        wallet = ClientWallet.objects.get(profile=self.profile)
        self.assertEqual(wallet.balance, Decimal('30.00'))
        self.assertEqual(ClientWalletLedger.objects.count(), 1)


class RechargeMockGateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mock-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid=OPENID,
            nickname='mock测试老板',
        )
        self.product = RechargeProduct.objects.create(
            amount=Decimal('50.00'),
            product_id='test_rcg_50',
            goods_price_fen=5000,
            is_active=True,
        )
        self.client_api = APIClient()
        self.client_api.force_authenticate(self.user)

    @override_settings(ENABLE_MOCK_PAYMENT=False)
    def test_mock_endpoint_hard_gated_when_disabled(self):
        recharge = RechargeOrder.objects.create(
            recharge_no='RCGMOCK0001',
            profile=self.profile,
            product=self.product,
            amount=Decimal('50.00'),
            channel=RechargeOrder.CHANNEL_MOCK,
            status=RechargeOrder.STATUS_PAYING,
        )
        response = self.client_api.post(
            f'/api/client/wallet/recharge/mock/{recharge.recharge_no}/success'
        )
        self.assertEqual(response.status_code, 404)
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, RechargeOrder.STATUS_PAYING)
        self.assertEqual(ClientWalletLedger.objects.count(), 0)

    @override_settings(ENABLE_MOCK_PAYMENT=True, WECHAT_VIRTUALPAY_ENABLED=False)
    def test_mock_create_and_success_credits_wallet(self):
        create_response = self.client_api.post(
            '/api/client/wallet/recharge/create',
            {'product_id': self.product.id, 'code': ''},
            format='json',
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertTrue(create_response.data['mock'])
        recharge_no = create_response.data['recharge_no']

        success_response = self.client_api.post(
            f'/api/client/wallet/recharge/mock/{recharge_no}/success'
        )
        self.assertEqual(success_response.status_code, 200)
        self.assertEqual(success_response.data['status'], RechargeOrder.STATUS_CREDITED)
        self.assertEqual(success_response.data['balance'], '50.00')

        recharge = RechargeOrder.objects.get(recharge_no=recharge_no)
        self.assertEqual(recharge.channel, RechargeOrder.CHANNEL_MOCK)
        self.assertEqual(recharge.status, RechargeOrder.STATUS_CREDITED)
        wallet = ClientWallet.objects.get(profile=self.profile)
        self.assertEqual(wallet.balance, Decimal('50.00'))
        self.assertEqual(ClientWalletLedger.objects.count(), 1)

        # 重复点击 mock 成功不重复入账。
        replay_response = self.client_api.post(
            f'/api/client/wallet/recharge/mock/{recharge_no}/success'
        )
        self.assertEqual(replay_response.status_code, 200)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal('50.00'))
        self.assertEqual(ClientWalletLedger.objects.count(), 1)


class WalletApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='wallet-api-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid=OPENID,
            nickname='钱包接口测试老板',
        )
        self.client_api = APIClient()
        self.client_api.force_authenticate(self.user)

    def test_overview_returns_zero_without_wallet_row(self):
        response = self.client_api.get('/api/client/wallet/overview')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {
            'balance': '0.00',
            'recharged_total': '0.00',
            'spent_total': '0.00',
        })
        self.assertFalse(ClientWallet.objects.exists())

    def test_overview_returns_wallet_amounts(self):
        ClientWallet.objects.create(
            profile=self.profile,
            balance=Decimal('55.50'),
            recharged_total=Decimal('100.00'),
            spent_total=Decimal('44.50'),
        )
        response = self.client_api.get('/api/client/wallet/overview')
        self.assertEqual(response.data['balance'], '55.50')
        self.assertEqual(response.data['recharged_total'], '100.00')
        self.assertEqual(response.data['spent_total'], '44.50')

    def test_profile_embeds_wallet_balance_without_creating_row(self):
        response = self.client_api.get('/api/client/profile')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['wallet'], {'balance': '0.00'})
        self.assertFalse(ClientWallet.objects.exists())

        ClientWallet.objects.create(profile=self.profile, balance=Decimal('66.00'))
        response = self.client_api.get('/api/client/profile')
        self.assertEqual(response.data['wallet'], {'balance': '66.00'})

    def test_recharge_packages_only_active_sorted(self):
        RechargeProduct.objects.create(
            amount=Decimal('100.00'), product_id='test_rcg_100', goods_price_fen=10000,
            is_active=True, sort_order=2,
        )
        RechargeProduct.objects.create(
            amount=Decimal('30.00'), product_id='test_rcg_30', goods_price_fen=3000,
            is_active=True, sort_order=1,
        )
        RechargeProduct.objects.create(
            amount=Decimal('500.00'), product_id='test_rcg_500', goods_price_fen=50000,
            is_active=False, sort_order=0,
        )
        response = self.client_api.get('/api/client/wallet/recharge/packages')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item['amount'] for item in response.data['results']],
            ['30.00', '100.00'],
        )

    def test_transactions_pagination(self):
        wallet = ClientWallet.objects.create(profile=self.profile, balance=Decimal('30.00'))
        for index in range(3):
            ClientWalletLedger.objects.create(
                wallet=wallet,
                entry_type=ClientWalletLedger.TYPE_RECHARGE,
                amount=Decimal('10.00'),
                balance_after=Decimal(10 * (index + 1)),
                reference_id=f'RCGTX{index}',
            )
        response = self.client_api.get('/api/client/wallet/transactions?page=1&page_size=2')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 3)
        self.assertEqual(len(response.data['results']), 2)
        first = response.data['results'][0]
        self.assertEqual(first['entry_type'], 'recharge')
        self.assertEqual(first['amount'], '10.00')
        response_page2 = self.client_api.get('/api/client/wallet/transactions?page=2&page_size=2')
        self.assertEqual(len(response_page2.data['results']), 1)


class MockEndpointHardeningTests(TestCase):
    """B3/B5/B7 修复回归：mock 端点封堵、充值单号探测防护、mock 孤儿对账。"""

    def setUp(self):
        self.user = User.objects.create_user(username='mock-hardening-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_mock_hard',
            nickname='mock加固测试老板',
        )
        self.product = RechargeProduct.objects.create(
            amount=Decimal('30.00'),
            product_id='test_rcg_hard_30',
            goods_price_fen=3000,
            is_active=True,
        )
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def _make_recharge(self, channel, recharge_no):
        return RechargeOrder.objects.create(
            recharge_no=recharge_no,
            profile=self.profile,
            product=self.product,
            amount=Decimal('30.00'),
            channel=channel,
            status=RechargeOrder.STATUS_PAYING,
            expires_at=timezone.now() + timedelta(minutes=10),
        )

    @override_settings(ENABLE_MOCK_PAYMENT=True, **VIRTUAL_SETTINGS)
    def test_mock_success_disabled_when_virtualpay_enabled(self):
        """真实虚拟支付通道开启时，mock 确认端点必须整体 404，防止免费入账。"""
        recharge = self._make_recharge(RechargeOrder.CHANNEL_WECHAT_VIRTUAL, 'RCGHARD001')
        response = self.api.post(f'/api/client/wallet/recharge/mock/{recharge.recharge_no}/success')
        self.assertEqual(response.status_code, 404)
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, RechargeOrder.STATUS_PAYING)
        self.assertIsNone(recharge.credited_at)
        self.assertFalse(ClientWalletLedger.objects.exists())

    @override_settings(ENABLE_MOCK_PAYMENT=True, WECHAT_VIRTUALPAY_ENABLED=False)
    def test_mock_success_rejects_real_channel_recharge(self):
        """mock 确认端点不得把真实通道充值单强改为 mock 后入账。"""
        recharge = self._make_recharge(RechargeOrder.CHANNEL_WECHAT_VIRTUAL, 'RCGHARD002')
        response = self.api.post(f'/api/client/wallet/recharge/mock/{recharge.recharge_no}/success')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['detail'], '该充值单不支持模拟支付')
        recharge.refresh_from_db()
        self.assertEqual(recharge.channel, RechargeOrder.CHANNEL_WECHAT_VIRTUAL)
        self.assertEqual(recharge.status, RechargeOrder.STATUS_PAYING)
        self.assertFalse(ClientWalletLedger.objects.exists())

    @override_settings(ENABLE_MOCK_PAYMENT=True, WECHAT_VIRTUALPAY_ENABLED=False)
    def test_query_other_users_recharge_returns_uniform_not_found(self):
        """非本人查询与单号不存在必须返回完全一致的响应，防止存在性探测。"""
        recharge = self._make_recharge(RechargeOrder.CHANNEL_MOCK, 'RCGHARD003')
        other = User.objects.create_user(username='mock-hardening-other')
        ClientProfile.objects.create(user=other, openid='openid_mock_other', nickname='other')
        other_api = APIClient()
        other_api.force_authenticate(other)

        response_foreign = other_api.post(f'/api/client/wallet/recharge/query/{recharge.recharge_no}')
        response_missing = other_api.post('/api/client/wallet/recharge/query/RCG_NOT_EXIST')
        self.assertEqual(response_foreign.status_code, 400)
        self.assertEqual(response_missing.status_code, 400)
        self.assertEqual(response_foreign.data['detail'], '充值单不存在')
        self.assertEqual(response_missing.data['detail'], '充值单不存在')

    @override_settings(WECHAT_VIRTUALPAY_ENABLED=False)
    def test_reconcile_closes_expired_mock_recharges_without_config(self):
        """虚拟支付未配置时，对账命令仍应本地关闭过期的 mock 孤儿充值单。"""
        recharge = self._make_recharge(RechargeOrder.CHANNEL_MOCK, 'RCGHARD004')
        RechargeOrder.objects.filter(pk=recharge.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        call_command('reconcile_recharges')
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, RechargeOrder.STATUS_CLOSED)
