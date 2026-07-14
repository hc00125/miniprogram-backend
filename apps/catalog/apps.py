from django.apps import AppConfig


class CatalogConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.catalog'
    verbose_name = '套餐管理'

    def ready(self):
        from . import admin_p0  # noqa: F401
