from django.urls import path

from . import recharge_actions, views


urlpatterns = [
    path('overview', views.overview),
    path('recharge/packages', views.recharge_packages),
    path('recharge/orders', views.recharge_orders),
    path('recharge/create', views.recharge_create),
    path('recharge/query/<str:recharge_no>', views.recharge_query),
    path('recharge/cancel/<str:recharge_no>', recharge_actions.cancel_recharge),
    path('recharge/mock/<str:recharge_no>/success', views.recharge_mock_success),
    path('transactions', views.transactions),
]
