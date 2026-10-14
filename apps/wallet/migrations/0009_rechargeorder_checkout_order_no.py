from django.db import migrations, models


def backfill_checkout_order_no(apps, schema_editor):
    RechargeOrder = apps.get_model('wallet', 'RechargeOrder')
    for recharge in RechargeOrder.objects.filter(checkout_order_no='').iterator():
        payload = recharge.notify_payload or {}
        order_no = str(payload.get('checkout_order_no') or '').strip()
        if order_no:
            RechargeOrder.objects.filter(pk=recharge.pk).update(checkout_order_no=order_no[:40])


class Migration(migrations.Migration):
    dependencies = [
        ('wallet', '0008_close_unpublished_100_scale_recharges'),
    ]

    operations = [
        migrations.AddField(
            model_name='rechargeorder',
            name='checkout_order_no',
            field=models.CharField(blank=True, db_index=True, default='', max_length=40),
        ),
        migrations.RunPython(backfill_checkout_order_no, migrations.RunPython.noop),
    ]
