from django.conf import settings
from django.db import models


class Customer(models.Model):
    nickname = models.CharField('老板称呼', max_length=100)
    note = models.CharField('客服内部备注', max_length=300, blank=True, default='')
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='dispatch_customer')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_dispatch_customers')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = '客户档案'
        verbose_name_plural = '客户档案'
        permissions = [('use_console', '使用客服派单工作台')]

    def __str__(self):
        return f'{self.nickname}（客户#{self.pk}）'


class LoginThrottle(models.Model):
    key = models.CharField(max_length=64, unique=True)
    window = models.BigIntegerField(db_index=True)
    hits = models.PositiveIntegerField(default=0)


class HistoryClaim(models.Model):
    requested_number = models.PositiveBigIntegerField()
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.PROTECT)
    applicant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='history_claims')
    message = models.CharField(max_length=300)
    status = models.CharField(max_length=12, default='pending', choices=[('pending', '待核实'), ('approved', '已认领'), ('rejected', '未通过')])
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='reviewed_history_claims')
    review_note = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = '历史订单认领申请'
        verbose_name_plural = verbose_name
        constraints = [models.UniqueConstraint(fields=['applicant', 'requested_number'], condition=models.Q(status='pending'), name='dispatch_pending_claim_unique')]


class DispatchReceipt(models.Model):
    order = models.OneToOneField('orders.Order', on_delete=models.PROTECT, related_name='dispatch_receipt')
    request_key = models.UUIDField(unique=True, editable=False)
    request_digest = models.CharField(max_length=64, editable=False)
    received_amount = models.DecimalField(max_digits=12, decimal_places=2, editable=False)
    receipt_note = models.CharField(max_length=200)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = '线下收款派单记录'
        verbose_name_plural = verbose_name

