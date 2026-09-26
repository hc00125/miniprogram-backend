from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package, PlayerType
from apps.earnings.models import PlayerEarning, PlayerWallet, WalletLedger, Withdrawal
from apps.orders.models import Order
from apps.payments.models import Payment, Refund
from apps.players.models import Player
from apps.wallet.models import ClientWallet, ClientWalletLedger, RechargeOrder

from .daily_billing import build_daily_billing_report


class DailyBillingReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.day = timezone.localdate()
        local_noon = timezone.make_aware(datetime.combine(cls.day, datetime.min.time()) + timedelta(hours=12))
        previous_day = local_noon - timedelta(days=1)

        cls.admin = User.objects.create_superuser('billing-admin', 'billing@example.com', 'password')
        boss_user = User.objects.create_user('billing-boss')
        profile = ClientProfile.objects.create(user=boss_user, openid='billing-openid', nickname='账单老板')
        client_wallet = ClientWallet.objects.create(profile=profile)

        package = Package.objects.create(name='账单测试商品', base_price=Decimal('80.00'), player_count=1)
        order = Order.objects.create(
            order_no='BILLING-ORDER-1',
            boss_user=boss_user,
            boss_wechat='billing-wechat',
            package=package,
            required_players=1,
            total_amount=Decimal('80.00'),
            status=Order.STATUS_COMPLETED,
            paid=True,
        )
        cls.profile = profile
        cls.order = order
        balance_payment = Payment.objects.create(
            payment_no='BILLING-PAY-BALANCE', order=order, channel='balance', scene='balance',
            amount=Decimal('30.00'), status='paid', paid_at=local_noon,
        )
        direct_payment = Payment.objects.create(
            payment_no='BILLING-PAY-DIRECT', order=order, channel='wechat_virtual',
            scene='short_series_goods', amount=Decimal('50.00'), status='paid', paid_at=local_noon,
        )
        old_payment = Payment.objects.create(
            payment_no='BILLING-PAY-OLD', order=order, channel='balance', scene='balance',
            amount=Decimal('999.00'), status='paid', paid_at=previous_day,
        )

        refund = Refund.objects.create(
            refund_no='BILLING-REFUND-1', payment=direct_payment, order=order,
            amount=Decimal('10.00'), status=Refund.STATUS_SUCCEEDED,
            notify_payload={
                'wechat_original_refund': {
                    'status': 'succeeded',
                    'succeeded_at': local_noon.isoformat(),
                },
            },
        )
        Refund.objects.filter(pk=refund.pk).update(updated_at=local_noon + timedelta(days=1))
        wallet_refund = Refund.objects.create(
            refund_no='BILLING-WALLET-REFUND', payment=balance_payment, order=order,
            amount=Decimal('5.00'), status=Refund.STATUS_SUCCEEDED,
            notify_payload={'balance_refund': {'automatic': True, 'settled_at': local_noon.isoformat()}},
        )
        Refund.objects.filter(pk=wallet_refund.pk).update(updated_at=local_noon + timedelta(days=1))

        recharge = RechargeOrder.objects.create(
            recharge_no='BILLING-RECHARGE-1', profile=profile, amount=Decimal('100.00'),
            status=RechargeOrder.STATUS_CREDITED, paid_at=local_noon, credited_at=local_noon,
        )
        linked_recharge = RechargeOrder.objects.create(
            recharge_no='BILLING-LINKED-RECHARGE', profile=profile, amount=Decimal('20.00'),
            status=RechargeOrder.STATUS_CREDITED, checkout_order_no=order.order_no,
            paid_at=local_noon, credited_at=local_noon,
        )

        cls.client_ledger = ClientWalletLedger.objects.create(
            wallet=client_wallet,
            entry_type=ClientWalletLedger.TYPE_VIRTUAL_PURCHASE_CREDIT,
            amount=Decimal('50.00'), balance_after=Decimal('50.00'),
            reference_id='BILLING-INTERNAL-CREDIT', note='内部购入钻石',
        )
        ClientWalletLedger.objects.filter(pk=cls.client_ledger.pk).update(created_at=local_noon)

        player_user = User.objects.create_user('billing-player-user')
        player_type = PlayerType.objects.create(name='账单陪玩类型', priority=1)
        player = Player.objects.create(user=player_user, name='账单陪玩师', player_type=player_type)
        player_wallet = PlayerWallet.objects.create(player=player)
        PlayerEarning.objects.create(
            order=order, player=player, gross_amount=Decimal('500.00'),
            commission_rate=Decimal('16.00'), commission_amount=Decimal('80.00'),
            net_amount=Decimal('420.00'), review_until=local_noon + timedelta(days=8),
        )
        PlayerEarning.objects.filter(order=order, player=player).update(created_at=local_noon)

        cls.player_ledger = WalletLedger.objects.create(
            wallet=player_wallet, entry_type=WalletLedger.TYPE_EARNING_CREATED,
            bucket=WalletLedger.BUCKET_PENDING, delta=Decimal('420.00'),
            balance_after=Decimal('420.00'), reference_id=order.order_no,
        )
        WalletLedger.objects.filter(pk=cls.player_ledger.pk).update(created_at=local_noon)

        withdrawal = Withdrawal.objects.create(
            withdrawal_no='BILLING-WITHDRAWAL-1', player=player, wallet=player_wallet,
            amount=Decimal('100.00'), status=Withdrawal.STATUS_PAID,
            account_name='账单陪玩师', account_no='test', paid_at=local_noon,
        )
        Withdrawal.objects.filter(pk=withdrawal.pk).update(created_at=local_noon, updated_at=local_noon)

    def test_range_includes_both_days_and_preserves_refund_event_times(self):
        self.client.force_login(self.admin)
        first = self.day - timedelta(days=1)
        response = self.client.get(reverse('admin_daily_billing'), {
            'start_date': first.isoformat(), 'end_date': self.day.isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['summary']['order_revenue'], Decimal('1079.00'))
        self.assertEqual(response.context['summary']['refund_amount'], Decimal('15.00'))
        self.assertEqual(response.context['summary']['cash_refund_amount'], Decimal('10.00'))
        self.assertEqual(response.context['start'].date(), first)
        self.assertEqual(response.context['end'].date(), self.day + timedelta(days=1))

    def test_cross_month_boundaries_apply_to_all_details_and_show_ledger_dates(self):
        from datetime import date
        from .daily_billing import SHANGHAI_TZ
        start = datetime(2026, 1, 31, tzinfo=SHANGHAI_TZ)
        end = datetime(2026, 2, 3, tzinfo=SHANGHAI_TZ)
        datasets = [
            (Payment, 'paid_at', 'payments'), (RechargeOrder, 'paid_at', 'recharges'),
            (PlayerEarning, 'created_at', 'earnings'), (Withdrawal, 'paid_at', 'withdrawals'),
            (ClientWalletLedger, 'created_at', 'boss_ledgers'),
            (WalletLedger, 'created_at', 'player_ledgers'),
        ]
        for moment, included in [(start - timedelta(microseconds=1), False), (start, True),
                                 (end - timedelta(microseconds=1), True), (end, False)]:
            for model, field, key in datasets:
                model.objects.all().update(**{field: moment})
            with timezone.override('UTC'):
                report = build_daily_billing_report(date(2026, 1, 31), date(2026, 2, 2))
            for model, field, key in datasets:
                self.assertEqual(bool(list(report[key])), included, (key, moment))
        ClientWalletLedger.objects.all().update(created_at=start)
        WalletLedger.objects.all().update(created_at=start)
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_daily_billing'), {
            'start_date': '2026-01-31', 'end_date': '2026-02-02',
        })
        self.assertContains(response, '<td>2026-01-31 00:00:00</td>',
                            count=ClientWalletLedger.objects.count() + WalletLedger.objects.count(), html=True)
        self.assertNotContains(response, '按当日工资创建时间')

    def test_report_aggregates_business_metrics_without_counting_internal_ledgers(self):
        report = build_daily_billing_report(self.day)

        self.assertEqual(report['summary']['recharge_received'], Decimal('100.00'))
        self.assertEqual(report['summary']['order_revenue'], Decimal('80.00'))
        self.assertEqual(report['summary']['refund_amount'], Decimal('15.00'))
        self.assertEqual(report['summary']['cash_refund_amount'], Decimal('10.00'))
        self.assertEqual(report['summary']['platform_commission_yuan'], Decimal('8.00'))
        self.assertEqual(report['summary']['player_wages_yuan'], Decimal('42.00'))
        self.assertEqual(report['summary']['withdrawal_yuan'], Decimal('10.00'))
        self.assertEqual(report['summary']['cash_received'], Decimal('170.00'))
        self.assertEqual(report['summary']['cash_receipt_count'], 3)
        self.assertEqual(report['summary']['net_cash_flow'], Decimal('150.00'))
        self.assertEqual(report['summary']['order_count'], 1)

    def test_shanghai_day_boundaries_do_not_follow_active_timezone(self):
        shanghai_midnight = timezone.make_aware(datetime.combine(self.day, datetime.min.time()))
        included = Payment.objects.create(
            payment_no='BILLING-BOUNDARY-IN', order=self.order, channel='balance', scene='balance',
            amount=Decimal('1.00'), status='paid', paid_at=shanghai_midnight,
        )
        excluded = Payment.objects.create(
            payment_no='BILLING-BOUNDARY-OUT', order=self.order, channel='balance', scene='balance',
            amount=Decimal('2.00'), status='paid', paid_at=shanghai_midnight + timedelta(days=1),
        )

        with timezone.override('UTC'):
            report = build_daily_billing_report(self.day)

        payment_ids = [row.pk for row in report['payments']]
        self.assertIn(included.pk, payment_ids)
        self.assertNotIn(excluded.pk, payment_ids)

    def test_mock_recharge_and_payment_are_excluded_from_business_totals(self):
        local_noon = timezone.make_aware(
            datetime.combine(self.day, datetime.min.time()) + timedelta(hours=12)
        )
        RechargeOrder.objects.create(
            recharge_no='BILLING-MOCK-RECHARGE', profile=self.profile, amount=Decimal('999.00'),
            channel=RechargeOrder.CHANNEL_MOCK, status=RechargeOrder.STATUS_CREDITED,
            paid_at=local_noon, credited_at=local_noon,
        )
        Payment.objects.create(
            payment_no='BILLING-MOCK-PAY', order=self.order, channel='wechat', scene='jsapi',
            amount=Decimal('888.00'), status='paid', paid_at=local_noon,
            qr_code='mockpay://BILLING-MOCK-PAY',
        )

        report = build_daily_billing_report(self.day)

        self.assertEqual(report['summary']['cash_received'], Decimal('170.00'))
        self.assertEqual(report['summary']['order_revenue'], Decimal('80.00'))
        self.assertEqual(report['summary']['cash_receipt_count'], 3)

    def test_report_includes_all_boss_and_player_ledger_rows(self):
        report = build_daily_billing_report(self.day)

        boss_ids = [row.pk for row in report['boss_ledgers']]
        player_ids = [row.pk for row in report['player_ledgers']]
        self.assertIn(self.client_ledger.pk, boss_ids)
        self.assertIn(self.player_ledger.pk, player_ids)
        # The report's contract is a Shanghai calendar day, even when the
        # isolated test settings use UTC. Do not make this assertion depend
        # on whether the test happens to run before Shanghai midnight.
        with timezone.override('Asia/Shanghai'):
            self.assertEqual(
                set(boss_ids),
                set(ClientWalletLedger.objects.filter(created_at__date=self.day).values_list('pk', flat=True)),
            )
            self.assertEqual(
                set(player_ids),
                set(WalletLedger.objects.filter(created_at__date=self.day).values_list('pk', flat=True)),
            )


class DailyBillingAdminViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('daily-admin', 'daily@example.com', 'password')

    def test_invalid_or_ambiguous_dates_are_rejected_without_report(self):
        self.client.force_login(self.admin)
        cases = [
            {'date': 'bad'}, {'date': '2026-02-30'}, {'date': ''},
            {'start_date': '2026-03-02', 'end_date': '2026-03-01'},
            {'start_date': '2026-03-01'}, {'end_date': '2026-03-01'},
            {'start_date': '', 'end_date': ''}, {'date': '9999-12-31'},
            {'date': '2026-1-2'}, {'preset': 'unknown'},
            {'date': '2026-03-01', 'start_date': '2026-03-01', 'end_date': '2026-03-02'},
            {'preset': 'today', 'date': '2026-03-01'},
            {'date': ['2026-03-01', '2026-03-02']},
        ]
        for params in cases:
            with self.subTest(params=params):
                response = self.client.get(reverse('admin_daily_billing'), params)
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, '日期筛选错误', status_code=400)
                self.assertNotContains(response, 'class="metrics"', status_code=400)

    def test_presets_use_shanghai_today_and_calendar_boundaries(self):
        from unittest.mock import patch
        from datetime import timezone as dt_timezone
        self.client.force_login(self.admin)
        cases = [
            (datetime(2026, 3, 1, 16, 30, tzinfo=dt_timezone.utc), {
                'today': ('2026-03-02', '2026-03-02'),
                'this_week': ('2026-03-02', '2026-03-08'),
                'this_month': ('2026-03-01', '2026-03-31'),
                'last_month': ('2026-02-01', '2026-02-28'),
            }),
            (datetime(2024, 3, 1, tzinfo=dt_timezone.utc), {
                'last_month': ('2024-02-01', '2024-02-29'),
                'this_week': ('2024-02-26', '2024-03-03'),
            }),
            (datetime(2026, 1, 1, tzinfo=dt_timezone.utc), {
                'last_month': ('2025-12-01', '2025-12-31'),
            }),
        ]
        for now, presets in cases:
            for preset, expected in presets.items():
                with self.subTest(preset=preset, now=now), timezone.override('UTC'), patch(
                    'apps.admin_api.daily_billing.timezone.now', return_value=now
                ):
                    response = self.client.get(reverse('admin_daily_billing'), {'preset': preset})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context['start'].date().isoformat(), expected[0])
                    self.assertEqual((response.context['end'] - timedelta(days=1)).date().isoformat(), expected[1])

    def test_range_page_has_separate_forms_actual_bounds_and_working_links(self):
        from html.parser import HTMLParser
        class Links(HTMLParser):
            def __init__(self):
                super().__init__()
                self.urls = []
            def handle_starttag(self, tag, attrs):
                href = dict(attrs).get('href', '')
                if tag == 'a' and href.startswith('?'):
                    self.urls.append(href)
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_daily_billing'), {
            'start_date': '2026-01-31', 'end_date': '2026-02-02',
        })
        for text in ('name="start_date"', 'name="end_date"', 'name="date"',
                     '2026-01-31 至 2026-02-02', '2026-02-03 00:00',
                     '本周', '本月', '上月', '所选范围内无支付记录'):
            self.assertContains(response, text)
        parser = Links()
        parser.feed(response.content.decode())
        self.assertEqual(len([u for u in parser.urls if 'preset=' in u]), 4)
        for link in parser.urls:
            self.assertEqual(self.client.get(reverse('admin_daily_billing') + link).status_code, 200)
        single = self.client.get(reverse('admin_daily_billing'), {'date': '2026-01-31'})
        self.assertContains(single, '?date=2026-01-30')
        self.assertContains(single, '?date=2026-02-01')
        self.assertEqual(single.context['start'].date().isoformat(), '2026-01-31')
        self.assertEqual(single.context['end'].date().isoformat(), '2026-02-01')

    def test_range_permissions_match_single_day_permissions(self):
        url = reverse('admin_daily_billing') + '?start_date=2026-01-31&end_date=2026-02-02'
        self.assertEqual(self.client.get(url).status_code, 302)
        user = User.objects.create_user('range-non-staff', is_staff=False)
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 302)
        user.is_staff = True
        user.save(update_fields=['is_staff'])
        self.assertEqual(self.client.get(url).status_code, 403)
        user.user_permissions.add(Permission.objects.get(
            content_type__app_label='wallet', codename='view_clientwalletledger'))
        self.assertEqual(self.client.get(url).status_code, 403)
        user.user_permissions.add(Permission.objects.get(
            content_type__app_label='earnings', codename='view_walletledger'))
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_staff_can_open_daily_billing_page(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse('admin_daily_billing'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '每日账单')
        self.assertContains(response, '老板钻石流水')
        self.assertContains(response, '陪玩钱包流水')

    def test_staff_with_both_finance_permissions_can_open_page(self):
        staff = User.objects.create_user('finance-staff', password='password', is_staff=True)
        staff.user_permissions.add(
            Permission.objects.get(content_type__app_label='wallet', codename='view_clientwalletledger'),
            Permission.objects.get(content_type__app_label='earnings', codename='view_walletledger'),
        )
        self.client.force_login(staff)

        response = self.client.get('/admin/daily-billing/')

        self.assertEqual(response.status_code, 200)

    def test_staff_without_finance_permissions_is_forbidden(self):
        staff = User.objects.create_user('ordinary-staff', password='password', is_staff=True)
        self.client.force_login(staff)

        response = self.client.get('/admin/daily-billing/')

        self.assertEqual(response.status_code, 403)

    def test_non_staff_is_redirected_to_admin_login(self):
        response = self.client.get('/admin/daily-billing/')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)
