from django.urls import path

from . import views

urlpatterns = [
    path('<str:order_no>/send', views.send),
    path('<str:order_no>/messages', views.messages),
    path('<str:order_no>/read', views.read),
    path('<str:order_no>/read-status', views.read_status),
]
