from django.urls import path

from . import views

urlpatterns = [
    path('overview', views.overview),
    path('settlements', views.settlements),
    path('withdrawals', views.withdrawals),
]
