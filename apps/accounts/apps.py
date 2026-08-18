from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.accounts'
    verbose_name = '客户管理'

    def ready(self):
        from . import phone_models, signals  # noqa: F401
