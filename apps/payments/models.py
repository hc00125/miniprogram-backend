from django.conf import settings
from django.core.exceptions import ValidationError
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


class VirtualProductBinding(models.Model):
    """把小程序商品/规格绑定到微信虚拟支付道具。"""

    package = models.ForeignKey(
        'catalog.Package',
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name='virtual_payment_bindings',
        verbose_name='绑定商品',
    )
    spec = models.ForeignKey(
        'catalog.PackageSpec',
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name='virtual_payment_bindings',
        verbose_name='绑定规格',
    )
    product_id = models.CharField(
        max_length=20,
        unique=True,
        verbose_name='微信道具ID',
        help_text='例如 escort_15。必须与微信虚拟支付后台中的道具ID完全一致。',
    )
    goods_price_fen = models.PositiveIntegerField(
        verbose_name='道具单价（分）',
        help_text='例如15元填写1500。',
    )
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    remark = models.CharField(max_length=100, blank=True, default='', verbose_name='备注')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'virtual_product_bindings'
        verbose_name = '虚拟支付商品绑定'
        verbose_name_plural = '虚拟支付商品绑定'
        ordering = ['product_id']

    def clean(self):
        if bool(self.package_id) == bool(self.spec_id):
            raise ValidationError('绑定商品和绑定规格必须且只能选择一个。')
        if self.spec_id and self.spec.package_id != self.spec.package_id:
            raise ValidationError('规格信息不正确。')

    def __str__(self):
        target = self.spec or self.package
        return f'{self.product_id} → {target}'


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


class Refund(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_SUCCEEDED = 'succeeded'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'

    STATUS_CHOICES = [
        (STATUS_PENDING, '待处理'),
        (STATUS_PROCESSING, '退款中'),
        (STATUS_SUCCEEDED, '退款成功'),
        (STATUS_FAILED, '退款失败'),
        (STATUS_CANCELLED, '已取消'),
    ]

    refund_no = models.CharField(max_length=40, unique=True, db_index=True)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='refunds')
    order = models.ForeignKey('orders.Order', to_field='order_no', db_column='order_no', on_delete=models.PROTECT, related_name='refunds')
    amount = models.FloatField()
    reason = models.CharField(max_length=200, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    third_refund_no = models.CharField(max_length=80, blank=True, null=True)
    notify_payload = models.JSONField(blank=True, null=True)
    failed_reason = models.CharField(max_length=300, blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name='created_refunds')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payment_refunds'
        verbose_name = '退款记录'
        verbose_name_plural = '退款记录列表'
        ordering = ['-created_at']

    def __str__(self):
        return self.refund_no
