from django.conf import settings
from django.db import models


class Order(models.Model):
    STATUS_WAITING = '待接单'
    STATUS_IN_PROGRESS = '进行中'
    STATUS_PENDING_PAYMENT = '待支付'
    STATUS_COMPLETED = '已完成'
    STATUS_CANCELLED = '已取消'

    STATUS_CHOICES = [
        (STATUS_WAITING, STATUS_WAITING),
        (STATUS_IN_PROGRESS, STATUS_IN_PROGRESS),
        (STATUS_PENDING_PAYMENT, STATUS_PENDING_PAYMENT),
        (STATUS_COMPLETED, STATUS_COMPLETED),
        (STATUS_CANCELLED, STATUS_CANCELLED),
    ]

    order_no = models.CharField(max_length=20, unique=True, db_index=True)
    boss_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name='boss_orders')
    boss_wechat = models.CharField(max_length=50)
    game_id = models.CharField(max_length=100, blank=True, null=True)
    package = models.ForeignKey('catalog.Package', on_delete=models.PROTECT, related_name='orders')
    addon = models.ForeignKey('catalog.Addon', on_delete=models.SET_NULL, blank=True, null=True, related_name='orders')
    addon_details = models.JSONField(blank=True, null=True)
    required_players = models.IntegerField()
    designated_types = models.JSONField(blank=True, null=True)
    designated_players = models.JSONField(blank=True, null=True)
    boss_note = models.TextField(blank=True, null=True)
    total_price_per_hour = models.FloatField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_WAITING)
    start_time = models.DateTimeField(blank=True, null=True)
    end_time = models.DateTimeField(blank=True, null=True)
    duration_minutes = models.IntegerField(blank=True, null=True)
    total_amount = models.FloatField(blank=True, null=True)
    paid = models.BooleanField(default=False)
    payment_method = models.CharField(max_length=20, blank=True, null=True)
    payment_confirmed_at = models.DateTimeField(blank=True, null=True)
    payment_confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name='confirmed_payments')
    is_custom = models.BooleanField(default=False)
    custom_price = models.FloatField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    canceled_at = models.DateTimeField(blank=True, null=True)
    cancel_reason = models.CharField(max_length=200, blank=True, null=True)
    booked_hours = models.FloatField(default=1.0)
    timer_started_at = models.DateTimeField(blank=True, null=True)
    paused_duration = models.IntegerField(default=0)
    is_paused = models.BooleanField(default=False)
    last_paused_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'orders'
        verbose_name = '订单'
        verbose_name_plural = '订单列表'
        ordering = ['-created_at']

    def __str__(self):
        return self.order_no


class OrderPlayer(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='order_players')
    player = models.ForeignKey('players.Player', on_delete=models.CASCADE, related_name='order_players')
    is_designated = models.BooleanField(default=False)
    designated_type_id = models.IntegerField(blank=True, null=True)
    grab_time = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default='已接单')

    class Meta:
        db_table = 'order_players'
        verbose_name = '订单陪玩'
        verbose_name_plural = '订单陪玩列表'
        unique_together = [('order', 'player')]


class Rating(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='ratings')
    player = models.ForeignKey('players.Player', on_delete=models.CASCADE, related_name='ratings')
    rating = models.IntegerField()
    comment = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ratings'
        verbose_name = '评分'
        verbose_name_plural = '评分列表'
        unique_together = [('order', 'player')]


class OrderStatusLog(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='status_logs')
    from_status = models.CharField(max_length=20, blank=True, default='')
    to_status = models.CharField(max_length=20)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True)
    reason = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'order_status_logs'
        verbose_name = '订单状态日志'
        verbose_name_plural = '订单状态日志列表'
        ordering = ['-created_at']


class OrderEditLog(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='edit_logs')
    admin = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    field_name = models.CharField(max_length=50)
    old_value = models.CharField(max_length=500, blank=True, null=True)
    new_value = models.CharField(max_length=500, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'order_edit_logs'
        verbose_name = '订单编辑日志'
        verbose_name_plural = '订单编辑日志列表'