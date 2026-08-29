from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0004_player_audio_intro'),
    ]

    operations = [
        migrations.AddField(
            model_name='playerapplication',
            name='audio_intro_url',
            field=models.CharField(blank=True, default='', max_length=500, verbose_name='音频自我介绍URL'),
        ),
        migrations.AddField(
            model_name='playerapplication',
            name='audio_intro_title',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='音频标题'),
        ),
    ]
