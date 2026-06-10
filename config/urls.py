from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(['GET'])
@permission_classes([AllowAny])
def health(_request):
    return Response({'status': 'ok'})


urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/health', health),
    path('api/client/', include('apps.accounts.urls')),
    path('api/boss/', include('apps.orders.boss_urls')),
    path('api/player/', include('apps.players.urls')),
    path('api/pay/', include('apps.payments.urls')),
    path('api/chat/', include('apps.chat.urls')),
    path('api/admin/', include('apps.admin_api.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

try:
    from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
except ImportError:
    pass
else:
    urlpatterns += [
        path('api/schema', SpectacularAPIView.as_view(), name='schema'),
        path('api/docs', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    ]
