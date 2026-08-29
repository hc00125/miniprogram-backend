"""HTTP API for the static designated-play draft flow."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.content_security import SCENE_SOCIAL, ensure_texts_safe, user_openid

from .designated_drafts import (
    create_draft,
    quote_draft,
    serialize_draft,
    submit_draft,
    update_draft,
)
from .models import DesignatedOrderDraft
from .serializers import DesignatedDraftSerializer


def _input(request, *, partial=False):
    serializer = DesignatedDraftSerializer(data=request.data, partial=partial)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    ensure_texts_safe(
        [data.get('game_id'), data.get('boss_note')],
        openid=str(data.get('boss_wechat') or '') or user_openid(request.user),
        scene=SCENE_SOCIAL,
    )
    return data


def _draft_response(draft, quote=None):
    """Return the nested shape consumed by the designated-flow client.

    The duplicate top-level ``quote`` keeps the endpoint convenient for future
    lightweight clients while all draft state remains under ``draft``.
    """
    payload = serialize_draft(draft)
    if quote is not None:
        quote_payload = serialize_draft(draft, quote=quote)['quote']
        payload['quote'] = quote_payload
        return {'draft': payload, 'quote': quote_payload}
    return {'draft': payload}


@api_view(['POST', 'PUT'])
@permission_classes([IsAuthenticated])
def designated_drafts(request):
    """Create or update a selection-only draft; no client price is accepted."""
    if request.method == 'POST':
        data = _input(request)
        # Contact/game are collected at the final confirmation screen.  A
        # selection-only draft must still be quoteable before that step.
        required = {'base_spec_id'}
        missing = sorted(field for field in required if not data.get(field))
        if missing:
            return Response(
                {'detail': f'缺少必要参数：{", ".join(missing)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        draft = create_draft(user=request.user, payload=data)
        return Response(_draft_response(draft), status=status.HTTP_201_CREATED)

    data = _input(request, partial=True)
    draft_id = data.pop('draft_id', None)
    if not draft_id:
        return Response({'detail': '更新草稿必须提供 draft_id'}, status=status.HTTP_400_BAD_REQUEST)
    draft = update_draft(user=request.user, draft_id=draft_id, payload=data)
    return Response(_draft_response(draft))


@api_view(['PUT'])
@permission_classes([IsAuthenticated])
def designated_draft_detail(request, draft_id):
    """Path-parameter update alias for clients that prefer REST-style URLs."""
    data = _input(request, partial=True)
    data.pop('draft_id', None)
    draft = update_draft(user=request.user, draft_id=draft_id, payload=data)
    return Response(_draft_response(draft))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def quote(request, draft_id):
    # Quote can be shown before the virtual binding is configured.  Submission
    # performs the binding check before it creates any invitation.
    draft, result = quote_draft(user=request.user, draft_id=draft_id, require_virtual_binding=False)
    return Response(_draft_response(draft, quote=result))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def submit(request, draft_id):
    # The client may finalize the boss contact/note together with submit.  Run
    # that through the same draft validation path; prices are still ignored and
    # recomputed entirely on the server below.
    data = _input(request, partial=True)
    data.pop('draft_id', None)
    if data:
        update_draft(user=request.user, draft_id=draft_id, payload=data)
    order, created = submit_draft(user=request.user, draft_id=draft_id)
    order_payload = {
        'order_no': order.order_no,
        'status': order.status,
        'pricing_mode': order.pricing_mode,
        'total_price_per_hour': order.total_price_per_hour,
        'total_amount': order.total_amount,
        'created': created,
        'message': (
            '指定陪玩订单已提交，指定名额已发出邀请，其余名额已进入公开抢单大厅'
            if created else '该草稿已经提交过，已返回原订单'
        ),
    }
    draft = DesignatedOrderDraft.objects.select_related('submitted_order').get(pk=draft_id)
    return Response({
        'draft': serialize_draft(draft),
        'order': order_payload,
        **order_payload,
    }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
