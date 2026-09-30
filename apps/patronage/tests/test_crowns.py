from datetime import timedelta
from decimal import Decimal
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient
from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.patronage.models import PatronagePurchase


class CrownTests(TestCase):
    def test_multiple_bosses_bonus_and_expiry_are_readonly_display_rights(self):
        player = Player.objects.create(name='冠名陪玩', player_type=PlayerType.objects.create(name='冠名类型', priority=1))
        client = APIClient()
        response = client.get('/api/patronage/crowns/', {'player_id': player.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'results': []})
        Grant = apps.get_model('patronage', 'CrownGrant')
        now = timezone.now()
        expected = []
        for i, kind in enumerate(('active', 'bonus', 'expired', 'future', 'revoked', 'unknown', 'year')):
            boss = get_user_model().objects.create_user(username=f'private-username-{i}')
            ClientProfile.objects.create(user=boss, openid=f'private-openid-{i}', nickname=f'老板{i}', avatar_url=f'https://example.test/{i}.png')
            code = 'day_pass' if kind == 'bonus' else ('year' if kind == 'year' else 'week')
            purchase = PatronagePurchase.objects.create(boss=boss, player=player, player_name=player.name,
                package_code=code, package_name={'day_pass':'包天', 'year':'年冠', 'week':'周冠'}[code],
                amount_yuan=Decimal('520.00'), commission_rate=Decimal('0.25'),
                platform_amount_yuan=Decimal('130.00'), player_amount_yuan=Decimal('390.00'),
                price_version='c'*64, config_snapshot={'contract_version':'1.0'}, idempotency_key=f'crown-{i}',
                payment_status='unknown' if kind == 'unknown' else 'paid', paid_at=None if kind == 'unknown' else now,
                bonus_naming_days=7 if kind == 'bonus' else 0)
            grant = Grant.objects.create(purchase=purchase, source='day_pass_bonus' if kind == 'bonus' else 'purchase',
                package_name='周冠' if kind == 'bonus' else purchase.package_name,
                starts_at=now+timedelta(days=1) if kind == 'future' else now-timedelta(days=1),
                expires_at=now-timedelta(seconds=1) if kind == 'expired' else now+timedelta(days=730 if kind == 'year' else 7),
                revoked_at=now if kind == 'revoked' else None)
            if kind in ('active', 'bonus', 'year'):
                expected.append(grant.pk)
        with CaptureQueriesContext(connection) as queries:
            response = client.get('/api/patronage/crowns/', {'player_id': player.pk})
        results = response.json()['results']
        self.assertCountEqual([r['id'] for r in results], expected)
        self.assertIn('day_pass_bonus', [r['source'] for r in results])
        self.assertTrue(all(set(r) == {'id','boss_name','boss_avatar_url','package_name','starts_at','expires_at','source'} for r in results))
        self.assertNotIn('private-', str(response.json()))
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
