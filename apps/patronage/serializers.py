from rest_framework import serializers
from .pricing import PACKAGE_CHOICES
from .models import PatronagePurchase, CrownGrant


class PlayerInputSerializer(serializers.Serializer):
    player_id = serializers.IntegerField(min_value=1, max_value=9223372036854775807)


class QuoteInputSerializer(PlayerInputSerializer):
    package_code = serializers.ChoiceField(choices=PACKAGE_CHOICES)


class LiteralKeyField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail('invalid')
        return super().to_internal_value(data)


class PurchaseInputSerializer(QuoteInputSerializer):
    price_version = serializers.RegexField(r'\A[0-9a-f]{64}\Z', trim_whitespace=False)
    idempotency_key = LiteralKeyField(max_length=100, trim_whitespace=False)
    code = serializers.CharField(max_length=512, required=False, allow_blank=True, trim_whitespace=False)


class KeyInputSerializer(serializers.Serializer):
    idempotency_key = LiteralKeyField(max_length=100, trim_whitespace=False)


class RecordSerializer(serializers.ModelSerializer):
    player_id = serializers.IntegerField(read_only=True)
    # Keep the patronage wire contract independent of global DRF coercion.
    amount_yuan = serializers.DecimalField(
        max_digits=12, decimal_places=2, coerce_to_string=True, read_only=True,
    )
    amount_diamonds = serializers.SerializerMethodField()

    def get_amount_diamonds(self, obj):
        return f'{obj.amount_yuan * 10:.1f}'

    def to_representation(self, obj):
        data = super().to_representation(obj)
        from .service_duration import TERM_FIELDS
        snapshot = (getattr(obj, 'config_snapshot', None) or {}).get('quote', {})
        data.update({key: snapshot.get(key) for key in TERM_FIELDS})
        data['player_archived'] = obj.player.is_archived
        if obj.player.is_archived:
            data['player_name'] = obj.player_name + '（已离开）'
        attempt = obj.attempt
        if attempt:
            if attempt.status == 'unknown':
                data['payment_status'] = 'unknown'
            elif attempt.status in ('prepared', 'dispatching', 'succeeded'):
                data['payment_status'] = 'processing'
            elif attempt.status == 'failed':
                data['payment_status'] = 'failed'
            elif attempt.status == 'completed' and obj.payment_status != 'paid':
                data['payment_status'] = 'processing'
            if attempt.blocker:
                data['blockers'] = list(dict.fromkeys([*data['blockers'], attempt.blocker]))
        if data['payment_status'] == 'paid' and not (attempt and attempt.status == 'completed'
            and obj.paid_at and obj.starts_at and obj.expires_at and obj.expires_at > obj.starts_at
            and hasattr(obj, 'earning') and obj.crowns.exists()):
            data['payment_status'] = 'unknown'
            data['blockers'] = [*data['blockers'], 'FULFILLMENT_REVIEW_REQUIRED']
        if data['payment_status'] != 'paid':
            data['paid_at'] = data['starts_at'] = data['expires_at'] = None
        return data

    class Meta:
        model = PatronagePurchase
        fields = ('purchase_no', 'player_id', 'player_name', 'package_code', 'package_name',
                  'amount_yuan', 'amount_diamonds', 'payment_status', 'created_at', 'paid_at',
                  'starts_at', 'expires_at', 'bonus_naming_days', 'blockers',
                  'idempotency_key', 'price_version')
        read_only_fields = fields


class CrownSerializer(serializers.ModelSerializer):
    boss_name = serializers.SerializerMethodField()
    boss_avatar_url = serializers.SerializerMethodField()

    def get_boss_name(self, obj):
        profile = getattr(obj.purchase.boss, 'client_profile', None)
        return profile.nickname if profile and profile.nickname else '老板'

    def get_boss_avatar_url(self, obj):
        profile = getattr(obj.purchase.boss, 'client_profile', None)
        return profile.avatar_url if profile else ''

    class Meta:
        model = CrownGrant
        fields = ('id', 'boss_name', 'boss_avatar_url', 'package_name', 'starts_at', 'expires_at', 'source')
        read_only_fields = fields
