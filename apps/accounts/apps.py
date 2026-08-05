from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.accounts'
    verbose_name = '客户管理'

    def ready(self):
        from . import signals  # noqa: F401

        # 在客户资料后台直接调整老板钻石；未创建钱包的注册用户会自动补建钱包。
        from . import client_wallet_admin_patch  # noqa: F401
