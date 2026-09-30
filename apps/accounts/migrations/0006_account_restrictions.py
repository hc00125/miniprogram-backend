from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_vip_kook_room'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='clientprofile',
            name='account_restricted_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='最近账户操作时间'),
        ),
        migrations.AddField(
            model_name='clientprofile',
            name='account_restricted_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='restricted_client_profiles',
                to=settings.AUTH_USER_MODEL,
                verbose_name='最近操作管理员',
            ),
        ),
        migrations.AddField(
            model_name='clientprofile',
            name='account_restriction_reason',
            field=models.CharField(blank=True, default='', max_length=500, verbose_name='账户限制原因'),
        ),
        migrations.AddField(
            model_name='clientprofile',
            name='account_status',
            field=models.CharField(
                choices=[('active', '正常'), ('suspended', '暂停使用'), ('banned', '永久封禁')],
                db_index=True,
                default='active',
                max_length=20,
                verbose_name='账户状态',
            ),
        ),
        migrations.AddField(
            model_name='clientprofile',
            name='account_suspended_until',
            field=models.DateTimeField(
                blank=True,
                help_text='仅“暂停使用”状态需要填写，到期后系统自动恢复。',
                null=True,
                verbose_name='暂停截止时间',
            ),
        ),
        migrations.CreateModel(
            name='AccountRestrictionLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('from_status', models.CharField(choices=[('active', '正常'), ('suspended', '暂停使用'), ('banned', '永久封禁')], max_length=20, verbose_name='原状态')),
                ('to_status', models.CharField(choices=[('active', '正常'), ('suspended', '暂停使用'), ('banned', '永久封禁')], max_length=20, verbose_name='新状态')),
                ('suspended_until', models.DateTimeField(blank=True, null=True, verbose_name='暂停截止时间')),
                ('reason', models.CharField(blank=True, default='', max_length=500, verbose_name='操作原因')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='操作时间')),
                ('operator', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='account_restriction_logs', to=settings.AUTH_USER_MODEL, verbose_name='操作管理员')),
                ('profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='account_restriction_logs', to='accounts.clientprofile', verbose_name='账户')),
            ],
            options={
                'verbose_name': '账户限制记录',
                'verbose_name_plural': '账户限制记录',
                'db_table': 'account_restriction_logs',
                'ordering': ['-created_at', '-id'],
            },
        ),
    ]
