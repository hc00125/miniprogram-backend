from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsApprovedPlayer, current_player

from .escort_qualification import get_escort_qualification, submit_escort_application
from .models import PlayerEscortApplication, PlayerEscortQualification
from .serializers import PlayerEscortApplicationCreateSerializer, PlayerEscortApplicationSerializer


def escort_payload(player):
    qualification = get_escort_qualification(player)
    pending = (
        player.escort_applications
        .filter(status=PlayerEscortApplication.STATUS_PENDING)
        .order_by('-submitted_at')
        .first()
    )
    latest = player.escort_applications.order_by('-submitted_at').first()
    can_submit = qualification.status in {
        PlayerEscortQualification.STATUS_NONE,
        PlayerEscortQualification.STATUS_PENDING,
        PlayerEscortQualification.STATUS_REJECTED,
    }
    return {
        'qualification': {
            'status': qualification.status,
            'status_text': qualification.get_status_display(),
            'has_qualification': qualification.status == PlayerEscortQualification.STATUS_APPROVED,
            'review_note': qualification.review_note,
            'reviewed_at': qualification.reviewed_at,
            'can_submit': can_submit,
        },
        'pending_application': PlayerEscortApplicationSerializer(pending).data if pending else None,
        'latest_application': PlayerEscortApplicationSerializer(latest).data if latest else None,
        'application_notice': '护航资格与娱乐陪、技术陪等等级独立。申请通过后才具备护航接单资格；商品端护航限制将在后续接入。',
    }


@api_view(['GET', 'POST'])
@permission_classes([IsApprovedPlayer])
def escort_qualification(request):
    player = current_player(request.user)
    if request.method == 'GET':
        return Response(escort_payload(player))

    serializer = PlayerEscortApplicationCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    application, _ = submit_escort_application(
        player,
        serializer.validated_data['experience'],
        serializer.validated_data['evidence_urls'],
    )
    return Response({
        'message': '护航资格申请已提交审核',
        'application': PlayerEscortApplicationSerializer(application).data,
        **escort_payload(player),
    })
