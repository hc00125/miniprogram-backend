from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.payments'
    verbose_name = '支付管理'

    def ready(self):
        # 微信支付单的有效期不得晚于业务订单的阵容保留截止时间。
        from . import payment_deadline_signals  # noqa: F401
        # 余额支付退款无需等待第三方确认：创建退款即原子退回老板钱包；
        # 微信支付仍保留异步待处理流程。直接创建 Refund 的后台/脚本也由
        # refund_integrity 中的兜底信号统一处理。
        from .refund_integrity import install_refund_service_patch

        install_refund_service_patch()
