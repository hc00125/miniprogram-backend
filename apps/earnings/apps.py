from django.apps import AppConfig


class EarningsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.earnings'
    verbose_name = '陪玩收益与钱包'

    def ready(self):
        # 独立模型在应用启动时注册，兼容既有 earnings.models 文件结构。
        from . import payout_batch_models, signals  # noqa: F401

        # Django Admin 自动发现完成后覆盖原提现批量动作：审核与付款分离，
        # 外部流水号、备注和凭证改为批次级选填信息。
        from . import payout_batch_admin_patch  # noqa: F401
