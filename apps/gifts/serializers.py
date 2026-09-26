from rest_framework import serializers
from .models import Gift


class GiftSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Gift
        fields = ('code', 'name', 'image_url', 'price_diamonds', 'description')

    def get_image_url(self, obj):
        return self.context['request'].build_absolute_uri(obj.image.url)
