from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0003_repair_rejected_player_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='player',
            name='audio_intro_url',
            field=models.CharField(blank=True, default='', help_text='填写 mp3/m4a 等音频地址，例如 https://api.huc125.cn/media/player-audio/xxx.mp3', max_length=500, verbose_name='音频自我介绍URL'),
        ),
        migrations.AddField(
            model_name='player',
            name='audio_intro_title',
            field=models.CharField(blank=True, default='', help_text='可选，例如：我的自我介绍', max_length=100, verbose_name='音频标题'),
        ),
    ]
