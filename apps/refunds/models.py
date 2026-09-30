from apps.payments.models import Payment, Refund


class RefundPayment(Payment):
    """后台退款处理入口，不创建新表。"""

    class Meta:
        proxy = True
        app_label = 'refunds'
        verbose_name = '退款处理'
        verbose_name_plural = '退款处理'


class RefundRecord(Refund):
    """把退款记录从支付管理移动到独立退款管理菜单。"""

    class Meta:
        proxy = True
        app_label = 'refunds'
        verbose_name = '退款记录'
        verbose_name_plural = '退款记录'
