from django.urls import path

from . import api_views


urlpatterns = [
    path('players/<int:player_id>/products', api_views.player_service_products),
]
