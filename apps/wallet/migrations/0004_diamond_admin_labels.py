from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('wallet', '0003_fixed_diamond_recharge_tiers'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='rechargeproduct',
            options={
                'db_table': 'wallet_recharge_products',
                'ordering': ['sort_order', 'id'],
                'verbose_name': '钻石充值档位',
                'verbose_name_plural': '钻石充值档位',
            },
        ),
        migrations.AlterModelOptions(
            name='rechargeorder',
            options={
                'db_table': 'wallet_recharge_orders',
                'ordering': ['-created_at'],
                'verbose_name': '钻石充值单',
                'verbose_name_plural': '钻石充值单',
            },
        ),
        migrations.AlterField(
            model_name='clientwallet',
            name='balance',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12, verbose_name='人民币等值余额(元)'),
        ),
        migrations.AlterField(
            model_name='clientwallet',
            name='recharged_total',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12, verbose_name='累计充值(元)'),
        ),
        migrations.AlterField(
            model_name='clientwallet',
            name='spent_total',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12, verbose_name='累计余额支付(元)'),
        ),
        migrations.AlterField(
            model_name='rechargeproduct',
            name='amount',
            field=models.DecimalField(decimal_places=2, max_digits=12, verbose_name='实际支付金额(元)'),
        ),
        migrations.AlterField(
            model_name='rechargeproduct',
            name='goods_price_fen',
            field=models.PositiveIntegerField(help_text='例如30元填写3000。', verbose_name='微信道具单价（分）'),
        ),
        migrations.AlterField(
            model_name='rechargeorder',
            name='amount',
            field=models.DecimalField(decimal_places=2, max_digits=12, verbose_name='实际支付金额(元)'),
        ),
        migrations.AlterField(
            model_name='clientwalletledger',
            name='amount',
            field=models.DecimalField(decimal_places=2, max_digits=12, verbose_name='人民币等值变动金额(元)'),
        ),
        migrations.AlterField(
            model_name='clientwalletledger',
            name='balance_after',
            field=models.DecimalField(decimal_places=2, max_digits=12, verbose_name='人民币等值变动后余额(元)'),
        ),
    ]
