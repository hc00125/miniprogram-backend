from django.urls import path

from .views import customer_service_center


urlpatterns = [
    path('customer-service/', customer_service_center, name='support-customer-service'),
]
