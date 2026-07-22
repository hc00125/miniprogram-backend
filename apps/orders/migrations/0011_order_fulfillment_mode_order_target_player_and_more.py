import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0010_composition_pricing'),
        ('players', '0011_player_minimum_designated_player_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='fulfillment_mode',
            field=models.CharField(choices=[('public', '公开抢单'), ('targeted', '指定陪玩师商品')], db_index=True, default='public', max_length=20, verbose_name='履约方式'),
        ),
        migrations.AddField(
            model_name='order',
            name='target_player',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='targeted_orders', to='players.player', verbose_name='指定服务陪玩师'),
        ),
        migrations.AddField(
            model_name='order',
            name='target_player_name_snapshot',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='指定服务陪玩师名称快照'),
        ),
    ]
