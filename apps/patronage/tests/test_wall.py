"""Public aggregate reads use isolated fixtures, never a payment or production API."""
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.patronage.models import CrownGrant, PatronagePurchase


URL = '/api/patronage/wall/'
PLAYER_FIELDS = {'id', 'name', 'avatar_url', 'type_name'}
CROWN_FIELDS = {'id', 'boss_name', 'boss_avatar_url', 'package_name', 'starts_at', 'expires_at', 'source'}


class PatronageWallTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.now = datetime(2026, 10, 10, 12, tzinfo=dt_timezone.utc)
        self.sequence = 0
        self.player_type = PlayerType.objects.create(name='公开技术陪', priority=1)

    def next_id(self):
        self.sequence += 1
        return self.sequence

    def user(self, *, profile=True, nickname=None, **values):
        sequence = self.next_id()
        user = get_user_model().objects.create_user(username=f'private-wall-login-{sequence}', **values)
        if profile:
            ClientProfile.objects.create(
                user=user, openid=f'private-wall-openid-{sequence}',
                nickname=nickname if nickname is not None else f'公开老板{sequence}',
                avatar_url=f'https://example.test/wall/{sequence}.png',
            )
        return user

    def player(self, **values):
        sequence = self.next_id()
        return Player.objects.create(name=f'公开陪玩{sequence}', player_type=self.player_type, **values)

    def grant(self, player, *, boss=None, source='purchase', status='paid', starts=None, expires=None, revoked=None, package='周冠'):
        sequence = self.next_id()
        boss = boss or self.user()
        purchase = PatronagePurchase.objects.create(
            boss=boss, player=player, player_name=player.name,
            package_code='day_pass' if source == 'day_pass_bonus' else 'week',
            package_name='包天' if source == 'day_pass_bonus' else package,
            amount_yuan=Decimal('520.00'), commission_rate=Decimal('0.25'),
            platform_amount_yuan=Decimal('130.00'), player_amount_yuan=Decimal('390.00'),
            price_version='c' * 64, config_snapshot={'offline': True},
            idempotency_key=f'private-wall-key-{sequence}', payment_status=status,
            paid_at=self.now if status == 'paid' else None,
            bonus_naming_days=7 if source == 'day_pass_bonus' else 0,
        )
        return CrownGrant.objects.create(
            purchase=purchase, source=source, package_name=package,
            starts_at=starts if starts is not None else self.now - timedelta(days=1),
            expires_at=expires if expires is not None else self.now + timedelta(days=7),
            revoked_at=revoked,
        )

    def read(self, data=None, **headers):
        with patch('apps.patronage.wall.timezone.now', return_value=self.now):
            return self.client.get(URL, data or {}, **headers)

    def assert_contract(self, response):
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {'contract_version', 'count', 'results'})
        self.assertEqual(body['contract_version'], '1.0')
        self.assertEqual(body['count'], len(body['results']))
        for entry in body['results']:
            self.assertEqual(set(entry), {'player', 'crowns'})
            self.assertEqual(set(entry['player']), PLAYER_FIELDS)
            self.assertGreater(entry['player']['id'], 0)
            self.assertIsInstance(entry['player']['avatar_url'], str)
            self.assertTrue(entry['crowns'])
            for crown in entry['crowns']:
                self.assertEqual(set(crown), CROWN_FIELDS)
                self.assertIsInstance(crown['boss_name'], str)
                self.assertIsInstance(crown['boss_avatar_url'], str)
                self.assertIsNotNone(datetime.fromisoformat(crown['starts_at']).tzinfo)
                self.assertIsNotNone(datetime.fromisoformat(crown['expires_at']).tzinfo)
        return body

    def test_empty_wall_is_a_complete_contract_with_no_pagination(self):
        self.player()
        response = self.read()
        self.assertEqual(response.json(), {'contract_version': '1.0', 'count': 0, 'results': []})
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_only_paid_active_unrevoked_grants_are_returned_at_one_snapshot_time(self):
        player = self.player()
        active = self.grant(player)
        bonus = self.grant(player, source='day_pass_bonus')
        at_start = self.grant(player, starts=self.now)
        self.grant(player, expires=self.now)
        self.grant(player, expires=self.now - timedelta(seconds=1))
        self.grant(player, starts=self.now + timedelta(microseconds=1))
        self.grant(player, revoked=self.now)
        for status in ('created', 'processing', 'unknown', 'failed'):
            self.grant(player, status=status)
        with patch('apps.patronage.wall.timezone.now', return_value=self.now) as clock:
            body = self.assert_contract(self.client.get(URL))
        self.assertEqual(clock.call_count, 1)
        self.assertEqual(body['count'], 1)
        self.assertCountEqual([c['id'] for c in body['results'][0]['crowns']], [active.pk, bonus.pk, at_start.pk])
        self.assertIn('day_pass_bonus', [c['source'] for c in body['results'][0]['crowns']])

    def test_longest_right_is_selected_per_player_and_boss_with_a_stable_id_tie_break(self):
        first, second = self.player(), self.player()
        boss, other_boss = self.user(), self.user()
        self.grant(first, boss=boss, expires=self.now + timedelta(days=7))
        self.grant(first, boss=boss, expires=self.now + timedelta(days=30), package='月冠')
        longest = self.grant(first, boss=boss, expires=self.now + timedelta(days=30), package='季冠')
        other = self.grant(first, boss=other_boss, source='day_pass_bonus', expires=self.now + timedelta(days=10))
        same_boss_other_player = self.grant(second, boss=boss, expires=self.now + timedelta(days=20))
        body = self.assert_contract(self.read())
        by_player = {entry['player']['id']: entry['crowns'] for entry in body['results']}
        self.assertEqual([c['id'] for c in by_player[first.pk]], [longest.pk, other.pk])
        self.assertEqual([c['package_name'] for c in by_player[first.pk]], ['季冠', '周冠'])
        self.assertEqual([c['id'] for c in by_player[second.pk]], [same_boss_other_player.pk])

    def test_visibility_follows_player_availability_without_online_or_order_acceptance_filters(self):
        resting = self.player(is_online=False, can_accept_orders=False, can_be_designated=False)
        self.grant(resting)
        active_user = self.user()
        active_player = self.player(user=active_user)
        self.grant(active_player)
        no_profile_player = self.player(user=self.user(profile=False))
        self.grant(no_profile_player)
        hidden = self.player(is_publicly_visible=False)
        archived = self.player()
        # Fixture-only bypass: model.save intentionally prevents unaudited archive writes.
        Player.objects.filter(pk=archived.pk).update(is_archived=True)
        unavailable = [hidden, archived]
        unavailable.extend(self.player(status=status) for status in ('pending', 'rejected', 'disabled'))
        unavailable.append(self.player(user=self.user(is_active=False)))
        for status in ('suspended', 'banned'):
            user = self.user()
            ClientProfile.objects.filter(user=user).update(
                account_status=status, account_suspended_until=self.now - timedelta(days=1),
            )
            unavailable.append(self.player(user=user))
        for player in unavailable:
            self.grant(player)
        body = self.assert_contract(self.read())
        self.assertCountEqual([entry['player']['id'] for entry in body['results']], [resting.pk, active_player.pk, no_profile_player.pk])
        self.assertEqual(ClientProfile.objects.filter(account_status='suspended').count(), 1)

    def test_order_uses_unrounded_mean_descending_then_player_id_ascending(self):
        zero = self.player(total_rating=100, rating_count=0)
        low = self.player(total_rating=9.82, rating_count=2)
        equal_first = self.player(total_rating=10, rating_count=2)
        equal_later = self.player(total_rating=200, rating_count=40, total_orders=500)
        high_rounded_same_as_low = self.player(total_rating=494, rating_count=100)
        for player in (zero, low, equal_first, equal_later, high_rounded_same_as_low):
            self.grant(player)
        body = self.assert_contract(self.read())
        self.assertEqual([entry['player']['id'] for entry in body['results']], [equal_first.pk, equal_later.pk, high_rounded_same_as_low.pk, low.pk, zero.pk])

    def test_anonymous_invalid_and_legacy_authorization_headers_never_run_authenticators(self):
        from apps.accounts.authentication import LegacyPlayerTokenAuthentication, LenientJWTAuthentication
        player = self.player()
        self.grant(player)
        Player.objects.filter(pk=player.pk).update(session_token='private-wall-token')
        for header in ('', 'Bearer malformed.not.jwt', 'Bearer private-wall-token'):
            with patch.object(LegacyPlayerTokenAuthentication, 'authenticate', side_effect=AssertionError('must not authenticate')), \
                 patch.object(LenientJWTAuthentication, 'authenticate', side_effect=AssertionError('must not authenticate')):
                body = self.assert_contract(self.read(HTTP_AUTHORIZATION=header))
            self.assertEqual(body['count'], 1)

    def test_non_get_methods_return_405_without_reading_or_changing_data(self):
        for method in ('post', 'put', 'patch', 'delete'):
            with CaptureQueriesContext(connection) as queries:
                response = getattr(self.client, method)(URL, {'player_id': 1}, format='json')
            self.assertEqual(response.status_code, 405)
            self.assertEqual(len(queries), 0)

    def test_public_fields_and_missing_profile_fallbacks_never_disclose_private_identity_or_purchase_data(self):
        receiver = self.user()
        player = self.player(user=receiver, contact_wechat='private-wall-contact', session_token='private-wall-player-token')
        blank_nickname_boss = self.user(nickname='')
        no_profile_boss = self.user(profile=False)
        self.grant(player, boss=blank_nickname_boss)
        self.grant(player, boss=no_profile_boss)
        response = self.read()
        body = self.assert_contract(response)
        entry = body['results'][0]
        self.assertEqual(entry['player']['name'], player.name)
        self.assertEqual(entry['player']['avatar_url'], receiver.client_profile.avatar_url)
        self.assertEqual(entry['player']['type_name'], self.player_type.name)
        self.assertEqual([c['boss_name'] for c in entry['crowns']], ['老板', '老板'])
        self.assertIn('', [c['boss_avatar_url'] for c in entry['crowns']])
        text = response.content.decode()
        for forbidden in ('private-wall-', 'openid', 'contact_wechat', 'username', 'phone', 'balance', 'purchase_no', 'idempotency', 'amount_yuan', 'commission', 'session_token'):
            self.assertNotIn(forbidden, text)

    def test_complete_wall_keeps_one_select_query_without_writes_as_the_number_of_players_and_grants_grows(self):
        for count in (1, 20):
            for _ in range(count):
                player = self.player(user=self.user())
                self.grant(player, boss=self.user())
                self.grant(player, boss=self.user())
            with CaptureQueriesContext(connection) as queries:
                response = self.read(data={'page': 2, 'page_size': 1})
            body = self.assert_contract(response)
            self.assertEqual(body['count'], 1 if count == 1 else 21)
            self.assertEqual(len(queries), 1, [q['sql'] for q in queries])
            self.assertTrue(all(q['sql'].lstrip(' (\n\t').upper().startswith('SELECT') for q in queries), [q['sql'] for q in queries])
            self.assertEqual(sum(len(entry['crowns']) for entry in body['results']), body['count'] * 2)
