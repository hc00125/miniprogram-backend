from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MediaContentSecurityCheck',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('trace_id', models.CharField(db_index=True, max_length=128, unique=True, verbose_name='微信 Trace ID')),
                ('media_url', models.CharField(db_index=True, max_length=1000, verbose_name='媒体 URL')),
                ('media_type', models.PositiveSmallIntegerField(verbose_name='媒体类型')),
                ('scene', models.PositiveSmallIntegerField(default=1, verbose_name='检测场景')),
                ('status', models.CharField(choices=[('pending', '检测中'), ('pass', '已通过'), ('review', '需复核'), ('risky', '有风险'), ('error', '检测异常')], db_index=True, default='pending', max_length=20)),
                ('suggest', models.CharField(blank=True, default='', max_length=20, verbose_name='微信建议')),
                ('label', models.CharField(blank=True, default='', max_length=64, verbose_name='风险标签')),
                ('errcode', models.IntegerField(default=0, verbose_name='微信错误码')),
                ('errmsg', models.CharField(blank=True, default='', max_length=300, verbose_name='微信错误信息')),
                ('raw_result', models.JSONField(blank=True, default=dict, verbose_name='原始回调结果')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='media_content_security_checks', to=settings.AUTH_USER_MODEL, verbose_name='用户')),
            ],
            options={
                'verbose_name': '微信媒体内容安全检测',
                'verbose_name_plural': '微信媒体内容安全检测',
                'db_table': 'media_content_security_checks',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='mediacontentsecuritycheck',
            index=models.Index(fields=['user', 'media_url', 'media_type'], name='media_sec_user_url_idx'),
        ),
    ]
