from datetime import timedelta
from decimal import Decimal

from django.db import migrations


THREE_DAYS = timedelta(days=3)


def set_three_day_review_period(apps, schema_editor):
    EarningsConfig = apps.get_model('earnings', 'EarningsConfig')
    PlayerEarning = apps.get_model('earnings', 'PlayerEarning')

    config, _created = EarningsConfig.objects.get_or_create(
        key='default',
        defaults={
            'default_commission_rate': Decimal('16.00'),
            'review_days': 3,
            'min_withdrawal_amount': Decimal('10.00'),
        },
    )
    if config.review_days != 3:
        config.review_days = 3
        config.save(update_fields=['review_days'])

    # 已经进入审核期的工资也同步缩短到“生成后3天”。这里只缩短，
    # 不延长任何原本更早可用的记录；到期后的实际余额释放仍由现有
    # release_due_earnings 流程完成，以保持钱包流水和并发锁逻辑一致。
    for earning in PlayerEarning.objects.filter(status='pending').iterator():
        if not earning.created_at:
            continue
        review_until = earning.created_at + THREE_DAYS
        if earning.review_until > review_until:
            earning.review_until = review_until
            earning.save(update_fields=['review_until'])


class Migration(migrations.Migration):

    dependencies = [
        ('earnings', '0004_default_commission_16'),
    ]

    operations = [
        migrations.RunPython(set_three_day_review_period, migrations.RunPython.noop),
    ]
