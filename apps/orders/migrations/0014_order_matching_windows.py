from datetime import timedelta

from django.db import migrations, models
import django.db.models.deletion


MATCHING_DECISION_MINUTES = 30


def backfill_matching_windows_and_cleanup(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    OrderMatchingWindow = apps.get_model('orders', 'OrderMatchingWindow')
    OrderReplacementState = apps.get_model('orders', 'OrderReplacementState')
    using = schema_editor.connection.alias

    # 付款前的普通陪玩退出属于继续匹配，不应保留“紧急补位”状态。
    OrderReplacementState.objects.using(using).filter(
        mode='public',
        status='open',
        order__paid=False,
        order__fulfillment_mode='public',
        order__status__in=['待接单', '待支付'],
    ).update(status='resolved', missing_slots=0)

    existing_order_ids = OrderMatchingWindow.objects.using(using).values_list('order_id', flat=True)
    orders = (
        Order.objects.using(using)
        .filter(
            order_type='normal',
            fulfillment_mode='public',
            paid=False,
            status__in=['待接单', '待支付'],
        )
        .exclude(pk__in=existing_order_ids)
        .only('pk', 'created_at')
        .iterator(chunk_size=1000)
    )
    batch = []
    for order in orders:
        started_at = order.created_at
        batch.append(OrderMatchingWindow(
            order_id=order.pk,
            started_at=started_at,
            deadline_at=started_at + timedelta(minutes=MATCHING_DECISION_MINUTES),
        ))
        if len(batch) >= 1000:
            OrderMatchingWindow.objects.using(using).bulk_create(batch, ignore_conflicts=True)
            batch.clear()
    if batch:
        OrderMatchingWindow.objects.using(using).bulk_create(batch, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0013_player_cancellation_and_replacement'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrderMatchingWindow',
            fields=[
                ('order', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name='matching_window', serialize=False, to='orders.order', verbose_name='订单')),
                ('started_at', models.DateTimeField(db_index=True, verbose_name='开始匹配时间')),
                ('deadline_at', models.DateTimeField(db_index=True, verbose_name='需老板确认时间')),
                ('extension_count', models.PositiveSmallIntegerField(default=0, verbose_name='继续等待次数')),
                ('last_extended_at', models.DateTimeField(blank=True, null=True, verbose_name='最近继续等待时间')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': '订单匹配时限',
                'verbose_name_plural': '订单匹配时限',
                'db_table': 'order_matching_windows',
            },
        ),
        migrations.RunPython(backfill_matching_windows_and_cleanup, migrations.RunPython.noop),
    ]
