import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0008_static_composition_catalog'),
        ('players', '0011_player_minimum_designated_player_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='package',
            name='owner_player',
            field=models.ForeignKey(blank=True, help_text='仅“陪玩师专属商品”需要填写；下单时由后端据此锁定服务人员。', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='service_products', to='players.player', verbose_name='所属陪玩师'),
        ),
        migrations.AddField(
            model_name='package',
            name='selling_mode',
            field=models.CharField(choices=[('public', '普通商城商品'), ('player_designated', '陪玩师专属商品')], db_index=True, default='public', help_text='陪玩师专属商品只能由所属陪玩师接受，不会进入公开抢单大厅。', max_length=30, verbose_name='销售方式'),
        ),
        migrations.AddConstraint(
            model_name='package',
            constraint=models.UniqueConstraint(condition=models.Q(('selling_mode', 'player_designated')), fields=('owner_player',), name='uniq_player_designated_service_product'),
        ),
    ]
