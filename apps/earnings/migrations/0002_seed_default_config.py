from decimal import Decimal

from django.db import migrations


def create_default_config(apps, schema_editor):
    EarningsConfig = apps.get_model('earnings', 'EarningsConfig')
    EarningsConfig.objects.get_or_create(
        key='default',
        defaults={
            'default_commission_rate': Decimal('15.00'),
            'review_days': 8,
            'min_withdrawal_amount': Decimal('1.00'),
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ('earnings', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_default_config, migrations.RunPython.noop),
    ]
