from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0005_playerapplication_audio_intro'),
    ]

    operations = [
        migrations.AddField(
            model_name='playerapplication',
            name='real_name',
            field=models.CharField(
                default='',
                help_text='仅供平台审核使用，不对老板或其他用户公开',
                max_length=30,
                verbose_name='真实姓名',
            ),
            preserve_default=False,
        ),
    ]
