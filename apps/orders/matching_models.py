from django.db import models


class OrderMatchingWindow(models.Model):
    """Track the public matching window separately from payment and room-entry deadlines."""

    order = models.OneToOneField(
        'orders.Order',
        on_delete=models.CASCADE,
        related_name='matching_window',
        primary_key=True,
        verbose_name='订单',
    )
    started_at = models.DateTimeField(db_index=True, verbose_name='开始匹配时间')
    deadline_at = models.DateTimeField(db_index=True, verbose_name='需老板确认时间')
    extension_count = models.PositiveSmallIntegerField(default=0, verbose_name='继续等待次数')
    last_extended_at = models.DateTimeField(blank=True, null=True, verbose_name='最近继续等待时间')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'order_matching_windows'
        verbose_name = '订单匹配时限'
        verbose_name_plural = '订单匹配时限'

    def __str__(self):
        return f'{self.order.order_no} - {self.deadline_at}'
