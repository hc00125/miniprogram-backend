from django.db import models


class Payment(models.Model):
    payment_no = models.CharField(max_length=40, unique=True, db_index=True)
    order = models.ForeignKey('orders.Order', to_field='order_no', db_column='order_no', on_delete=models.CASCADE, related_name='payments')
    channel = models.CharField(max_length=20)
    scene = models.CharField(max_length=20)
    amount = models.FloatField()
    status = models.CharField(max_length=20, default='created')
    third_trade_no = models.CharField(max_length=80, blank=True, null=True)
    third_order_no = models.CharField(max_length=80, blank=True, null=True)
    qr_code = models.TextField(blank=True, null=True)
    notify_payload = models.JSONField(blank=True, null=True)
    paid_at = models.DateTimeField(blank=True, null=True)
    expires_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'payment_transactions'
        verbose_name = '支付记录'
        verbose_name_plural = '支付记录列表'
        ordering = ['-created_at']

    @property
    def mock(self):
        return bool(self.qr_code and self.qr_code.startswith('mockpay://'))


class PaymentCallbackLog(models.Model):
    payment_no = models.CharField(max_length=40, db_index=True, blank=True, default='')
    channel = models.CharField(max_length=20)
    payload = models.JSONField(blank=True, null=True)
    verify_result = models.BooleanField(default=False)
    handled_result = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'payment_notify_logs'
        verbose_name = '支付回调日志'
        verbose_name_plural = '支付回调日志列表'
        ordering = ['-created_at']