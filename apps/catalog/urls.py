from django.urls import path

from . import api_views


urlpatterns = [
    path('players/<int:player_id>/offers', api_views.player_offers),
]
