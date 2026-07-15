from django.conf import settings
from django.db import models
from django.db.models import Q


class PlayerEscortQualification(models.Model):
    STATUS_NONE = 'none'
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_SUSPENDED = 'suspended'
    STATUS_CHOICES = [
        (STATUS_NONE, '未申请'),
        (STATUS_PENDING, '审核中'),
        (STATUS_APPROVED, '已通过'),
        (STATUS_REJECTED, '未通过'),
        (STATUS_SUSPENDED, '已暂停'),
    ]

    player = models.OneToOneField(
        'players.Player',
        on_delete=models.CASCADE,
        related_name='escort_qualification',
        verbose_name='陪玩师',
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_NONE,
        db_index=True,
        verbose_name='护航资格状态',
    )
    review_note = models.CharField(max_length=300, blank=True, default='', verbose_name='审核备注')
    reviewed_at = models.DateTimeField(blank=True, null=True, verbose_name='审核时间')
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='reviewed_player_escort_qualifications',
        verbose_name='审核人',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'player_escort_qualifications'
        verbose_name = '护航资格'
        verbose_name_plural = '护航资格'

    def __str__(self):
        return f'{self.player.name} - {self.get_status_display()}'


class PlayerEscortApplication(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_CHOICES = [
        (STATUS_PENDING, '待审核'),
        (STATUS_APPROVED, '已通过'),
        (STATUS_REJECTED, '未通过'),
    ]

    player = models.ForeignKey(
        'players.Player',
        on_delete=models.CASCADE,
        related_name='escort_applications',
        verbose_name='陪玩师',
    )
    experience = models.TextField(verbose_name='护航经历与能力说明')
    evidence_urls = models.JSONField(default=list, blank=True, verbose_name='证明材料链接')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    reject_reason = models.CharField(max_length=300, blank=True, default='', verbose_name='未通过原因')
    review_note = models.CharField(max_length=300, blank=True, default='', verbose_name='审核备注')
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(blank=True, null=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='reviewed_escort_applications',
        verbose_name='审核人',
    )

    class Meta:
        db_table = 'player_escort_applications'
        verbose_name = '护航资格申请'
        verbose_name_plural = '护航资格申请'
        ordering = ['-submitted_at']
        constraints = [
            models.UniqueConstraint(
                fields=['player'],
                condition=Q(status='pending'),
                name='uniq_pending_player_escort_application',
            ),
        ]

    def __str__(self):
        return f'{self.player.name} - {self.get_status_display()}'
