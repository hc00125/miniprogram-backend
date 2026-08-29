from django.db import migrations, models
import django.db.models.deletion


def backfill_existing_orders(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    Snapshot = apps.get_model('players', 'OrderEscortRequirementSnapshot')
    rows = []
    for order in Order.objects.select_related('package').iterator(chunk_size=500):
        rows.append(Snapshot(
            order_id=order.id,
            requires_escort_qualification=False,
            source_package_id=order.package_id,
            source_package_name=getattr(order.package, 'name', '') or '',
        ))
        if len(rows) >= 500:
            Snapshot.objects.bulk_create(rows, ignore_conflicts=True)
            rows = []
    if rows:
        Snapshot.objects.bulk_create(rows, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0006_package_requires_escort_qualification'),
        ('orders', '0009_orderplayer_room_entry'),
        ('players', '0009_escort_qualification'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrderEscortRequirementSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('requires_escort_qualification', models.BooleanField(db_index=True, default=False, verbose_name='需要护航资格')),
                ('source_package_id', models.PositiveBigIntegerField(blank=True, null=True, verbose_name='下单时商品ID')),
                ('source_package_name', models.CharField(blank=True, default='', max_length=100, verbose_name='下单时商品名称')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('order', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='escort_requirement_snapshot', to='orders.order', verbose_name='订单')),
            ],
            options={
                'verbose_name': '订单护航要求快照',
                'verbose_name_plural': '订单护航要求快照',
                'db_table': 'order_escort_requirement_snapshots',
            },
        ),
        migrations.RunPython(backfill_existing_orders, migrations.RunPython.noop),
    ]
