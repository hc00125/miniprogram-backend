from decimal import Decimal

from django.db import migrations


FIXED_RECHARGE_TIERS = (10, 30, 50, 100, 200, 500, 1000)


def seed_fixed_recharge_tiers(apps, schema_editor):
    RechargeProduct = apps.get_model('wallet', 'RechargeProduct')
    expected_ids = []
    for index, yuan in enumerate(FIXED_RECHARGE_TIERS, start=1):
        product_id = f'recharge_{yuan}'
        expected_ids.append(product_id)
        product, _created = RechargeProduct.objects.get_or_create(
            product_id=product_id,
            defaults={
                'amount': Decimal(f'{yuan}.00'),
                'goods_price_fen': yuan * 100,
                # 新档位先保持关闭，待微信后台道具审核、上架并核对ID后再启用。
                'is_active': False,
                'sort_order': index * 10,
                'remark': f'支付¥{yuan}，到账{yuan * 10}钻石；启用前请核对微信虚拟道具',
            },
        )
        RechargeProduct.objects.filter(pk=product.pk).update(
            amount=Decimal(f'{yuan}.00'),
            goods_price_fen=yuan * 100,
            sort_order=index * 10,
            remark=f'支付¥{yuan}，到账{yuan * 10}钻石；启用前请核对微信虚拟道具',
        )

    # 产品规则固定为七档；保留历史记录，但其他档位停止向新用户展示。
    RechargeProduct.objects.exclude(product_id__in=expected_ids).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [
        ('wallet', '0002_seed_recharge_products'),
    ]

    operations = [
        migrations.RunPython(seed_fixed_recharge_tiers, migrations.RunPython.noop),
    ]
