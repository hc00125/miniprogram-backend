from django.urls import path

from . import batch_views, boss_views, catalog_navigation_views, designation_views, payment_views, rating_views

urlpatterns = [
    path('catalog-navigation', catalog_navigation_views.catalog_navigation),
    path('packages', boss_views.packages),
    path('package-groups', boss_views.package_groups),
    path('addons', boss_views.addons),
    path('player-types', boss_views.player_types),
    path('online-players', boss_views.online_players),
    path('order', boss_views.create_order),
    path('order/<str:order_no>', payment_views.order_detail),
    path('order/<str:order_no>/renew', boss_views.create_renewal),
    path('order/<str:order_no>/designations', designation_views.designations),
    path('order/<str:order_no>/designation/<int:designation_id>/release', designation_views.release),
    path('orders/me', boss_views.my_orders),
    path('orders/batch', batch_views.create_cart_order_batch),
    path('orders/<str:boss_wechat>', boss_views.boss_orders),
    path('order/<str:order_no>/cancel', boss_views.cancel_order),
    path('order/<str:order_no>/pause', boss_views.pause),
    path('order/<str:order_no>/resume', boss_views.resume),
    path('order/<str:order_no>/self-confirm-payment', boss_views.self_confirm_payment),
    path('order/<str:order_no>/rate', boss_views.rate_player),
    path('order/<str:order_no>/ratings', rating_views.order_ratings),
    path('cart', boss_views.cart),
    path('cart/clear', boss_views.clear_cart),
    path('cart/<int:item_id>', boss_views.cart_item),
]
