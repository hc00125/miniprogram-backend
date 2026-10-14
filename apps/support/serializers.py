from rest_framework import serializers

from .models import SupportChannel, Complaint


class ComplaintAttachmentSerializer(serializers.ModelSerializer):
    content_url = serializers.SerializerMethodField()

    def get_content_url(self, obj):
        from django.urls import reverse
        return reverse('complaint-attachment-content', kwargs={'pk': obj.pk})

    class Meta:
        from .models import ComplaintAttachment
        model = ComplaintAttachment
        fields = ['id', 'name', 'content_url']


class AttachmentInputSerializer(serializers.Serializer):
    attachment_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), max_length=3, default=list)

    def validate_attachment_ids(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError('附件不可重复')
        return value


class ComplaintMessageSerializer(serializers.ModelSerializer):
    attachments = ComplaintAttachmentSerializer(many=True, read_only=True)
    author_role = serializers.SerializerMethodField()

    def get_author_role(self, obj):
        return 'user' if obj.author_id == obj.complaint.user_id else 'staff'

    class Meta:
        from .models import ComplaintMessage
        model = ComplaintMessage
        fields = ['id', 'content', 'author_role', 'created_at', 'attachments']


class ComplaintMessageCreateSerializer(AttachmentInputSerializer):
    content = serializers.CharField(min_length=1, max_length=1000)


class ComplaintSerializer(serializers.ModelSerializer):
    attachments = serializers.SerializerMethodField()
    messages = serializers.SerializerMethodField()

    def get_attachments(self, obj):
        return ComplaintAttachmentSerializer(obj.attachments.filter(message__isnull=True), many=True).data

    status_history = serializers.SerializerMethodField()

    def get_messages(self, obj):
        return ComplaintMessageSerializer(obj.messages.filter(is_internal=False), many=True).data

    def get_status_history(self, obj):
        return list(obj.status_history.values('from_status', 'to_status', 'created_at'))

    order_no = serializers.CharField(source='order.order_no', default='')

    class Meta:
        model = Complaint
        fields = ['number', 'category', 'description', 'status', 'order_no', 'contact', 'created_at', 'updated_at', 'messages', 'status_history', 'attachments']


class ComplaintCreateSerializer(AttachmentInputSerializer):
    order_no = serializers.CharField(max_length=20, allow_blank=True, default="")
    category = serializers.ChoiceField(choices=Complaint.CATEGORY_CHOICES)
    description = serializers.CharField(min_length=10, max_length=1000)
    contact = serializers.CharField(max_length=100, allow_blank=True, default='')
    client_request_id = serializers.CharField(max_length=64)



class SupportContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportChannel
        fields = [
            'id', 'name', 'wechat_id', 'service_hours',
            'description', 'audience', 'sort_order',
        ]
