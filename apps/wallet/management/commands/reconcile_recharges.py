from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.payments.virtualpay import VirtualPaymentConfigurationError, validate_configuration
from apps.wallet.coin_recharge import reconcile_coin_recharge_order
from apps.wallet.models import RechargeOrder


class Command(BaseCommand):
    help = '对过期仍处于支付中的充值单主动向微信查单对账：已支付入账、明确未支付关闭、状态不明跳过'

    def handle(self, *args, **options):
        now = timezone.now()
        mock_closed = (
            RechargeOrder.objects
            .filter(
                status=RechargeOrder.STATUS_PAYING,
                channel=RechargeOrder.CHANNEL_MOCK,
                expires_at__lte=now,
            )
            .update(status=RechargeOrder.STATUS_CLOSED, updated_at=now)
        )
        if mock_closed:
            self.stdout.write(f'已关闭过期 mock 充值单 {mock_closed} 笔')

        try:
            validate_configuration()
        except VirtualPaymentConfigurationError as exc:
            self.stdout.write(self.style.WARNING(f'虚拟支付未配置，跳过微信充值对账：{exc}'))
            return

        credited = closed = skipped = 0
        queryset = (
            RechargeOrder.objects
            .select_related('profile')
            .filter(
                status=RechargeOrder.STATUS_PAYING,
                channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
                # 只处理真正过期的充值单。不能把刚创建、仍在微信收银台中的
                # PAYING 订单当成“未支付”关闭，否则会形成远端已支付、本地 CLOSED。
                expires_at__lte=now,
            )
            .order_by('id')
        )
        for recharge in queryset.iterator():
            outcome = reconcile_coin_recharge_order(recharge)
            if outcome == 'credited':
                credited += 1
            elif outcome == 'closed':
                closed += 1
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f'充值对账完成：入账 {credited} 笔，关闭 {closed} 笔，跳过 {skipped} 笔'
        ))
