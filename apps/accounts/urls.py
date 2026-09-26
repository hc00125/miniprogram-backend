from django.urls import path

from . import consumption_views, views

urlpatterns = [
    path('wechat-login', views.wechat_login),
    path('profile', views.profile),
    path('phone-number', views.bind_phone_number),
    path('avatar', views.avatar),
    path('consumption-records', consumption_views.consumption_records),
]
