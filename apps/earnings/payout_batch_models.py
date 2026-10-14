from decimal import Decimal

from django.conf import settings
from django.db import models

from .models import Withdrawal


class WithdrawalPayoutBatch(models.Model):
    batch_no = models.CharField(max_length=40, unique=True, db_index=True, verbose_name='系统付款批次号')
    payment_method = models.CharField(
        max_length=20,
        choices=Withdrawal.METHOD_CHOICES,
        default=Withdrawal.METHOD_WECHAT,
        verbose_name='实际付款方式',
    )
    item_count = models.PositiveIntegerField(default=0, verbose_name='付款笔数')
    total_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name='付款总额',
    )
    external_transfer_no = models.CharField(
        max_length=150,
        blank=True,
        default='',
        verbose_name='外部转账批次号',
        help_text='选填。微信、支付宝或银行提供的外部流水号。',
    )
    admin_note = models.CharField(max_length=500, blank=True, default='', verbose_name='付款备注')
    proof_url = models.CharField(max_length=500, blank=True, default='', verbose_name='付款凭证URL')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='created_withdrawal_payout_batches',
        verbose_name='付款管理员',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='付款时间')

    class Meta:
        app_label = 'earnings'
        db_table = 'withdrawal_payout_batches'
        verbose_name = '提现付款批次'
        verbose_name_plural = '提现付款批次'
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f'{self.batch_no} - {self.item_count}笔 - {self.total_amount}'


class WithdrawalPayoutBatchItem(models.Model):
    batch = models.ForeignKey(
        WithdrawalPayoutBatch,
        on_delete=models.PROTECT,
        related_name='items',
        verbose_name='付款批次',
    )
    withdrawal = models.OneToOneField(
        Withdrawal,
        on_delete=models.PROTECT,
        related_name='payout_batch_item',
        verbose_name='提现申请',
    )
    amount_snapshot = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='付款金额快照')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'earnings'
        db_table = 'withdrawal_payout_batch_items'
        verbose_name = '提现付款批次明细'
        verbose_name_plural = '提现付款批次明细'
        ordering = ['id']

    def __str__(self):
        return f'{self.batch.batch_no} - {self.withdrawal.withdrawal_no}'
