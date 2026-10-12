"""Discovery contract: synthetic fixtures, real endpoint/ORM/PostgreSQL."""
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from types import SimpleNamespace
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package, PlayerType
from apps.orders.models import Order, OrderPlayer
from apps.players.models import Player, PlayerProfileUpdateRequest

NOW = datetime(2026, 10, 11, 12, tzinfo=ZoneInfo('Asia/Shanghai'))


class LegacyDiscoveryCompatibilityTests(SimpleTestCase):
    def test_presence_works_without_unreleased_archive_field(self):
        from apps.players.discovery import presence_online
        legacy_player = SimpleNamespace(presence_seen_at=NOW)
        self.assertTrue(presence_online(legacy_player, NOW))
        self.assertFalse(presence_online(legacy_player, NOW + timedelta(seconds=900)))


@override_settings(FEATURE_DESIGNATE_DISABLED=False)
class DiscoveryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.clock = patch('django.utils.timezone.now', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.kind = PlayerType.objects.create(name='发现测试类型', priority=1)
        self.package = Package.objects.create(name='发现测试服务', player_count=1, base_price=10)
        self.serial = 0

    def player(self, name, **kwargs):
        return Player.objects.create(name=name, player_type=self.kind, **kwargs)

    def order(self, player, at=NOW, status=Order.STATUS_COMPLETED, member='已完成', **kwargs):
        self.serial += 1
        order = Order.objects.create(order_no=f'DISC{self.serial}', package=self.package,
            required_players=1, status=status, paid=True, total_amount=10, **kwargs)
        relation = OrderPlayer.objects.create(order=order, player=player, status=member)
        OrderPlayer.objects.filter(pk=relation.pk).update(grab_time=at)
        return order

    def rows(self, mode='quality', **params):
        response = self.client.get('/api/player/list', {'discovery': mode, **params})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def ids(self, mode='quality', **params):
        return [row['id'] for row in self.rows(mode, **params)]

    def test_quality_available_group_precedes_higher_score_resting_player(self):
        resting = self.player('高分休息', is_online=False, bio='完整简介', audio_intro_url='https://test.invalid/a')
        for _ in range(20):
            self.order(resting)
        forbidden = self.player('高分但禁止接单', is_online=True, can_accept_orders=False,
                                presence_seen_at=NOW)
        for _ in range(20):
            self.order(forbidden)
        available = self.player('可接单新人', is_online=True, can_accept_orders=True)
        self.assertEqual(self.ids(), [available.pk, forbidden.pk, resting.pk])

    def test_quality_real_recent_completion_beats_stars_and_legacy_total(self):
        legacy = self.player('旧评分高', is_online=True, total_orders=999999,
                             rating_count=100, total_rating=500)
        completed = self.player('真实近期完单', is_online=True)
        self.order(completed)
        self.assertEqual(self.ids(ordering='-total_orders'), [completed.pk, legacy.pk])
        Player.objects.filter(pk=legacy.pk).update(total_orders=0, rating_count=0, total_rating=0)
        self.assertEqual(self.ids(), [completed.pk, legacy.pk])

    def test_final_order_and_member_gates_exclude_cancelled_exited_and_nonmembers(self):
        polluted = self.player('非完单', is_online=True)
        verified = self.player('确实完单', is_online=True)
        self.order(verified)
        for status in (Order.STATUS_WAITING, Order.STATUS_PENDING_PAYMENT,
                       Order.STATUS_READY_TO_START, Order.STATUS_IN_PROGRESS, Order.STATUS_CANCELLED):
            self.order(polluted, status=status)
        for member in ('已接单', '订单已取消', '已退出'):
            self.order(polluted, member=member)
        # A third player completing service does not credit a nonmember.
        third = self.player('别人成员', is_publicly_visible=False)
        self.order(third)
        self.assertEqual(self.ids(), [verified.pk, polluted.pk])

    def test_real_completion_renewal_and_cancellations_credit_only_completed_root(self):
        from apps.orders.models import OrderItem, OrderStatusLog
        from apps.orders.renewals import create_renewal_order, finalize_paid_renewal
        from apps.orders.services import complete_order, cancel_order
        from apps.orders.player_cancellations import cancel_player_order
        owner = User.objects.create_user(username='discovery-renewal-owner')
        player = self.player('服务成员', is_online=True)
        reference = self.player('已有两单', is_online=True)
        self.order(reference)
        self.order(reference)
        root = self.order(player, status=Order.STATUS_IN_PROGRESS, member='已接单', boss_user=owner)
        root.required_players = 2
        root.save(update_fields=['required_players'])
        second = self.player('第二成员', is_publicly_visible=False)
        OrderPlayer.objects.create(order=root, player=second)
        OrderItem.objects.create(order=root, package=self.package, package_name='测试根服务',
            unit_price=10, quantity=1, amount=10)
        renewal, created = create_renewal_order(root.order_no, owner)
        self.assertTrue(created)
        renewal.paid = True
        renewal.save(update_fields=['paid'])
        finalize_paid_renewal(renewal)
        renewal.refresh_from_db()
        self.assertEqual(renewal.status, Order.STATUS_COMPLETED)
        renewal.order_players.update(status='已完成')
        # Copied renewal memberships are not another completed service.
        self.assertEqual(self.ids(), [reference.pk, player.pk])
        complete_order(root, player)
        self.assertEqual(self.ids(), [reference.pk, player.pk])
        complete_order(root, second)
        root.refresh_from_db()
        self.assertEqual(root.status, Order.STATUS_COMPLETED)
        self.assertEqual(root.order_players.get(player=player).grab_time, NOW)
        for _ in range(5):
            OrderStatusLog.objects.create(order=root, to_status=Order.STATUS_COMPLETED, reason='重复日志')
        waiting = self.order(player, status=Order.STATUS_WAITING, member='已接单')
        Order.objects.filter(pk=waiting.pk).update(paid=False)
        waiting.refresh_from_db()
        cancel_order(waiting)
        leaving = self.order(player, status=Order.STATUS_WAITING, member='已接单')
        Order.objects.filter(pk=leaving.pk).update(paid=False)
        cancel_player_order(leaving.order_no, player, '测试退出')
        self.assertFalse(leaving.order_players.exists())
        self.assertEqual(self.ids(), [reference.pk, player.pk])

    def test_historical_verified_orders_add_twenty_points_without_recent_orders(self):
        historical = self.player('历史经验', is_online=True)
        recent = self.player('近期一单', is_online=True)
        for _ in range(20):
            self.order(historical, at=NOW - timedelta(days=90))
        self.order(recent)
        self.assertEqual(self.ids(), [historical.pk, recent.pk])

    def test_recent_acceptance_window_is_inclusive_and_not_updated_or_completion_time(self):
        from .discovery import completion_counts
        player = self.player('时间边界', is_online=True)
        for at in (NOW - timedelta(days=30, microseconds=1), NOW - timedelta(days=30),
                   NOW, NOW + timedelta(microseconds=1)):
            self.order(player, at=at)
        self.assertEqual(completion_counts([player.pk], NOW), {player.pk: {'recent': 2, 'history': 4}})
        self.assertEqual(completion_counts([], NOW), {})

    def test_fixed_recent_ten_and_history_twenty_caps_do_not_rescale_after_filtering(self):
        from .discovery import quality_score
        player = self.player('封顶测试', is_online=True)
        for recent, history, expected in ((0, 0, 0), (1, 1, 7), (9, 19, 73),
                (10, 20, 80), (11, 21, 80), (1000, 1000, 80)):
            with self.subTest(recent=recent, history=history):
                self.assertEqual(quality_score(player, {'recent': recent, 'history': history}, NOW), expected)
        extra_recent = self.player('封顶 多近期', is_online=True)
        capped = self.player('封顶 满历史', is_online=True)
        for _ in range(11):
            self.order(extra_recent)
        for _ in range(10):
            self.order(capped)
            self.order(capped, at=NOW - timedelta(days=60))
        self.assertEqual(self.ids(search='封顶 ')[0], capped.pk)

    def test_presence_has_ten_points_only_in_zero_to_899_seconds_not_acceptance_switch(self):
        from .discovery import quality_score
        player = self.player('活跃边界', is_online=False, can_accept_orders=False)
        for age, expected in ((None, 0), (-1, 0), (0, 10), (899.999999, 10), (900, 0), (901, 0)):
            player.presence_seen_at = None if age is None else NOW - timedelta(seconds=age)
            with self.subTest(age=age):
                self.assertEqual(quality_score(player, {}, NOW), expected)
        player.presence_seen_at = NOW
        player.save(update_fields=['presence_seen_at'])
        absent = self.player('不活跃休息', is_online=False)
        self.assertEqual(self.ids(), [player.pk, absent.pk])

    def test_new_mode_presence_dto_uses_the_same_single_request_clock(self):
        self.player('活跃甲', presence_seen_at=NOW)
        self.player('活跃乙', presence_seen_at=NOW)
        with patch('django.utils.timezone.now', side_effect=[NOW, NOW + timedelta(seconds=900),
                                                            NOW + timedelta(seconds=1800)]) as clock:
            rows = self.rows()
        self.assertEqual(clock.call_count, 1)
        self.assertTrue(all(row['presence_online'] for row in rows))

    def test_profile_partial_points_are_exact_and_pending_or_whitespace_adds_nothing(self):
        from fractions import Fraction
        from .discovery import quality_score
        user = User.objects.create_user(username='discovery-profile')
        profile = ClientProfile.objects.create(user=user, openid='synthetic-profile-openid', avatar_url='')
        player = self.player('资料分数', user=user)
        PlayerProfileUpdateRequest.objects.create(player=player, bio='尚未审核',
                                                  audio_intro_url='https://test.invalid/pending')
        self.assertEqual(quality_score(player, {}, NOW), 0)
        for count, avatar, bio, audio in (
                (0, ' \t', '\n', ' '), (1, '', '真实简介', ''),
                (2, '', '真实简介', 'https://test.invalid/voice'),
                (3, 'https://test.invalid/avatar', '真实简介', 'https://test.invalid/voice')):
            profile.avatar_url = avatar
            profile.save(update_fields=['avatar_url'])
            player = Player.objects.select_related('user__client_profile').get(pk=player.pk)
            player.bio, player.audio_intro_url = bio, audio
            with self.subTest(count=count):
                self.assertEqual(quality_score(player, {}, NOW), Fraction(10 * count, 3))
                self.assertIsInstance(quality_score(player, {}, NOW), Fraction)
        player.presence_seen_at = NOW
        self.assertEqual(quality_score(player, {'recent': 100, 'history': 100}, NOW), 100)

    def test_profile_fraction_sorting_precedes_blank_and_does_not_round_to_integer(self):
        blank = self.player('空资料', is_online=True)
        partial = self.player('一项资料', is_online=True, bio='已审核简介')
        historical = self.player('三单历史', is_online=True)
        for _ in range(3):
            self.order(historical, at=NOW - timedelta(days=60))
        self.assertEqual(self.ids(), [partial.pk, historical.pk, blank.pk])

    def test_quality_equal_scores_cycle_daily_in_beijing_and_ignore_legacy_ordering(self):
        players = [self.player(f'同分{i}', is_online=True, total_orders=i * 100) for i in range(4)]
        first = self.ids(ordering='-total_orders')
        self.assertEqual(first, self.ids(ordering='total_orders'))
        self.assertEqual(first, self.ids())
        heads = []
        for day in range(4):
            with patch('django.utils.timezone.now', return_value=NOW + timedelta(days=day)):
                current = self.ids()
            heads.append(current[0])
            self.assertCountEqual(current, [p.pk for p in players])
            if day == 1:
                self.assertEqual(current, first[1:] + first[:1])
        self.assertEqual(len(set(heads)), 4)
        midnight = NOW.replace(hour=0)
        with patch('django.utils.timezone.now', return_value=midnight - timedelta(microseconds=1)):
            before = self.ids()
        with patch('django.utils.timezone.now', return_value=midnight.astimezone(ZoneInfo('UTC'))):
            after = self.ids()
        self.assertEqual(after, before[1:] + before[:1])

    def test_random_same_seed_stable_new_seed_and_beijing_day_change_permutation(self):
        players = [self.player(f'随机{i}', is_online=True) for i in range(12)]
        first = self.ids('random', shuffle_key='browse-A')
        self.assertEqual(first, self.ids('random', shuffle_key='browse-A', ordering='-created_at'))
        self.assertNotEqual(first, self.ids('random', shuffle_key='refresh-B'))
        with patch('django.utils.timezone.now', return_value=NOW + timedelta(days=1)):
            tomorrow = self.ids('random', shuffle_key='browse-A')
        self.assertNotEqual(first, tomorrow)
        self.assertCountEqual(tomorrow, [p.pk for p in players])
        self.assertEqual(self.ids('random'), self.ids('random', shuffle_key=''))

    def test_random_available_group_and_no_score_weighting(self):
        players = [self.player(f'随机可接{i}', is_online=True) for i in range(10)]
        resting = self.player('随机休息', is_online=False, bio='完整', presence_seen_at=NOW)
        forbidden = self.player('随机禁止接单', is_online=True, can_accept_orders=False)
        first = self.ids('random', shuffle_key='fixed')
        self.assertEqual(set(first[:10]), {p.pk for p in players})
        self.assertEqual(set(first[10:]), {resting.pk, forbidden.pk})
        target = players[0]
        for _ in range(20):
            self.order(target)
        Player.objects.filter(pk=target.pk).update(bio='高资料', audio_intro_url='https://test.invalid/v',
            presence_seen_at=NOW, total_orders=999999, total_rating=500, rating_count=100)
        self.assertEqual(first, self.ids('random', shuffle_key='fixed'))

    def test_invalid_modes_and_random_seed_format_return_400_without_queries(self):
        for mode in ('QUALITY', 'recommend', ' quality', 'random ', 'x'):
            with self.subTest(mode=mode), self.assertNumQueries(0):
                self.assertEqual(self.client.get('/api/player/list', {'discovery': mode}).status_code, 400)
        for seed in ('x' * 65, 'bad.seed', ' bad', 'a/b', 'a\nb', 'a\n', '中文', 'é', 'Ａ'):
            with self.subTest(seed=seed), self.assertNumQueries(0):
                self.assertEqual(self.client.get('/api/player/list',
                    {'discovery': 'random', 'shuffle_key': seed}).status_code, 400)

    def test_valid_seed_lengths_default_and_quality_seed_is_irrelevant(self):
        self.player('合法模式')
        for seed in ('A', 'a_Z-09', 'x' * 64, ''):
            self.assertEqual(len(self.rows('random', shuffle_key=seed)), 1)
        self.assertEqual(self.ids(), self.ids(shuffle_key='quality-does-not-use-seed' * 4))

    def test_all_candidates_and_existing_filters_are_preserved_in_both_modes(self):
        players = [self.player(f'全量搜索{i:02}', is_online=(i % 2 == 0)) for i in range(62)]
        winner = players[-1]
        self.order(winner)
        other_kind = PlayerType.objects.create(name='其他分类', priority=2)
        other = Player.objects.create(name='分类外', player_type=other_kind)
        for mode in ('quality', 'random'):
            with self.subTest(mode=mode):
                all_ids = self.ids(mode)
                self.assertEqual(len(all_ids), 63)
                self.assertEqual(len(set(all_ids)), 63)
                self.assertCountEqual(all_ids, [p.pk for p in players] + [other.pk])
                self.assertCountEqual(self.ids(mode, type_id=self.kind.pk), [p.pk for p in players])
                self.assertCountEqual(self.ids(mode, search='全量搜索', is_online='true'),
                                      [p.pk for p in players if p.is_online])
                self.assertEqual(self.ids(mode, search='全量搜索61'), [winner.pk])
                self.assertEqual(self.ids(mode, search='不存在'), [])

    def test_archived_hidden_unapproved_and_restricted_accounts_stay_hidden_readonly(self):
        visible = self.player('正常可见')
        self.player('隐藏资料', is_publicly_visible=False)
        archived = self.player('已移除资料')
        Player.objects.filter(pk=archived.pk).update(is_archived=True)
        for status in (Player.STATUS_PENDING, Player.STATUS_REJECTED, Player.STATUS_DISABLED):
            self.player(f'未审核{status}', status=status)
        expired = None
        for i, status, until in ((0, ClientProfile.ACCOUNT_STATUS_BANNED, None),
                (1, ClientProfile.ACCOUNT_STATUS_SUSPENDED, NOW + timedelta(seconds=1)),
                (2, ClientProfile.ACCOUNT_STATUS_SUSPENDED, None),
                (3, ClientProfile.ACCOUNT_STATUS_SUSPENDED, NOW)):
            user = User.objects.create_user(username=f'discovery-restriction-{i}')
            ClientProfile.objects.create(user=user, openid=f'synthetic-restriction-{i}', nickname=f'限制昵称{i}',
                account_status=status, account_suspended_until=until)
            player = self.player(f'限制账户{i}', user=user)
            if i == 3:
                expired = player
        for mode in ('quality', 'random'):
            with CaptureQueriesContext(connection) as queries:
                self.assertCountEqual(self.ids(mode), [visible.pk, expired.pk])
            self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertEqual(expired.user.client_profile.account_status, ClientProfile.ACCOUNT_STATUS_SUSPENDED)

    def test_public_dto_is_identical_to_legacy_and_does_not_leak_internal_metrics(self):
        user = User.objects.create_user(username='private-discovery-login')
        ClientProfile.objects.create(user=user, openid='private-discovery-openid', avatar_url='https://test.invalid/avatar')
        player = self.player('公开白名单', user=user, contact_wechat='private-wechat',
                             session_token='private-token', presence_seen_at=NOW)
        self.order(player, status=Order.STATUS_IN_PROGRESS)
        expected_keys = {'id', 'name', 'type_id', 'type_name', 'type_priority', 'price_extra',
            'designated_billing_type_id', 'designated_billing_type_name', 'designated_billing_type_priority',
            'avatar_url', 'bio', 'audio_intro_url', 'audio_intro_title', 'player_type',
            'designated_billing_type', 'avg_rating', 'rating_count', 'total_orders', 'is_online',
            'presence_online', 'can_be_designated', 'status', 'created_at'}
        legacy = self.client.get('/api/player/list').data
        for mode in ('quality', 'random'):
            rows = self.rows(mode)
            self.assertEqual(rows, legacy)
            self.assertEqual(set(rows[0]), expected_keys)
            self.assertEqual(rows[0]['status'], '接单中')
            self.assertNotIn('private-', str(rows))
        with override_settings(FEATURE_DESIGNATE_DISABLED=True):
            self.assertFalse(self.rows()[0]['can_be_designated'])

    def test_old_ordering_and_blank_discovery_remain_unchanged(self):
        a = self.player('旧调用甲', rating_count=2, total_rating=10, total_orders=1)
        b = self.player('旧调用乙', rating_count=1, total_rating=3, total_orders=9)
        Player.objects.filter(pk=a.pk).update(created_at=NOW - timedelta(days=1))
        for ordering, expected in (('avg_rating', [b.pk, a.pk]), ('-avg_rating', [a.pk, b.pk]),
                ('total_orders', [a.pk, b.pk]), ('-total_orders', [b.pk, a.pk]),
                ('created_at', [a.pk, b.pk]), ('-created_at', [b.pk, a.pk]),
                ('unknown', [a.pk, b.pk])):
            with self.subTest(ordering=ordering):
                old = self.client.get('/api/player/list', {'ordering': ordering})
                empty = self.client.get('/api/player/list', {'ordering': ordering,
                                            'discovery': '', 'shuffle_key': 'ignored.bad'})
                self.assertEqual(old.status_code, 200)
                self.assertEqual([row['id'] for row in old.data], expected)
                self.assertEqual(empty.data, old.data)

    def test_query_count_is_bounded_for_all_users_and_only_selects(self):
        for i in range(55):
            user = User.objects.create_user(username=f'discovery-batch-{i}')
            ClientProfile.objects.create(user=user, openid=f'synthetic-batch-{i}', nickname=f'批量昵称{i}', avatar_url='https://test.invalid/a')
            player = self.player(f'批量{i}', user=user, bio='资料')
            self.order(player)
        for mode, bound in (('quality', 2), ('random', 1)):
            with self.subTest(mode=mode), CaptureQueriesContext(connection) as queries:
                self.assertEqual(len(self.rows(mode)), 55)
            self.assertLessEqual(len(queries), bound)
            self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
            if mode == 'quality':
                aggregate = [q['sql'] for q in queries if 'COUNT(' in q['sql'].upper()]
                self.assertEqual(len(aggregate), 1)
                self.assertIn('DISTINCT', aggregate[0].upper())

    def test_unknown_legacy_grab_time_counts_history_but_not_recent_without_invention(self):
        from .discovery import completion_counts
        player = self.player('未知接受时间')
        order = self.order(player)
        # Current schema forbids NULL. A nullable TEMP historical-table fixture
        # exercises actual PG aggregation without changing models or migrations.
        with connection.cursor() as cursor:
            cursor.execute('CREATE TEMP TABLE order_players AS SELECT * FROM public.order_players WHERE FALSE')
            cursor.execute('INSERT INTO order_players SELECT * FROM public.order_players WHERE order_id = %s', [order.pk])
            cursor.execute('UPDATE order_players SET grab_time = NULL')
            cursor.execute('INSERT INTO order_players SELECT * FROM order_players')
        try:
            self.assertEqual(completion_counts([player.pk], NOW),
                             {player.pk: {'recent': 0, 'history': 1}})
        finally:
            with connection.cursor() as cursor:
                cursor.execute('DROP TABLE pg_temp.order_players')

    def test_ci_keeps_all_original_labels_and_adds_discovery(self):
        from hashlib import sha256
        from pathlib import Path
        import re
        root = Path(__file__).resolve().parents[2]
        candidate = (root / '.github/workflows/p0-backend-check.yml').read_text()
        # Frozen committed baseline; also works in a shallow GitHub checkout.
        original = (
            'apps.common.test_purchase_guard',
            'apps.common.test_content_security',
            'apps.common.test_media_security',
            'apps.chat',
            'apps.earnings',
            'apps.orders.test_admin_delete',
            'apps.orders.test_order_cancellation',
            'apps.orders.test_player_cancellations',
            'apps.orders.test_replacement_refunds',
            'apps.orders.test_matching_flow',
            'apps.orders.test_payment_deadlines',
            'apps.orders.test_room_entry_requeue',
            'apps.orders.test_p0_pricing',
            'apps.orders.test_designated_pricing',
            'apps.orders.test_designations',
            'apps.orders.test_escort_order_qualification',
            'apps.orders.test_cart_batch_orders',
            'apps.orders.test_service_duration',
            'apps.orders.test_shared_player_service_listings',
            'apps.orders.test_boss_timer_access',
            'apps.payments.test_payment_deadline_alignment',
            'apps.payments.test_virtualpay',
            'apps.payments.test_closed_virtual_payment',
            'apps.payments.test_coin_checkout_recovery',
            'apps.payments.test_unified_diamond_refunds',
            'apps.payments.test_admin_search',
            'apps.refunds.tests',
            'apps.players.tests',
            'apps.players.test_application_submission',
            'apps.players.test_escort_qualification',
            'apps.accounts.test_default_nickname',
            'apps.accounts.test_p2',
            'apps.accounts.test_account_restrictions',
            'apps.wallet.test_wallet_creation',
            'apps.wallet.test_diamonds',
            'apps.wallet.test_balance_payment',
            'apps.wallet.test_balance_refund_auto',
            'apps.wallet.test_coin_recharge',
            'apps.wallet.test_ios_recharge_credit',
            'apps.wallet.test_coin_checkout',
            'apps.wallet.test_coin_checkout_reservation',
            'apps.wallet.test_targeted_coin_checkout',
            'apps.wallet.test_coin_idempotency',
            'apps.wallet.test_coin_refund_sync',
            'apps.wallet.test_recharge_reconciliation',
            'apps.support',
            'apps.common.test_ci_workflow',
            'apps.patronage.tests.test_wall',
            'apps.patronage.tests.test_crowns',
            'apps.patronage.tests.test_records',
            'apps.patronage.tests.test_popular_announcements',
        )
        current = re.search(r'python manage.py test (.+?) --verbosity', candidate).group(1).split()
        self.assertEqual(current, [*original, 'apps.players.test_discovery'])
        unchanged = candidate.replace(' apps.players.test_discovery --verbosity', ' --verbosity')
        self.assertEqual(sha256(unchanged.encode()).hexdigest(),
                         '9764df3586c65bca38f3d7f63972feeca30ae7278944584c9aeba805b1392f01')
