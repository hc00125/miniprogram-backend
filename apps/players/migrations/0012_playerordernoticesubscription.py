import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0011_player_minimum_designated_player_type'),
    ]

    operations = [
        migrations.CreateModel(
            name='PlayerOrderNoticeSubscription',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('template_id', models.CharField(blank=True, default='', max_length=128, verbose_name='订阅消息模板 ID')),
                ('available_count', models.PositiveIntegerField(default=0, verbose_name='可发送次数')),
                ('last_subscribed_at', models.DateTimeField(blank=True, null=True, verbose_name='最近授权时间')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('player', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='order_notice_subscription', to='players.player', verbose_name='陪玩师')),
            ],
            options={
                'verbose_name': '陪玩师接单订阅消息授权',
                'verbose_name_plural': '陪玩师接单订阅消息授权',
                'db_table': 'player_order_notice_subscriptions',
            },
        ),
    ]
