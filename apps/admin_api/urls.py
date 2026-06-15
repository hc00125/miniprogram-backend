from django.urls import path

from . import payment_views, views

urlpatterns = [
    path('login', views.login),
    path('me', views.me),
    path('player-applications', views.player_applications),
    path('player-applications/<int:application_id>/approve', views.approve_application),
    path('player-applications/<int:application_id>/reject', views.reject_application),
    path('orders', views.orders),
    path('orders/<str:order_no>', views.order_detail),
    path('payments', payment_views.payment_transactions),
    path('payments/<str:payment_no>', payment_views.payment_detail),
    path('payment-callbacks', payment_views.payment_callbacks),
    path('refunds', payment_views.refunds),
    path('refunds/<str:refund_no>', payment_views.refund_detail),
    path('packages', views.packages),
    path('packages/<int:package_id>', views.package_detail),
    path('packages/<int:package_id>/disable', views.disable_package),
    path('addons', views.addons),
    path('addons/<int:addon_id>', views.addon_detail),
    path('addons/<int:addon_id>/disable', views.disable_addon),
    path('package-groups', views.package_groups),
]
