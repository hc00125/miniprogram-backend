from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.players.models import Player

from .models import Order, OrderPlayer


ORDER_PLAYER_CANCELLED_STATUS = '订单已取消'


@receiver(pre_save, sender=Order, dispatch_uid='capture_order_status_before_cancel')
def capture_order_status_before_cancel(sender, instance, **kwargs):
    """记录保存前状态，用于保证取消清理只执行一次。"""
    if not instance.pk:
        instance._status_before_save = None
        return
    instance._status_before_save = (
        Order.objects
        .filter(pk=instance.pk)
        .values_list('status', flat=True)
        .first()
    )


@receiver(post_save, sender=Order, dispatch_uid='release_players_after_order_cancel')
def release_players_after_order_cancel(sender, instance, created, **kwargs):
    """老板在付款前取消订单时，释放已经接单的陪玩。

    订单和接单关系都保留用于审计，不做物理删除；这里只停止入房倒计时、
    标记接单关系已取消，并撤回抢单时预先增加的接单次数。
    """
    previous_status = getattr(instance, '_status_before_save', None)
    if (
        created
        or instance.paid
        or instance.status != Order.STATUS_CANCELLED
        or previous_status == Order.STATUS_CANCELLED
    ):
        # 已支付订单必须走退款流程，不能因后台误改状态而释放阵容或回退统计。
        return

    with transaction.atomic():
        relations = list(
            OrderPlayer.objects
            .select_for_update()
            .filter(order=instance)
            .exclude(status=ORDER_PLAYER_CANCELLED_STATUS)
            .select_related('player')
        )
        if not relations:
            return

        # 取消后不再继续计算“接单后10分钟入房”超时；原确认时间和状态保留作审计。
        relation_ids = [relation.id for relation in relations]
        OrderPlayer.objects.filter(id__in=relation_ids).update(
            status=ORDER_PLAYER_CANCELLED_STATUS,
            room_join_deadline=None,
        )

        # total_orders 在抢单/接受指定时先加一；付款前取消不应计入陪玩的有效接单数。
        player_ids = {relation.player_id for relation in relations}
        for player in Player.objects.select_for_update().filter(id__in=player_ids):
            next_total = max(0, int(player.total_orders or 0) - 1)
            if next_total != player.total_orders:
                player.total_orders = next_total
                player.save(update_fields=['total_orders'])
