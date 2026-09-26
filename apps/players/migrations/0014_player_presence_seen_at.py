from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('players', '0013_player_service_listing')]
    operations = [
        migrations.AddField(
            model_name='player', name='presence_seen_at',
            field=models.DateTimeField(blank=True, null=True, editable=False, verbose_name='小程序最近活跃时间'),
        ),
    ]
