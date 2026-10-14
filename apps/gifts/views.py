from django.conf import settings
from django.core.exceptions import ValidationError
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from .models import Gift
from .serializers import GiftSerializer
from .validators import validate_gift_image


@api_view(['GET'])
@permission_classes([AllowAny])
def catalog(request):
    if not getattr(settings, 'GIFT_CATALOG_ENABLED', True):
        return Response({'detail': '礼物目录暂未开放'}, status=503)
    valid = []
    for gift in Gift.objects.filter(kind=Gift.KIND_GIFT, is_active=True).exclude(image='').order_by('sort_order', 'id'):
        try:
            validate_gift_image(gift.image)
        except ValidationError:
            continue
        finally:
            gift.image.close()
        valid.append(gift)
    paginator = PageNumberPagination()
    paginator.page_size = 20
    page = paginator.paginate_queryset(valid, request)
    return paginator.get_paginated_response(GiftSerializer(page, many=True, context={'request': request}).data)
