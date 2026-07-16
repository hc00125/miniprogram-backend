from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import SupportChannel
from .serializers import SupportContactSerializer


VALID_AUDIENCES = {
    SupportChannel.AUDIENCE_BOSS,
    SupportChannel.AUDIENCE_PLAYER,
}


@api_view(['GET'])
@permission_classes([AllowAny])
def customer_service_center(request):
    audience = str(request.query_params.get('audience') or '').strip()
    audience_filter = Q(audience=SupportChannel.AUDIENCE_ALL)
    if audience in VALID_AUDIENCES:
        audience_filter |= Q(audience=audience)

    active_channels = SupportChannel.objects.filter(
        audience_filter,
        is_active=True,
    ).order_by('sort_order', 'id')

    official_enabled = active_channels.filter(
        channel_type=SupportChannel.TYPE_WECHAT_OFFICIAL,
    ).exists()
    contacts = active_channels.filter(
        channel_type=SupportChannel.TYPE_WECHAT_PERSONAL,
    )

    return Response({
        'official_customer_service_enabled': official_enabled,
        'contacts': SupportContactSerializer(contacts, many=True).data,
    })
