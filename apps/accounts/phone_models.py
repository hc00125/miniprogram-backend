from django.db import models

from .models import ClientProfile


class ClientPhoneBinding(models.Model):
    """A phone number explicitly authorized by the WeChat user."""

    profile = models.OneToOneField(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name='phone_binding',
        verbose_name='用户',
    )
    phone_number = models.CharField(max_length=32, db_index=True, verbose_name='手机号')
    country_code = models.CharField(max_length=8, blank=True, default='86', verbose_name='国家/地区码')
    bound_at = models.DateTimeField(auto_now_add=True, verbose_name='首次绑定时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='最近更新时间')

    class Meta:
        db_table = 'client_phone_bindings'
        verbose_name = '用户手机号绑定'
        verbose_name_plural = '用户手机号绑定'

    def __str__(self):
        return f'{self.profile_id} - {self.masked_phone_number}'

    @property
    def masked_phone_number(self):
        value = str(self.phone_number or '')
        if len(value) >= 7:
            return f'{value[:3]}****{value[-4:]}'
        return value
