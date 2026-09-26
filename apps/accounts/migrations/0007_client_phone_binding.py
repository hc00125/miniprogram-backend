from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0006_account_restrictions'),
    ]

    operations = [
        migrations.CreateModel(
            name='ClientPhoneBinding',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('phone_number', models.CharField(db_index=True, max_length=32, verbose_name='手机号')),
                ('country_code', models.CharField(blank=True, default='86', max_length=8, verbose_name='国家/地区码')),
                ('bound_at', models.DateTimeField(auto_now_add=True, verbose_name='首次绑定时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='最近更新时间')),
                ('profile', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='phone_binding', to='accounts.clientprofile', verbose_name='用户')),
            ],
            options={
                'verbose_name': '用户手机号绑定',
                'verbose_name_plural': '用户手机号绑定',
                'db_table': 'client_phone_bindings',
            },
        ),
    ]
