from django.urls import path

from . import boss_views

urlpatterns = [
    path('packages', boss_views.packages),
    path('package-groups', boss_views.package_groups),
    path('addons', boss_views.addons),
    path('player-types', boss_views.player_types),
    path('online-players', boss_views.online_players),
    path('order', boss_views.create_order),
    path('order/<str:order_no>', boss_views.order_detail),
    path('orders/me', boss_views.my_orders),
    path('orders/<str:boss_wechat>', boss_views.boss_orders),
    path('order/<str:order_no>/cancel', boss_views.cancel_order),
    path('order/<str:order_no>/pause', boss_views.pause),
    path('order/<str:order_no>/resume', boss_views.resume),
    path('order/<str:order_no>/self-confirm-payment', boss_views.self_confirm_payment),
    path('order/<str:order_no>/rate', boss_views.rate_player),
]
