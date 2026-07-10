from datetime import datetime

from django.contrib import admin
from django.db.models import Count, Q, Sum
from django.shortcuts import render
from django.urls import path

from apps.orders.models import Order

from .models import ClientProfile


@admin.register(ClientProfile)
class ClientProfileAdmin(admin.ModelAdmin):
    list_display = ['id', 'nickname', 'openid', 'player_status', 'created_at']
    search_fields = ['nickname', 'openid']
    list_filter = ['player_status']

    # ── 自定义视图：老板消费查询 ──────────────────────────

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'boss-consumption/',
                self.admin_site.admin_view(self.boss_consumption_view),
                name='accounts_clientprofile_boss_consumption',
            ),
        ]
        return custom_urls + urls

    def boss_consumption_view(self, request):
        query = request.GET.get('q', '').strip()
        date_from = request.GET.get('date_from', '').strip()
        date_to = request.GET.get('date_to', '').strip()
        total_all = request.GET.get('total_all', '') == '1'  # 勾选"全部"忽略日期

        results = []
        grand_total = 0
        grand_count = 0

        if query:
            profiles = ClientProfile.objects.filter(nickname__icontains=query)
            for profile in profiles:
                order_filter = Q(boss_wechat=profile.openid, status='已完成', paid=True)

                if not total_all:
                    if date_from:
                        try:
                            dt_from = datetime.strptime(date_from, '%Y-%m-%d')
                            order_filter &= Q(created_at__gte=dt_from)
                        except ValueError:
                            pass

                    if date_to:
                        try:
                            dt_to = datetime.strptime(
                                date_to + ' 23:59:59', '%Y-%m-%d %H:%M:%S'
                            )
                            order_filter &= Q(created_at__lte=dt_to)
                        except ValueError:
                            pass

                orders = Order.objects.filter(order_filter)
                aggregation = orders.aggregate(
                    total=Sum('total_amount'),
                    count=Count('id'),
                )

                total_spent = float(aggregation['total'] or 0)
                order_count = aggregation['count']
                recent_orders = orders.order_by('-created_at')[:10]

                results.append({
                    'profile': profile,
                    'total_spent': total_spent,
                    'order_count': order_count,
                    'recent_orders': recent_orders,
                })

                grand_total += total_spent
                grand_count += order_count

            results.sort(key=lambda r: r['total_spent'], reverse=True)

        context = {
            'title': '老板消费查询',
            'query': query,
            'date_from': date_from,
            'date_to': date_to,
            'total_all': total_all,
            'results': results,
            'grand_total': grand_total,
            'grand_count': grand_count,
            'opts': self.model._meta,
            'has_permission': request.user.is_active and request.user.is_staff,
            'site_header': self.admin_site.site_header,
            'site_title': self.admin_site.site_title,
            'is_popup': False,
            'is_nav_sidebar_enabled': False,
        }
        return render(request, 'admin/boss_consumption.html', context)
