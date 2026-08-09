from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.payments'
    verbose_name = '支付管理'

    def ready(self):
        # 微信支付单的有效期不得晚于业务订单的阵容保留截止时间。
        from . import payment_deadline_signals  # noqa: F401
        # 微信直接支付的商城订单也统一通过内部钻石桥接记账：人民币购钻石后立即消费，
        # 两条内部流水一正一负，用户钱包余额净变化为0。
        from . import diamond_settlement_signals  # noqa: F401
        # 普通退款统一退回老板钱包（前端显示为钻石）。微信原路退款走独立后台流程，
        # 不会先制造钱包退款；只有把“已退钻石”改成原路退款时才扣回对应钻石。
        from .refund_integrity import install_refund_service_patch

        install_refund_service_patch()
