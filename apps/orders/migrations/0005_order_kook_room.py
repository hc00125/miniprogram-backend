import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0004_orderitem'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='kook_room_number',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='KOOK房间号'),
        ),
        migrations.AddField(
            model_name='order',
            name='kook_room_updated_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='KOOK房间号更新时间'),
        ),
        migrations.AddField(
            model_name='order',
            name='kook_room_updated_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='updated_kook_rooms', to=settings.AUTH_USER_MODEL, verbose_name='KOOK房间号填写人'),
        ),
    ]
