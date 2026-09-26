from django.db import migrations


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('payments', '0005_decimal_payment_amounts'),
    ]

    operations = [
        migrations.CreateModel(
            name='RefundPayment',
            fields=[],
            options={
                'verbose_name': '退款处理',
                'verbose_name_plural': '退款处理',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('payments.payment',),
        ),
        migrations.CreateModel(
            name='RefundRecord',
            fields=[],
            options={
                'verbose_name': '退款记录',
                'verbose_name_plural': '退款记录',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('payments.refund',),
        ),
    ]
