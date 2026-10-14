from django.urls import path
from . import views
from .webhook import webhook

player_urlpatterns = [
    path('kook-binding', views.BindingView.as_view()),
    path('kook-binding/challenges', views.ChallengeCreateView.as_view()),
    path('kook-binding/challenges/<uuid:challenge_id>', views.ChallengeView.as_view()),
    path('kook-binding/challenges/<uuid:challenge_id>/confirm', views.ConfirmView.as_view()),
    path('kook-binding/test-notification', views.TestNotificationView.as_view()),
    path('kook-binding/test-notifications/<uuid:delivery_id>', views.TestStatusView.as_view()),
    path('kook-entry/<str:opaque_intent>', views.EntryView.as_view()),
]
from django.urls import include
urlpatterns = [path('api/player/', include(player_urlpatterns)), path('api/integrations/kook/webhook',webhook)]
