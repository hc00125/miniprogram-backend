"""Actual WeChat user-signed currency contract; no synthetic query success.

Verified official docs: server/API/VirtualPayment/api_currency_pay and
api_query_order. query_order explicitly excludes currency spends.
"""
from django.conf import settings
from rest_framework.exceptions import ValidationError
from .coin_sync import _request_session
from apps.payments.xpay_user import user_xpay_post


class WeChatSpendAdapter:
    def authenticate(self, attempt, code):
        if attempt.kind == 'order' and not getattr(settings, 'WECHAT_VIRTUALPAY_ENABLED', False):
            raise ValidationError({'code': 'PLATFORM_DISABLED', 'detail': '微信虚拟支付已关闭'})
        if not getattr(settings, 'SHARED_SPEND_PLATFORM_APPROVED', False):
            raise ValidationError({'code': 'POLICY_UNCONFIRMED', 'detail': '共享消费场景尚未获平台准入批准'})
        return _request_session(attempt.wallet.profile, code)

    def spend(self, attempt, session):
        return user_xpay_post('/xpay/currency_pay', attempt.request_payload, session,
                              idempotent_success=False)

    def query(self, attempt):
        # No documented per-currency-spend query. Balance deltas and cash order
        # queries are NOT proof. Keep the reservation for audited resolution.
        return None
