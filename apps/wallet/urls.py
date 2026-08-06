from django.urls import path

from apps.common.account_guard import require_operational
from apps.common.purchase_guard import require_purchase_available

from . import recharge_actions, views


urlpatterns = [
    path('overview', views.overview),
    path('recharge/packages', views.recharge_packages),
    path('recharge/orders', views.recharge_orders),
    path('recharge/create', require_purchase_available(require_operational(views.recharge_create))),
    path('recharge/query/<str:recharge_no>', views.recharge_query),
    path('recharge/cancel/<str:recharge_no>', recharge_actions.cancel_recharge),
    path('recharge/mock/<str:recharge_no>/success', views.recharge_mock_success),
    path('transactions', views.transactions),
]
