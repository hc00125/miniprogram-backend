from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('payments', '0004_alter_payment_options_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='payment',
            name='amount',
            field=models.DecimalField(decimal_places=2, max_digits=12, verbose_name='人民币等值支付金额(元)'),
        ),
        migrations.AlterField(
            model_name='refund',
            name='amount',
            field=models.DecimalField(decimal_places=2, max_digits=12, verbose_name='人民币等值退款金额(元)'),
        ),
    ]
