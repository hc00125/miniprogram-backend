from rest_framework import serializers

from .models import SupportChannel


class SupportContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportChannel
        fields = [
            'id', 'name', 'wechat_id', 'service_hours',
            'description', 'audience', 'sort_order',
        ]
