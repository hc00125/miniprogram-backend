from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import SupportChannel, Complaint
from .serializers import ComplaintSerializer, ComplaintCreateSerializer
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination
from apps.accounts.authentication import LegacyPlayerTokenAuthentication, LenientJWTAuthentication
from rest_framework.authentication import SessionAuthentication


class ComplaintTokenAuthentication(LegacyPlayerTokenAuthentication):
    def authenticate(self, request):
        result = super().authenticate(request)
        if result and not result[0].is_active:
            from rest_framework.exceptions import AuthenticationFailed
            raise AuthenticationFailed('账号已停用')
        return result

    def authenticate_header(self, request):
        return 'Bearer'


class ComplaintAccessMixin:
    authentication_classes = [ComplaintTokenAuthentication, LenientJWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]


class ComplaintListCreateView(ComplaintAccessMixin, generics.ListCreateAPIView):
    serializer_class = ComplaintSerializer
    pagination_class = PageNumberPagination

    def get_queryset(self):
        return Complaint.objects.filter(user=self.request.user).select_related('order')

    def create(self, request, *args, **kwargs):
        serializer = ComplaintCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from .services import create_complaint
        complaint, created = create_complaint(request.user, serializer.validated_data)
        return Response(ComplaintSerializer(complaint).data, status=201 if created else 200)


class ComplaintAttachmentUploadView(ComplaintAccessMixin, generics.GenericAPIView):
    def post(self, request):
        from .services import upload_attachment
        from .serializers import ComplaintAttachmentSerializer
        attachment = upload_attachment(request.user, request.FILES.get('file'))
        return Response(ComplaintAttachmentSerializer(attachment).data, status=201)


class ComplaintAttachmentContentView(ComplaintAccessMixin, generics.GenericAPIView):
    def get(self, request, pk):
        from django.shortcuts import get_object_or_404
        from django.http import FileResponse, Http404
        from .models import ComplaintAttachment
        from .services import private_storage
        attachment = get_object_or_404(ComplaintAttachment.objects.select_related('complaint', 'message'), pk=pk)
        staff = request.user.is_staff and request.user.has_perm('support.view_complaint')
        if attachment.complaint_id:
            allowed = staff or (attachment.complaint.user_id == request.user.pk and not (attachment.message_id and attachment.message.is_internal))
        else:
            allowed = attachment.uploader_id == request.user.pk
        if not allowed:
            raise Http404
        try:
            response = FileResponse(private_storage().open(attachment.storage_name, 'rb'), content_type=attachment.content_type)
        except FileNotFoundError:
            raise Http404
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response


class ComplaintMessageView(ComplaintAccessMixin, generics.GenericAPIView):
    def post(self, request, number):
        from django.shortcuts import get_object_or_404
        from .serializers import ComplaintMessageCreateSerializer, ComplaintMessageSerializer
        from .services import add_complaint_message
        complaint = get_object_or_404(Complaint, number=number, user=request.user)
        serializer = ComplaintMessageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = add_complaint_message(complaint, request.user, **serializer.validated_data)
        return Response(ComplaintMessageSerializer(message).data, status=201)


class ComplaintDetailView(ComplaintAccessMixin, generics.RetrieveAPIView):
    serializer_class = ComplaintSerializer
    lookup_field = 'number'

    def get_queryset(self):
        return Complaint.objects.filter(user=self.request.user).select_related('order')

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
