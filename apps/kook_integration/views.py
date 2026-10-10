import uuid
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import APIException
from .authentication import StrictWechatJWTAuthentication
from .permissions import ApprovedWechatPlayer
from .secrets import bot_key, KookError
from . import binding, serializers
from .models import KookBinding, KookBindingChallenge, KookDelivery

def mode_flags():
    """Display-only mode, NOT per-user send permission or token/target readiness."""
    enabled = bool(getattr(settings, 'KOOK_ENABLED', False))
    return {'enabled': enabled, 'send_enabled': enabled and bool(getattr(settings, 'KOOK_SEND_ENABLED', False))}


def binding_result(obj):
    return {**mode_flags(), 'status':'bound','binding_id':obj.pk,'binding_version':str(obj.version), 'masked_kook_user':binding.masked(obj.kook_user_id), 'kook_display_name':obj.display_name, 'notifications_enabled':obj.notifications_enabled,'bound_at':obj.verified_at}

def validate(cls, request):
    instance=cls(data=request.data)
    instance.is_valid(raise_exception=True)
    return instance.validated_data

class PlayerView(APIView):
    authentication_classes = [StrictWechatJWTAuthentication]
    permission_classes = [IsAuthenticated, ApprovedWechatPlayer]

    def handle_exception(self, exc):
        if isinstance(exc, APIException):
            code = exc.get_codes()
            if not isinstance(code,str):
                code='INVALID_REQUEST'
            return Response({'code':code,'detail':str(exc.detail) if isinstance(exc,KookError) else 'Request rejected', 'request_id':str(uuid.uuid4())},status=exc.status_code)
        return super().handle_exception(exc)

    def active_binding(self, request):
        return KookBinding.objects.filter(player=request.kook_player, bot_key=bot_key(), active=True).first()

class BindingView(PlayerView):
    def get(self, request):
        current=self.active_binding(request)
        if current:
            return Response(binding_result(current))
        challenge=KookBindingChallenge.objects.filter(player=request.kook_player,bot_key=bot_key(),state__in=binding.ACTIVE_STATES,expires_at__gt=timezone.now()).order_by('-created_at').first()
        data={**mode_flags(), 'status':'unbound','notifications_enabled':False}
        if challenge:
            data.update(status=challenge.state,challenge_id=str(challenge.pk),masked_kook_user=binding.masked(challenge.candidate_kook_user_id),kook_display_name=challenge.kook_display_name)
        return Response(data)

    @transaction.atomic
    def patch(self, request):
        data=validate(serializers.ConsentSerializer,request)
        from apps.players.models import Player
        Player.objects.select_for_update().get(pk=request.kook_player.pk)
        obj=self.active_binding(request)
        if not obj:
            raise KookError('NOT_BOUND',404)
        obj.notifications_enabled=data['notifications_enabled']
        obj.save(update_fields=['notifications_enabled'])
        if not obj.notifications_enabled:
            binding.cancel_deliveries(obj)
        return Response(binding_result(obj))

    # WeChat native request supports POST; PATCH remains the same handler.
    post = patch

    def delete(self, request):
        binding.unbind(request.kook_player,validate(serializers.UnbindSerializer,request)['binding_version'])
        return Response(status=204)

class ChallengeCreateView(PlayerView):
    def post(self,request):
        c,code=binding.create_challenge(request.kook_player,validate(serializers.ChallengeSerializer,request)['purpose'])
        return Response({'challenge_id':str(c.pk),'code':code,'expires_at':c.expires_at,'bot_display_name':'KOOK通知机器人','instructions':'只私信机器人 bind <code>；发完回本页确认。不要向任何人提供绑定码。'},status=201)

class ChallengeView(PlayerView):
    def get(self,request,challenge_id):
        c=binding.owned_challenge(request.kook_player,challenge_id)
        state='expired' if c.state in binding.ACTIVE_STATES and c.expires_at<=timezone.now() else c.state
        data={'status':state,'expires_at':c.expires_at,'masked_kook_user':binding.masked(c.candidate_kook_user_id),'kook_display_name':c.kook_display_name}
        if state=='awaiting_confirmation':
            data['confirmation_nonce']=binding.confirmation_nonce(c)
        return Response(data)

    @transaction.atomic
    def delete(self,request,challenge_id):
        c=binding.owned_challenge(request.kook_player,challenge_id,True)
        if c.state in binding.ACTIVE_STATES:
            c.state='cancelled';c.save(update_fields=['state'])
        return Response(status=204)

class ConfirmView(PlayerView):
    def post(self,request,challenge_id):
        data=validate(serializers.ConfirmSerializer,request)
        obj=binding.confirm(request.kook_player,challenge_id,data['confirmation_nonce'],data['notifications_enabled'])
        return Response(binding_result(obj))

class TestNotificationView(PlayerView):
    def post(self,request):
        from .outbox import enqueue_test
        data=validate(serializers.TestSerializer,request)
        delivery=enqueue_test(request.kook_player,data['request_id'],request.META.get('REMOTE_ADDR',''))
        return Response({'delivery_id':str(delivery.pk),'status':delivery.status},status=202)

class TestStatusView(PlayerView):
    def get(self,request,delivery_id):
        obj=KookDelivery.objects.filter(pk=delivery_id,binding__player=request.kook_player,binding__bot_key=bot_key(),event__event_type='test.dm').first()
        if not obj:
            raise KookError('NOT_FOUND',404)
        state='queued' if obj.status=='inflight' else obj.status
        return Response({'status':state,'sent_at':obj.sent_at,'error_code':obj.last_error_code or None})

class EntryView(PlayerView):
    def get(self,request,opaque_intent):
        from .navigation import resolve_intent
        return Response(resolve_intent(opaque_intent,request.kook_player))
