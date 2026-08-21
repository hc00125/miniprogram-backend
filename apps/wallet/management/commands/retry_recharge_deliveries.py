from django.core.management.base import BaseCommand

from apps.wallet.coin_recharge import VIRTUAL_MODE_COIN
from apps.wallet.models import RechargeOrder
from apps.wallet.services import _deliver_paid_recharge


class Command(BaseCommand):
    help = '重试已入账但微信发货通知尚未成功的旧版固定道具充值单'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        limit = min(1000, max(1, options['limit']))
        retried = succeeded = skipped = 0
        queryset = (
            RechargeOrder.objects
            .filter(
                status=RechargeOrder.STATUS_CREDITED,
                channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            )
            .order_by('id')
        )
        for recharge in queryset.iterator():
            payload = dict(recharge.notify_payload or {})
            # short_series_coin 由微信直接充值代币，不需要 notify_provide_goods。
            if payload.get('mode') == VIRTUAL_MODE_COIN:
                continue
            if payload.get('delivery_status') == 'succeeded':
                continue
            retried += 1
            updated = _deliver_paid_recharge(recharge)
            updated_payload = dict(updated.notify_payload or {})
            if updated_payload.get('delivery_status') == 'succeeded':
                succeeded += 1
            else:
                skipped += 1
            if retried >= limit:
                break

        self.stdout.write(self.style.SUCCESS(
            f'旧版充值发货通知重试完成：处理 {retried} 笔，成功 {succeeded} 笔，待后续重试 {skipped} 笔'
        ))
