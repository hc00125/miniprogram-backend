from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('gifts', '0009_allow_free_catalog_gifts'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.AlterField(
        model_name='playergiftconfigaudit', name='actor',
        field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL),
    )]
