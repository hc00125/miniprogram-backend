from django.urls import path

from .views import ComplaintAttachmentUploadView, ComplaintAttachmentContentView, customer_service_center, ComplaintListCreateView, ComplaintDetailView, ComplaintMessageView


urlpatterns = [
    path('complaint-attachments/', ComplaintAttachmentUploadView.as_view(), name='complaint-attachment-upload'),
    path('complaint-attachments/<int:pk>/content/', ComplaintAttachmentContentView.as_view(), name='complaint-attachment-content'),
    path('complaints/<str:number>/messages/', ComplaintMessageView.as_view(), name='complaint-messages'),
    path('complaints/', ComplaintListCreateView.as_view(), name='complaint-list'),
    path('complaints/<str:number>/', ComplaintDetailView.as_view(), name='complaint-detail'),
    path('customer-service/', customer_service_center, name='support-customer-service'),
]
