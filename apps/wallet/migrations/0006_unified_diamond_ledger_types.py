from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0005_backfill_all_client_wallets'),
    ]

    operations = [
        migrations.AlterField(
            model_name='clientwalletledger',
            name='entry_type',
            field=models.CharField(
                choices=[
                    ('recharge', '充值'),
                    ('order_payment', '订单支付'),
                    ('refund_in', '退款入账'),
                    ('admin_adjust', '人工调整'),
                    ('virtual_purchase_credit', '微信直付购入钻石（内部）'),
                    ('virtual_order_payment', '微信直付订单钻石消费（内部）'),
                    ('wechat_refund_conversion', '钱包退款转微信原路退款'),
                    ('wechat_refund_restore', '微信原路退款失败返还钻石'),
                ],
                db_index=True,
                max_length=40,
            ),
        ),
    ]
