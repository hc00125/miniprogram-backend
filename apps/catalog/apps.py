from django.apps import AppConfig


class CatalogConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.catalog'
    verbose_name = '套餐管理'

    def ready(self):
        from .escort_admin_patch import patch_package_admin

        patch_package_admin()
