from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0007_game_service_navigation'),
        ('players', '0010_order_escort_requirement_snapshot'),
    ]

    operations = [
        migrations.AddField(
            model_name='player',
            name='minimum_designated_player_type',
            field=models.ForeignKey(
                blank=True,
                help_text='老板指定该陪玩时，至少按此类型对应的商品规格单人价计费；留空时按陪玩自身类型计费。',
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='minimum_designated_players',
                to='catalog.playertype',
                verbose_name='最低指定计费类型',
            ),
        ),
    ]
