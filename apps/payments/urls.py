from django.urls import path

from apps.wallet import views as wallet_views

from . import views

urlpatterns = [
    path('create', views.create),
    path('balance/create', wallet_views.pay_balance_create),
    path('wechat/miniprogram/create', views.create_wechat_miniprogram),
    path('wechat/virtual/create', views.create_wechat_virtual),
    path('status/<str:payment_no>', views.status_view),
    path('wechat/query/<str:payment_no>', views.query_wechat_order),
    path('wechat/virtual/query/<str:payment_no>', views.query_wechat_virtual),
    path('wechat/virtual/close/<str:payment_no>', views.close_wechat_virtual),
    path('wechat/virtual/query-order/<str:order_no>', views.query_wechat_virtual_by_order),
    path('mock/<str:payment_no>/success', views.mock_success),
    path('wechat/callback', views.wechat_callback),
]
