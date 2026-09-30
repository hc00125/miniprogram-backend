from django.urls import path

from apps.common.account_guard import require_operational

from . import views

urlpatterns = [
    path('overview', views.overview),
    path('settlements', views.settlements),
    path('withdrawals', require_operational(views.withdrawals)),
]
