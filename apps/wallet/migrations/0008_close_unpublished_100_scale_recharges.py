from django.db import migrations
from django.utils import timezone


def close_obsolete_100_scale_recharges(apps, schema_editor):
    RechargeOrder = apps.get_model('wallet', 'RechargeOrder')
    for recharge in RechargeOrder.objects.filter(status='paying', product__isnull=True).iterator():
        payload = recharge.notify_payload if isinstance(recharge.notify_payload, dict) else {}
        if payload.get('mode') != 'short_series_coin':
            continue
        if str(payload.get('wechat_coin_units_per_yuan') or '') != '100':
            continue

        payload = dict(payload)
        payload['closed_reason'] = 'wechat_coin_ratio_published_as_1_to_10'
        payload['obsolete_wechat_coin_units_per_yuan'] = 100
        recharge.notify_payload = payload
        recharge.status = 'closed'
        recharge.updated_at = timezone.now()
        recharge.save(update_fields=['notify_payload', 'status', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('wallet', '0007_backfill_virtual_payment_diamond_bridges'),
    ]

    operations = [
        migrations.RunPython(close_obsolete_100_scale_recharges, migrations.RunPython.noop),
    ]
