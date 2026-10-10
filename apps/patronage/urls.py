from django.urls import path
from . import views
from .service_duration_views import my_service_durations
from .wall import wall

app_name = 'patronage'
urlpatterns = [
    path('wall/', wall, name='wall'),
    path('my-service-durations/', my_service_durations, name='my-service-durations'),
    path('catalog/', views.catalog, name='catalog'),
    path('quotes/', views.quotes, name='quotes'),
    path('records/', views.records, name='records'),
    path('crowns/', views.crowns, name='crowns'),
    path('purchases/', views.purchases, name='purchases'),
    path('purchases/by-key/', views.purchase_by_key, name='purchase-by-key'),
    path('purchases/pending/', views.pending_purchases, name='pending-purchases'),
]
