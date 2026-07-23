from django.apps import AppConfig


class PlayersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.players'
    verbose_name = '陪玩管理'

    def ready(self):
        from . import escort_admin, escort_signals, service_listing_admin  # noqa: F401
