from django.urls import path

from apps.common.account_guard import require_operational
from apps.common.purchase_guard import require_purchase_available

from . import (
    batch_views,
    boss_replacement_views,
    boss_views,
    catalog_navigation_views,
    designation_views,
    listing_order_views,
    matching_views,
    payment_views,
    rating_views,
)

urlpatterns = [
    path('catalog-navigation', catalog_navigation_views.catalog_navigation),
    path('packages', boss_views.packages),
    path('package-groups', boss_views.package_groups),
    path('addons', boss_views.addons),
    path('player-types', boss_views.player_types),
    path('online-players', boss_views.online_players),
    path('order', require_purchase_available(require_operational(boss_views.create_order))),
    path('listing-order', require_purchase_available(require_operational(listing_order_views.create_order))),
    path('order/<str:order_no>', payment_views.order_detail),
    path('order/<str:order_no>/renew', require_purchase_available(require_operational(boss_views.create_renewal))),
    path('order/<str:order_no>/designations', require_operational(designation_views.designations)),
    path('order/<str:order_no>/designation/<int:designation_id>/release', designation_views.release),
    path('order/<str:order_no>/matching/continue', matching_views.continue_matching),
    path('order/<str:order_no>/replacement', boss_replacement_views.detail),
    path('order/<str:order_no>/replacement/public', boss_replacement_views.publish_public),
    path('order/<str:order_no>/replacement/reassign', boss_replacement_views.reassign),
    path('order/<str:order_no>/replacement/cancel-remaining', boss_replacement_views.cancel_remaining),
    path('order/<str:order_no>/replacement/revoke-cancel', boss_replacement_views.revoke_cancel_remaining),
    path('orders/me', boss_views.my_orders),
    path('orders/batch', require_purchase_available(require_operational(batch_views.create_cart_order_batch))),
    path('orders/<str:boss_wechat>', boss_views.boss_orders),
    path('order/<str:order_no>/cancel', boss_views.cancel_order),
    path('order/<str:order_no>/pause', boss_views.pause),
    path('order/<str:order_no>/resume', boss_views.resume),
    path('order/<str:order_no>/self-confirm-payment', require_operational(boss_views.self_confirm_payment)),
    path('order/<str:order_no>/rate', boss_views.rate_player),
    path('order/<str:order_no>/ratings', rating_views.order_ratings),
    path('cart', require_operational(boss_views.cart)),
    path('cart/clear', require_operational(boss_views.clear_cart)),
    path('cart/<int:item_id>', require_operational(boss_views.cart_item)),
]
