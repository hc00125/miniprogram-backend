from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.accounts.models import ClientProfile
from apps.payments.models import Refund

from .models import ClientWallet
from .services import refund_balance_payment


@receiver(post_save, sender=ClientProfile)
def create_wallet_for_new_client(sender, instance, created, using, **kwargs):
    """新老板资料创建后补建空钱包，已有钱包时保持幂等。"""
    if not created:
        return

    profile_id = instance.pk

    def ensure_wallet():
        ClientWallet.objects.using(using).get_or_create(profile_id=profile_id)

    # 等客户资料所在事务成功提交后再创建，避免登录事务回滚时留下孤立钱包；
    # 旧流程若已显式创建钱包，get_or_create 会直接复用，不会重复插入。
    transaction.on_commit(ensure_wallet, using=using)


@receiver(post_save, sender=Refund)
def sync_balance_refund(sender, instance, **kwargs):
    """退款成功且原支付通道是余额时，把钱退回老板钱包。幂等靠账本唯一约束。"""
    if instance.status != Refund.STATUS_SUCCEEDED:
        return
    refund_balance_payment(instance)
