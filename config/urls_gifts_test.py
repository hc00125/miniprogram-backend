from django.contrib import admin
from django.urls import include, path

urlpatterns = [path('admin/', admin.site.urls), path('api/gifts/', include('apps.gifts.urls')),
               path('api/boss/', include('apps.orders.boss_urls')),
               path('api/pay/', include('apps.payments.urls')),
               path('api/client/wallet/', include('apps.wallet.urls'))]
