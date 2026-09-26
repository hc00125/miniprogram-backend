from django.db.models import Prefetch
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.catalog.models import GameService, PackageGroup
from apps.catalog.serializers import GameServiceSerializer


@api_view(['GET'])
@permission_classes([AllowAny])
def catalog_navigation(request):
    games = (
        GameService.objects
        .filter(is_active=True)
        .order_by('sort_order', 'id')
        .prefetch_related(
            Prefetch(
                'package_groups',
                queryset=PackageGroup.objects.filter(is_active=True).order_by('sort_order', 'id'),
                to_attr='active_groups',
            )
        )
    )
    return Response({
        'games': GameServiceSerializer(games, many=True, context={'request': request}).data,
    })
