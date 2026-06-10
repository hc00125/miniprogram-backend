from django.urls import path

from . import views

urlpatterns = [
    path('wechat-login', views.wechat_login),
    path('profile', views.profile),
]
