from django.urls import path

from . import views

urlpatterns = [
    path('list', views.list),
    path('logout', views.logout),
    path('me', views.me),
    path('apply', views.apply),
    path('apply/status', views.apply_status),
    path('available-orders', views.available_orders),
    path('grab', views.grab),
    path('my-orders', views.my_orders),
    path('order/<str:order_no>', views.order_detail),
    path('start-timer', views.start_timer_view),
    path('complete', views.complete),
    path('order/<str:order_no>/pause', views.pause),
    path('order/<str:order_no>/resume', views.resume),
]
