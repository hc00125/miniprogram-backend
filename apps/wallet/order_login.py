"""Request-local order authentication; never persist a code or session key.

WeChatSpendAdapter.authenticate uses only kind and wallet.profile. A login
context deliberately has no payment ID: exchanging a login code is not a spend.
The returned session is bound to this request's verified account and reused for
pending-refund synchronization and the subsequent original-order capture.
"""
import logging
from types import SimpleNamespace
from django.conf import settings
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)


class OrderLogin:
    def __init__(self, code, order_no):
        self.code = code
        self.order_no = order_no
        self.adapter = None
        self.session = None
        self.identity = None

    def acquire(self, profile, *, for_refund=False):
        identity = (profile.pk, profile.openid)
        from .spend_adapter import WeChatSpendAdapter
        try:
            # A committed refund may synchronize while new purchases are off.
            # Cached refund credentials never bypass the independent pay gate.
            if not for_refund and not getattr(settings, 'WECHAT_VIRTUALPAY_ENABLED', False):
                raise ValidationError({'code': 'PLATFORM_DISABLED', 'detail': '微信虚拟支付已关闭'})
            if not getattr(settings, 'SHARED_SPEND_PLATFORM_APPROVED', False):
                raise ValidationError({'code': 'POLICY_UNCONFIRMED', 'detail': '共享消费场景尚未获平台准入批准'})
            if self.identity is not None:
                if identity != self.identity:
                    raise ValidationError({'code': 'IDENTITY_CHANGED', 'detail': '支付账号已变化，请重新进入小程序'})
                return self.session
            self.adapter = WeChatSpendAdapter()
            if for_refund:
                from .coin_sync import _request_session
                session = _request_session(profile, self.code)
            else:
                context = SimpleNamespace(kind='order', wallet=SimpleNamespace(profile=profile))
                session = self.adapter.authenticate(context, self.code)
        except Exception as exc:
            from apps.payments.virtualpay import VirtualPaymentAPIError
            raw_code = str(exc.code) if isinstance(exc, VirtualPaymentAPIError) else ''
            # Never log exception text/traceback: an upstream error can include
            # a credential-bearing URL or echo the one-time login code.
            safe_code = raw_code if (raw_code.lstrip('-').isdigit() and len(raw_code) <= 10
                or raw_code == 'jscode2session_failed') else 'unclassified'
            reason = 'UPSTREAM_LOGIN_ERROR' if isinstance(exc, VirtualPaymentAPIError) else 'LOGIN_INTERNAL_ERROR'
            if isinstance(exc, ValidationError):
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                local_code = str(detail.get('code', ''))
                if local_code in ('PLATFORM_DISABLED', 'POLICY_UNCONFIRMED', 'IDENTITY_CHANGED'):
                    reason = local_code
                elif str(detail.get('detail', '')) == '本次微信登录账号与当前账号不一致':
                    reason = 'WECHAT_IDENTITY_MISMATCH'
                elif not self.code:
                    reason = 'WECHAT_LOGIN_CODE_MISSING'
                else:
                    reason = 'LOGIN_VALIDATION_FAILED'
            logger.warning('order_login_failed order=%s profile=%s stage=pre_dispatch reason=%s error_type=%s wechat_code=%s',
                self.order_no, profile.pk, reason, type(exc).__name__, safe_code)
            if raw_code == '40163':
                raise ValidationError({'code': 'WECHAT_LOGIN_RETRYABLE', 'wechat_code': raw_code,
                    'detail': '微信登录凭证已失效，本次未扣款，请重新支付'}) from None
            if isinstance(exc, ValidationError):
                # Known local identity/configuration failures must not be
                # reclassified as automatic retries.
                raise
            raise ValidationError({'code': 'WECHAT_LOGIN_FAILED',
                'detail': '微信登录校验失败，本次未扣款，请稍后重新支付',
                'wechat_code': safe_code}) from None
        self.identity, self.session = identity, session
        return session

    def authenticate(self, attempt, code):
        # No external authentication here: login must finish before execute().
        if (attempt.kind != 'order' or self.identity is None or
                (attempt.wallet.profile_id, attempt.request_payload['openid']) != self.identity):
            raise ValidationError({'code': 'IDENTITY_CHANGED', 'detail': '支付账号已变化，请重新进入小程序'})
        return self.session

    def spend(self, attempt, session):
        return self.adapter.spend(attempt, session)
