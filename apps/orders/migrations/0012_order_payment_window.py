from datetime import timedelta

from django.db import migrations, models
import django.db.models.deletion


PAYMENT_TIMEOUT_MINUTES = 10
PAYMENT_CONFIRMATION_GRACE_SECONDS = 90


def backfill_active_payment_windows(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    OrderStatusLog = apps.get_model('orders', 'OrderStatusLog')
    OrderPaymentWindow = apps.get_model('orders', 'OrderPaymentWindow')

    pending_orders = Order.objects.filter(status='待支付', paid=False).iterator()
    for order in pending_orders:
        started_at = (
            OrderStatusLog.objects
            .filter(order_id=order.pk, to_status='待支付')
            .order_by('-created_at')
            .values_list('created_at', flat=True)
            .first()
        ) or order.created_at
        deadline_at = started_at + timedelta(minutes=PAYMENT_TIMEOUT_MINUTES)
        OrderPaymentWindow.objects.update_or_create(
            order_id=order.pk,
            defaults={
                'started_at': started_at,
                'deadline_at': deadline_at,
                'confirmation_deadline_at': deadline_at + timedelta(
                    seconds=PAYMENT_CONFIRMATION_GRACE_SECONDS,
                ),
            },
        )


def reverse_backfill(apps, schema_editor):
    OrderPaymentWindow = apps.get_model('orders', 'OrderPaymentWindow')
    OrderPaymentWindow.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0011_order_fulfillment_mode_order_target_player_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrderPaymentWindow',
            fields=[
                ('started_at', models.DateTimeField(db_index=True, verbose_name='支付窗口开始时间')),
                ('deadline_at', models.DateTimeField(db_index=True, verbose_name='支付截止时间')),
                ('confirmation_deadline_at', models.DateTimeField(db_index=True, verbose_name='微信核验截止时间')),
                ('expired_at', models.DateTimeField(blank=True, db_index=True, null=True, verbose_name='自动取消时间')),
                ('expire_reason', models.CharField(blank=True, default='', max_length=200, verbose_name='自动取消原因')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('order', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name='payment_window', serialize=False, to='orders.order', verbose_name='订单')),
            ],
            options={
                'verbose_name': '订单支付窗口',
                'verbose_name_plural': '订单支付窗口',
                'db_table': 'order_payment_windows',
                'ordering': ['-started_at'],
            },
        ),
        migrations.RunPython(backfill_active_payment_windows, reverse_backfill),
    ]
