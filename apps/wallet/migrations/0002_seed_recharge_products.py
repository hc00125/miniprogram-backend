from decimal import Decimal

from django.db import migrations


# 默认充值档位：30/50/100/200/500 元。
# product_id 是占位符（recharge_30 等），is_active=False：
# 运营需先在微信虚拟支付后台注册对应道具，把真实道具ID填入后台
# “充值档位”并启用，充值档位才会对用户可见。
DEFAULT_PACKAGES = [30, 50, 100, 200, 500]


def seed_recharge_products(apps, schema_editor):
    RechargeProduct = apps.get_model('wallet', 'RechargeProduct')
    for index, yuan in enumerate(DEFAULT_PACKAGES):
        RechargeProduct.objects.get_or_create(
            product_id=f'recharge_{yuan}',
            defaults={
                'amount': Decimal(yuan),
                'goods_price_fen': yuan * 100,
                'is_active': False,
                'sort_order': index,
                'remark': '占位道具ID，请在微信后台注册道具后填写真实ID并启用',
            },
        )


def remove_recharge_products(apps, schema_editor):
    RechargeProduct = apps.get_model('wallet', 'RechargeProduct')
    RechargeProduct.objects.filter(
        product_id__in=[f'recharge_{yuan}' for yuan in DEFAULT_PACKAGES],
        recharge_orders__isnull=True,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_recharge_products, remove_recharge_products),
    ]
