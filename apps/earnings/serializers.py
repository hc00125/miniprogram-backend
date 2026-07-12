from rest_framework import serializers

from .models import PlayerEarning, Withdrawal


class PlayerEarningSerializer(serializers.ModelSerializer):
    order_no = serializers.CharField(source='order.order_no', read_only=True)
    package_name = serializers.SerializerMethodField()
    status_text = serializers.SerializerMethodField()

    class Meta:
        model = PlayerEarning
        fields = [
            'id', 'order_no', 'package_name', 'gross_amount', 'commission_rate',
            'commission_amount', 'net_amount', 'status', 'status_text', 'review_until',
            'available_at', 'available_amount', 'withdrawing_amount', 'withdrawn_amount',
            'freeze_reason', 'created_at',
        ]

    def get_package_name(self, obj):
        return obj.order.package_name_snapshot or getattr(obj.order.package, 'name', '') or '陪玩服务'

    def get_status_text(self, obj):
        if obj.status == PlayerEarning.STATUS_PENDING:
            return '审核中'
        if obj.status == PlayerEarning.STATUS_FROZEN:
            return '已冻结'
        if obj.status == PlayerEarning.STATUS_REVERSED:
            return '已冲销'
        if obj.withdrawing_amount > 0:
            return '提现中'
        if obj.withdrawn_amount >= obj.net_amount and obj.net_amount > 0:
            return '已提现'
        if obj.withdrawn_amount > 0:
            return '部分已提现'
        return '已审核'


class WithdrawalCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0.01)
    payment_method = serializers.ChoiceField(choices=Withdrawal.METHOD_CHOICES)
    account_name = serializers.CharField(max_length=80, trim_whitespace=True)
    account_no = serializers.CharField(max_length=150, trim_whitespace=True)
    request_note = serializers.CharField(required=False, allow_blank=True, max_length=300)


class WithdrawalSerializer(serializers.ModelSerializer):
    status_text = serializers.CharField(source='get_status_display', read_only=True)
    payment_method_text = serializers.CharField(source='get_payment_method_display', read_only=True)
    account_no_masked = serializers.SerializerMethodField()

    class Meta:
        model = Withdrawal
        fields = [
            'withdrawal_no', 'amount', 'status', 'status_text', 'payment_method',
            'payment_method_text', 'account_name', 'account_no_masked', 'request_note',
            'reject_reason', 'admin_note', 'transfer_no', 'proof_url', 'reviewed_at',
            'paid_at', 'created_at',
        ]

    def get_account_no_masked(self, obj):
        value = (obj.account_no or '').strip()
        if len(value) <= 4:
            return value
        return f"{'*' * min(8, len(value) - 4)}{value[-4:]}"
