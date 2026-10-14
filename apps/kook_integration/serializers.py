from rest_framework import serializers

class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError({'non_field_errors': ['Unknown or invalid request fields']})
        return super().to_internal_value(data)

class ChallengeSerializer(StrictSerializer):
    purpose = serializers.ChoiceField(choices=['bind','rebind'])

class ConsentSerializer(StrictSerializer):
    notifications_enabled = serializers.BooleanField()

class ConfirmSerializer(ConsentSerializer):
    confirmation_nonce = serializers.CharField(min_length=64, max_length=64)

class UnbindSerializer(StrictSerializer):
    binding_version = serializers.UUIDField()

class TestSerializer(StrictSerializer):
    request_id = serializers.UUIDField()
