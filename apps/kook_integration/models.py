import uuid
from django.db import models
from django.db.models import Q
from django.utils import timezone

class KookBinding(models.Model):
    bot_key = models.CharField(max_length=80)
    player = models.ForeignKey('players.Player', on_delete=models.CASCADE)
    kook_user_id = models.CharField(max_length=80)
    display_name = models.CharField(max_length=100, blank=True)
    version = models.UUIDField(default=uuid.uuid4, unique=True)
    active = models.BooleanField(default=True)
    verified_at = models.DateTimeField(default=timezone.now)
    notifications_enabled = models.BooleanField(default=False)
    revoked_at = models.DateTimeField(null=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['bot_key', 'player'], condition=Q(active=True), name='kook_active_player'), models.UniqueConstraint(fields=['bot_key', 'kook_user_id'], condition=Q(active=True), name='kook_active_user')]

class KookBindingChallenge(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    player = models.ForeignKey('players.Player', on_delete=models.CASCADE)
    bot_key = models.CharField(max_length=80)
    purpose = models.CharField(max_length=10)
    digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    state = models.CharField(max_length=30, default='pending')
    candidate_kook_user_id = models.CharField(max_length=80, blank=True)
    kook_display_name = models.CharField(max_length=100, blank=True)
    proof_event_id = models.CharField(max_length=150, blank=True)
    confirmation_nonce_digest = models.CharField(max_length=64, blank=True)
    consumed_at = models.DateTimeField(null=True)
    binding = models.ForeignKey(KookBinding, null=True, on_delete=models.SET_NULL)

class KookInboundEvent(models.Model):
    bot_key = models.CharField(max_length=80)
    event_key = models.CharField(max_length=160)
    sn = models.PositiveIntegerField(default=0)
    event_time = models.BigIntegerField(default=0)
    received_at = models.DateTimeField(default=timezone.now)
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=20, default='queued')
    lease_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    attempt_count = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=80, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['bot_key', 'event_key'], name='kook_inbound_unique')]

class KookOrderProjection(models.Model):
    order = models.OneToOneField('orders.Order', on_delete=models.CASCADE)
    revision = models.PositiveIntegerField(default=0)
    canonical_state_hash = models.CharField(max_length=64, blank=True)
    public_epoch = models.PositiveIntegerField(default=0)
    latest_safe_snapshot = models.JSONField(default=dict)

class KookOutboxEvent(models.Model):
    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    schema_version = models.PositiveSmallIntegerField(default=1)
    event_type = models.CharField(max_length=40)
    order_id = models.PositiveBigIntegerField(null=True)
    designation_id = models.PositiveBigIntegerField(null=True)
    revision = models.PositiveIntegerField(default=1)
    occurred_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    payload = models.JSONField(default=dict)
    source_key = models.CharField(max_length=200, unique=True)

class KookMessageProjection(models.Model):
    bot_key = models.CharField(max_length=80)
    scope = models.CharField(max_length=10)
    target_id = models.CharField(max_length=80)
    order_id = models.PositiveBigIntegerField(default=0)
    designation_key = models.CharField(max_length=80, default='')
    public_epoch = models.PositiveIntegerField(default=0)
    remote_message_id = models.CharField(max_length=100, blank=True)
    applied_revision = models.PositiveIntegerField(default=0)
    desired_revision = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=20, default='new')
    lease_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['bot_key','scope','target_id','order_id','designation_key','public_epoch'], name='kook_unique_message_stream')]

class KookDelivery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(KookOutboxEvent, on_delete=models.CASCADE)
    operation = models.CharField(max_length=10, default='create')
    scope = models.CharField(max_length=10)
    target_id = models.CharField(max_length=80)
    binding = models.ForeignKey(KookBinding, null=True, on_delete=models.SET_NULL)
    binding_version = models.UUIDField(null=True)
    stream = models.ForeignKey(KookMessageProjection, null=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=20, default='queued', db_index=True)
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    remote_message_id = models.CharField(max_length=100, blank=True)
    last_error_code = models.CharField(max_length=80, blank=True)
    sent_at = models.DateTimeField(null=True)
    payload_hash = models.CharField(max_length=64, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['event','scope','target_id','operation'], name='kook_unique_delivery')]
        permissions = [('send_kook_test', 'Send fixed KOOK test notification')]

class KookRateLimit(models.Model):
    # Bucket names are hashed (bounded, no accidental sensitive response data).
    # All buckets conservatively gate the bot until route mappings are verified.
    bot_key = models.CharField(max_length=80)
    key = models.CharField(max_length=64)
    blocked_until = models.DateTimeField()
    class Meta:
        constraints = [models.UniqueConstraint(fields=['bot_key', 'key'], name='kook_unique_rate_gate')]


class KookEntryIntent(models.Model):
    digest = models.CharField(max_length=64, unique=True)
    scope = models.CharField(max_length=10)
    order_id = models.PositiveBigIntegerField()
    designation_id = models.PositiveBigIntegerField(null=True)
    binding = models.ForeignKey(KookBinding, null=True, on_delete=models.SET_NULL)
    binding_version = models.UUIDField(null=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True)
    url_link = models.URLField(blank=True)

class KookAuditLog(models.Model):
    actor = models.CharField(max_length=100)
    action = models.CharField(max_length=80)
    object_id = models.CharField(max_length=100, blank=True)
    request_id = models.CharField(max_length=80, blank=True)
    result = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
