from django.apps import AppConfig


class EarningsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.earnings'
    verbose_name = '陪玩收益与钱包'

    def ready(self):
        from . import signals  # noqa: F401
