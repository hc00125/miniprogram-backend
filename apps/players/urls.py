from django.urls import path

from . import designation_views, escort_views, permission_views, profile_views, room_views, views

urlpatterns = [
    path('list', profile_views.public_list),
    path('ratings/me', views.my_ratings),
    path('<int:player_id>/ratings', views.player_ratings),
    path('online-status', permission_views.update_online_status),
    path('logout', views.logout),
    path('me', views.me),
    path('profile-settings', profile_views.profile_settings),
    path('escort-qualification', escort_views.escort_qualification),
    path('apply', views.apply),
    path('apply/audio', views.upload_application_audio),
    path('apply/status', views.apply_status),
    path('available-orders', permission_views.available_orders),
    path('designation-invitations', designation_views.invitations),
    path('order/<str:order_no>/designation/accept', designation_views.accept),
    path('order/<str:order_no>/designation/decline', designation_views.decline),
    path('grab', permission_views.grab),
    path('my-orders', views.my_orders),
    path('order/<str:order_no>', views.order_detail),
    path('order/<str:order_no>/kook-room', views.set_kook_room),
    path('order/<str:order_no>/room-entry/confirm', room_views.confirm_room_entry),
    path('start-timer', views.start_timer_view),
    path('complete', views.complete),
    path('order/<str:order_no>/pause', views.pause),
    path('order/<str:order_no>/resume', views.resume),
]
