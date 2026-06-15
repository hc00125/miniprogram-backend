import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('orders', '0001_initial'),
        ('payments', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Refund',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('refund_no', models.CharField(db_index=True, max_length=40, unique=True)),
                ('amount', models.FloatField()),
                ('reason', models.CharField(blank=True, default='', max_length=200)),
                ('status', models.CharField(choices=[('pending', '待处理'), ('processing', '退款中'), ('succeeded', '退款成功'), ('failed', '退款失败'), ('cancelled', '已取消')], default='pending', max_length=20)),
                ('third_refund_no', models.CharField(blank=True, max_length=80, null=True)),
                ('notify_payload', models.JSONField(blank=True, null=True)),
                ('failed_reason', models.CharField(blank=True, default='', max_length=300)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_refunds', to=settings.AUTH_USER_MODEL)),
                ('order', models.ForeignKey(db_column='order_no', on_delete=django.db.models.deletion.PROTECT, related_name='refunds', to='orders.order', to_field='order_no')),
                ('payment', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='refunds', to='payments.payment')),
            ],
            options={
                'db_table': 'payment_refunds',
                'ordering': ['-created_at'],
                'verbose_name': '退款记录',
                'verbose_name_plural': '退款记录列表',
            },
        ),
    ]
