from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('earnings', '0003_p0_financial_integrity'),
    ]

    operations = [
        migrations.AlterField(
            model_name='earningsconfig',
            name='default_commission_rate',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('16.00'),
                max_digits=5,
                validators=[
                    MinValueValidator(Decimal('0.00')),
                    MaxValueValidator(Decimal('100.00')),
                ],
                verbose_name='默认抽成比例(%)',
            ),
        ),
    ]
