from django.urls import path

from apps.common.account_guard import require_operational
from apps.common.platform_context import capture_client_platform
from apps.common.purchase_guard import require_ios_balance_only, require_purchase_available
from apps.wallet import views as wallet_views

from . import views

urlpatterns = [
    path('create', require_purchase_available(views.create)),
    path(
        'balance/create',
        capture_client_platform(require_ios_balance_only(require_operational(wallet_views.pay_balance_create))),
    ),
    path('wechat/miniprogram/create', require_purchase_available(require_operational(views.create_wechat_miniprogram))),
    path(
        'wechat/virtual/create',
        capture_client_platform(require_purchase_available(require_operational(views.create_wechat_virtual))),
    ),
    # 已完成的 coin 充值必须始终允许落到业务订单；即使临时关闭新购买入口，
    # 也不能阻断已扣款用户的 finalize，因此这里只保留账户状态校验，不套购买熔断。
    path(
        'wechat/virtual/finalize/<str:recharge_no>',
        capture_client_platform(require_operational(views.finalize_checkout_coin)),
    ),
    path('status/<str:payment_no>', views.status_view),
    path('wechat/query/<str:payment_no>', views.query_wechat_order),
    path(
        'wechat/virtual/query/<str:payment_no>',
        capture_client_platform(views.query_wechat_virtual),
    ),
    path('wechat/virtual/close/<str:payment_no>', views.close_wechat_virtual),
    path(
        'wechat/virtual/query-order/<str:order_no>',
        capture_client_platform(views.query_wechat_virtual_by_order),
    ),
    path('mock/<str:payment_no>/success', views.mock_success),
    path('wechat/callback', views.wechat_callback),
]
