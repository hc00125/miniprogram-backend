from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .boss_views import can_access_order, forbidden_response, get_order_or_response
from .replacement_fixes import replacement_payload
from .replacements import (
    publish_replacement_public,
    reassign_replacement,
    request_cancel_remaining,
)


class ReassignReplacementSerializer(serializers.Serializer):
    player_id = serializers.IntegerField(min_value=1)


def _accessible_order(request, order_no):
    order, error_response = get_order_or_response(order_no)
    if error_response:
        return None, error_response
    if not can_access_order(order, request.user):
        return None, forbidden_response()
    return order, None


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def detail(request, order_no):
    order, error_response = _accessible_order(request, order_no)
    if error_response:
        return error_response
    return Response(replacement_payload(order))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def publish_public(request, order_no):
    order, error_response = _accessible_order(request, order_no)
    if error_response:
        return error_response
    publish_replacement_public(order, request.user)
    return Response({
        'message': '该空缺名额已转为公开补位',
        'replacement': replacement_payload(order),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def reassign(request, order_no):
    order, error_response = _accessible_order(request, order_no)
    if error_response:
        return error_response
    serializer = ReassignReplacementSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    designation = reassign_replacement(
        order,
        serializer.validated_data['player_id'],
        request.user,
    )
    return Response({
        'message': f'已重新指定 {designation.player.name}，等待对方接受',
        'replacement': replacement_payload(order),
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cancel_remaining(request, order_no):
    order, error_response = _accessible_order(request, order_no)
    if error_response:
        return error_response
    request_cancel_remaining(order, request.user)
    return Response({
        'message': '已提交取消剩余服务申请，客服将核算未履行部分',
        'replacement': replacement_payload(order),
    })
