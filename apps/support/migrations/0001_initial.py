from django.db import migrations, models


def seed_official_customer_service(apps, schema_editor):
    SupportChannel = apps.get_model('support', 'SupportChannel')
    SupportChannel.objects.update_or_create(
        name='微信官方客服',
        channel_type='wechat_official',
        audience='all',
        defaults={
            'description': '通过微信小程序客服会话联系平台',
            'service_hours': '',
            'sort_order': 0,
            'is_active': True,
        },
    )


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='SupportChannel',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=50, verbose_name='渠道名称')),
                ('channel_type', models.CharField(choices=[('wechat_official', '微信官方客服'), ('wechat_personal', '人工客服微信')], db_index=True, max_length=30, verbose_name='渠道类型')),
                ('wechat_id', models.CharField(blank=True, default='', max_length=100, verbose_name='客服微信号')),
                ('service_hours', models.CharField(blank=True, default='', max_length=100, verbose_name='服务时间')),
                ('description', models.CharField(blank=True, default='', max_length=200, verbose_name='展示说明')),
                ('audience', models.CharField(choices=[('all', '全部用户'), ('boss', '仅老板'), ('player', '仅陪玩')], db_index=True, default='all', max_length=20, verbose_name='展示对象')),
                ('sort_order', models.IntegerField(default=0, verbose_name='排序')),
                ('is_active', models.BooleanField(db_index=True, default=True, verbose_name='是否展示')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': '客服渠道',
                'verbose_name_plural': '客服渠道',
                'db_table': 'support_channels',
                'ordering': ['sort_order', 'id'],
            },
        ),
        migrations.RunPython(seed_official_customer_service, migrations.RunPython.noop),
    ]
