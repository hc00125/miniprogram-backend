from django.contrib import admin
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from .archive import archive_players, restore_players
from .models import Player


class ArchiveFilter(admin.SimpleListFilter):
    title = '移除状态'
    parameter_name = 'archive'

    def lookups(self, request, model_admin):
        return [('active', '未移除'), ('archived', '已移除'), ('all', '全部')]

    def queryset(self, request, queryset):
        if self.value() == 'all':
            return queryset
        return queryset.filter(is_archived=self.value() == 'archived')


def preview(players):
    from apps.earnings.models import PlayerWallet, Withdrawal
    from apps.orders.models import Order
    from apps.patronage.models import CrownGrant, PatronagePurchase
    rows = []
    for player in players:
        orders = Order.objects.filter(Q(order_players__player=player) | Q(target_player=player) | Q(designations__player=player)).exclude(status__in=[Order.STATUS_COMPLETED, Order.STATUS_CANCELLED]).distinct()
        wallet = PlayerWallet.objects.filter(player=player).values('pending_balance', 'available_balance', 'withdrawing_balance', 'withdrawn_total', 'debt_balance').first()
        withdrawals = list(Withdrawal.objects.filter(player=player, status__in=['pending_review', 'pending_payment', 'failed']).values('id', 'withdrawal_no', 'amount', 'status'))
        crowns = list(CrownGrant.objects.filter(purchase__player=player, revoked_at__isnull=True, expires_at__gt=timezone.now()).values('id', 'purchase__purchase_no', 'expires_at'))
        pending = list(PatronagePurchase.objects.filter(player=player, payment_status__in=['created', 'processing', 'unknown']).values('id', 'purchase_no', 'payment_status'))
        rows.append(dict(player=player, orders=list(orders.values('id', 'order_no', 'status')), wallet=wallet,
                         withdrawals=withdrawals, crowns=crowns, pending=pending))
    return rows


class PlayerArchiveAdminMixin:
    actions = ['archive_selected', 'restore_selected']
    change_form_template = 'admin/players/player/change_form.html'

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        return [path('<int:object_id>/archive/', self.admin_site.admin_view(self.archive_one), name='players_player_archive')] + super().get_urls()

    def archive_one(self, request, object_id):
        return self._archive_action(request, Player.objects.filter(pk=object_id), single=True)

    @admin.action(description='移除选中的陪玩（预览并确认）', permissions=['change'])
    def archive_selected(self, request, queryset):
        return self._archive_action(request, queryset)

    @admin.action(description='恢复选中的陪玩资料（保持下线）', permissions=['change'])
    def restore_selected(self, request, queryset):
        return self._archive_action(request, queryset, restore=True)

    def _archive_action(self, request, queryset, restore=False, single=False):
        if not self.has_change_permission(request):
            raise PermissionDenied
        players = list(queryset.order_by('pk'))
        if any(not self.has_change_permission(request, p) for p in players):
            raise PermissionDenied
        identity = [[p.pk, p.name, p.is_archived] for p in players]
        payload = {'identity': identity, 'restore': restore, 'actor': request.user.pk}
        error = ''
        if request.method == 'POST' and request.POST.get('confirm') == 'yes':
            try:
                signed = signing.loads(request.POST.get('preview_token', ''), salt='player-archive', max_age=1800)
                with transaction.atomic():
                    locked = list(Player.objects.select_for_update().filter(pk__in=[p.pk for p in players]).order_by('pk'))
                    current = {'identity': [[p.pk, p.name, p.is_archived] for p in locked], 'restore': restore, 'actor': request.user.pk}
                    if signed != current:
                        raise ValidationError('选择或昵称已变化，请重新预览')
                    fn = restore_players if restore else archive_players
                    fn([p.pk for p in locked], actor=request.user, reason=request.POST.get('reason'))
                    for p in locked:
                        self.log_change(request, p, ('恢复资料：' if restore else '移除陪玩：') + request.POST['reason'])
                self.message_user(request, '操作完成。历史和资金未变；恢复后仍保持下线。')
                return HttpResponseRedirect(reverse('admin:players_player_changelist'))
            except (signing.BadSignature, ValidationError) as exc:
                error = '确认失效或原因未填写，请重新核对：' + str(exc)
        return TemplateResponse(request, 'admin/players/player/archive_confirm.html', {
            **self.admin_site.each_context(request), 'opts': self.model._meta,
            'title': '恢复陪玩资料' if restore else '移除陪玩', 'rows': preview(players),
            'restore': restore, 'single': single, 'error': error,
            'action_name': 'restore_selected' if restore else 'archive_selected',
            'preview_token': signing.dumps(payload, salt='player-archive'),
        })
