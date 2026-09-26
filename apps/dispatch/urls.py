from django.urls import path
from . import views, web

urlpatterns = [
    path('', web.console), path('login/', web.ConsoleLogin.as_view()), path('logout/', web.sign_out),
    path('api/customers/', views.customers), path('api/catalog/', views.catalog),
    path('api/quote/', views.quote), path('api/orders/', views.orders),
    path('api/orders/<str:order_no>/', views.order_detail),
    path('api/orders/<str:order_no>/cancel/', views.cancel_order),
    path('api/submissions/<uuid:key>/', views.submission),
    path('api/claims/', views.claims), path('api/claims/<int:pk>/review/', views.review_claim),
]
