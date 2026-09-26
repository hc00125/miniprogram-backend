from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0005_packagespec_required_player_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='package',
            name='requires_escort_qualification',
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text='开启后，该商品生成的订单只能由护航资格已通过的陪玩接单；娱乐陪、技术陪等等级规则仍同时生效。',
                verbose_name='需要护航资格',
            ),
        ),
        migrations.AlterField(
            model_name='packagespec',
            name='required_player_type',
            field=models.ForeignKey(
                blank=True,
                help_text='陪玩等级 priority 必须达到该类型或更高；留空表示不限制陪玩等级。',
                null=True,
                on_delete=models.PROTECT,
                related_name='required_package_specs',
                to='catalog.playertype',
                verbose_name='最低陪玩等级',
            ),
        ),
    ]
