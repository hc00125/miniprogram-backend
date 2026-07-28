from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.wallet.models import ALLOWED_RECHARGE_AMOUNTS, RechargeProduct


class Command(BaseCommand):
    help = '创建或校准固定七档钻石充值商品，并停用其他充值档位'

    @transaction.atomic
    def handle(self, *args, **options):
        expected_ids = set()
        for sort_order, amount in enumerate(ALLOWED_RECHARGE_AMOUNTS, start=1):
            whole_yuan = int(Decimal(amount))
            product_id = f'recharge_{whole_yuan}'
            expected_ids.add(product_id)
            product, created = RechargeProduct.objects.update_or_create(
                product_id=product_id,
                defaults={
                    'amount': amount,
                    'goods_price_fen': whole_yuan * 100,
                    'is_active': True,
                    'sort_order': sort_order * 10,
                    'remark': f'支付¥{whole_yuan}，到账💎{whole_yuan * 10}',
                },
            )
            action = '创建' if created else '校准'
            self.stdout.write(f'{action} {product.product_id}: ¥{amount} → 💎{product.diamond_amount}')

        disabled = RechargeProduct.objects.exclude(product_id__in=expected_ids).filter(is_active=True).update(is_active=False)
        self.stdout.write(self.style.SUCCESS(f'固定七档充值商品已就绪；额外停用 {disabled} 个非标准档位'))
