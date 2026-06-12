from django.urls import path

from . import views

urlpatterns = [
    path('create', views.create),
    path('wechat/miniprogram/create', views.create_wechat_miniprogram),
    path('status/<str:payment_no>', views.status_view),
    path('wechat/query/<str:payment_no>', views.query_wechat_order),
    path('mock/<str:payment_no>/success', views.mock_success),
    path('wechat/callback', views.wechat_callback),
]
