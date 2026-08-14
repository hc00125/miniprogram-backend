from django.urls import path

from apps.common.account_guard import require_operational
from apps.common.purchase_guard import require_ios_balance_only, require_purchase_available
from apps.wallet import views as wallet_views

from . import views

urlpatterns = [
    path('create', require_purchase_available(views.create)),
    path('balance/create', require_ios_balance_only(require_operational(wallet_views.pay_balance_create))),
    path('wechat/miniprogram/create', require_purchase_available(require_operational(views.create_wechat_miniprogram))),
    path('wechat/virtual/create', require_purchase_available(require_operational(views.create_wechat_virtual))),
    path('status/<str:payment_no>', views.status_view),
    path('wechat/query/<str:payment_no>', views.query_wechat_order),
    path('wechat/virtual/query/<str:payment_no>', views.query_wechat_virtual),
    path('wechat/virtual/close/<str:payment_no>', views.close_wechat_virtual),
    path('wechat/virtual/query-order/<str:order_no>', views.query_wechat_virtual_by_order),
    path('mock/<str:payment_no>/success', views.mock_success),
    path('wechat/callback', views.wechat_callback),
]
