from rest_framework import serializers

from .models import ChatMessage, ChatReadStatus


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ['id', 'order_id', 'sender_type', 'sender_id', 'sender_name', 'content', 'message_type', 'created_at']


class ChatSendSerializer(serializers.Serializer):
    sender_type = serializers.ChoiceField(choices=['boss', 'player', 'admin'])
    sender_id = serializers.CharField(max_length=50)
    sender_name = serializers.CharField(max_length=50)
    content = serializers.CharField()
    message_type = serializers.CharField(required=False, default='text')


class ChatReadStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatReadStatus
        fields = ['reader_type', 'reader_id', 'last_read_at']
