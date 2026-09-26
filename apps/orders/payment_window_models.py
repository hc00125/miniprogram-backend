from django.db import models


class OrderPaymentWindow(models.Model):
    """Persisted payment-reservation window for one business order.

    The business order keeps its existing coarse status values. This model
    stores the exact reservation and WeChat reconciliation timestamps needed
    by customers, players, operations and audit tooling.
    """

    order = models.OneToOneField(
        'orders.Order',
        on_delete=models.CASCADE,
        related_name='payment_window',
        primary_key=True,
        verbose_name='订单',
    )
    started_at = models.DateTimeField(db_index=True, verbose_name='支付窗口开始时间')
    deadline_at = models.DateTimeField(db_index=True, verbose_name='支付截止时间')
    confirmation_deadline_at = models.DateTimeField(
        db_index=True,
        verbose_name='微信核验截止时间',
    )
    expired_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name='自动取消时间',
    )
    expire_reason = models.CharField(
        max_length=200,
        blank=True,
        default='',
        verbose_name='自动取消原因',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'order_payment_windows'
        verbose_name = '订单支付窗口'
        verbose_name_plural = '订单支付窗口'
        ordering = ['-started_at']

    def __str__(self):
        return f'{self.order_id} - {self.deadline_at}'
