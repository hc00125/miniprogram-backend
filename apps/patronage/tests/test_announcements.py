"""Public announcements: isolated real PG records, never real payments."""
from datetime import datetime, timedelta, timezone as dt_timezone
from django.test import TestCase, TransactionTestCase, override_settings
from apps.gifts.tests import test_immediate_income as gift_fixtures
from apps.gifts.models import Gift, GiftPurchase, GiftTransfer, GiftInventoryLot, GiftTransferAllocation, GiftEarning
from apps.gifts.services import transfers
from apps.patronage.tests import test_income as income_fixtures
from apps.patronage.models import CrownGrant, PatronagePurchase
from apps.patronage.fulfillment import fulfill
from apps.wallet import spend_service
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from unittest.mock import patch
from apps.patronage.pricing import PACKAGE_CHOICES


PUBLIC_FIELDS = {'id', 'kind', 'boss_name', 'player_id', 'player_name', 'item_name', 'quantity', 'occurred_at', 'text',
                 'boss_avatar_url', 'player_avatar_url', 'item_image_url'}


def assert_readonly_public(test, kind, count, query_limit):
    # Even with a valid legacy token, the public view must never call a repair /
    # presence-writing authenticator or read request.user.
    from apps.accounts.authentication import LegacyPlayerTokenAuthentication, LenientJWTAuthentication
    test.player.session_token = 'offline-announcement-token'
    test.player.token_expires_at = timezone.now() + timedelta(days=1)
    test.player.save(update_fields=['session_token', 'token_expires_at'])
    with patch.object(LegacyPlayerTokenAuthentication, 'authenticate', side_effect=AssertionError('auth must not run')), \
         patch.object(LenientJWTAuthentication, 'authenticate', side_effect=AssertionError('auth must not run')), \
         CaptureQueriesContext(connection) as captured:
        response = test.client.get('/api/announcements/', {'kind': kind, 'page_size': 50},
                                   HTTP_AUTHORIZATION='Bearer offline-announcement-token')
    test.assertEqual(response.status_code, 200)
    test.assertEqual(response.json()['count'], count)
    test.assertLessEqual(len(captured), query_limit, [q['sql'] for q in captured])
    test.assertTrue(all(q['sql'].lstrip(' (\n\t').upper().startswith('SELECT') for q in captured), [q['sql'] for q in captured])
    for row in response.json()['results']:
        test.assertEqual(set(row), PUBLIC_FIELDS)
        for field in ('boss_avatar_url', 'player_avatar_url', 'item_image_url'):
            test.assertIsInstance(row[field], str)
        test.assertGreater(row['player_id'], 0)
        test.assertGreater(row['quantity'], 0)
        test.assertIsNotNone(datetime.fromisoformat(row['occurred_at']).tzinfo)
    body = response.content.decode()
    for forbidden in ('openid', 'purchase_no', 'transfer_no', 'idempotency', 'commission', 'balance',
                      'offline-announcement-token', 'patronage-income-boss', 'gift-income-buyer',
                      'phone', 'real_name', 'identity', 'session_key', 'private-player-login'):
        test.assertNotIn(forbidden, body)


class PatronageAnnouncementTests(TestCase):
    setUp = income_fixtures.PatronageIncomeTests.setUp
    prepare = income_fixtures.PatronageIncomeTests.prepare

    def purchase(self, key='announcement', package='week', complete=True, when=None):
        # Call helpers only: never inherit another discovered test class.
        purchase = PatronagePurchase.objects.create(
            boss=self.boss, player=self.player, player_name=self.player.name,
            package_code=package, package_name=dict(PACKAGE_CHOICES)[package],
            amount_yuan='188.00', commission_rate='0.25', platform_amount_yuan='47.00',
            player_amount_yuan='141.00', price_version='b'*64, config_snapshot={'offline': True},
            idempotency_key=key, bonus_naming_days=7 if package == 'day_pass' else 0)
        purchase.refresh_from_db()
        if complete:
            attempt = self.prepare(purchase)
            spend_service.finalize(attempt.pk, apply=lambda a: fulfill(a, confirmed_at=when or datetime(2026, 1, 1, tzinfo=dt_timezone.utc)))
            purchase.refresh_from_db()
        return purchase

    def feed(self, **query):
        response = self.client.get('/api/announcements/', {'kind': 'patronage', **query})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_patronage_avatars_use_public_profiles_and_never_invent_item_image(self):
        from django.contrib.auth import get_user_model
        from apps.accounts.models import ClientProfile
        receiver = get_user_model().objects.create_user('private-player-login')
        ClientProfile.objects.create(user=receiver, openid='private-player-openid', nickname='public-player',
            avatar_url='https://example.test/patronage-player.png')
        self.player.user = receiver
        self.player.save(update_fields=['user'])
        self.profile.avatar_url = 'https://example.test/patronage-boss.png'
        self.profile.save(update_fields=['avatar_url'])
        self.purchase()
        with CaptureQueriesContext(connection) as captured:
            row = self.feed()['results'][0]
        self.assertEqual(row.get('boss_avatar_url'), self.profile.avatar_url)
        self.assertEqual(row.get('player_avatar_url'), receiver.client_profile.avatar_url)
        self.assertEqual(row.get('item_image_url'), '')
        self.assertLessEqual(len(captured), 2)
        self.assertTrue(all(q['sql'].lstrip(' (\n\t').upper().startswith('SELECT') for q in captured))

    def test_only_completed_paid_valid_granted_earning_records_are_public(self):
        valid = self.purchase(key='valid')
        for state in ('created', 'processing', 'unknown', 'failed'):
            row = self.purchase(key=state)
            PatronagePurchase.objects.filter(pk=row.pk).update(payment_status=state)
        row = self.purchase(key='revoked')
        CrownGrant.objects.filter(purchase=row).update(revoked_at=row.paid_at)
        row = self.purchase(key='no-dates')
        PatronagePurchase.objects.filter(pk=row.pk).update(starts_at=None, expires_at=None)
        # Genuine completed debit but incomplete downstream fulfillment.
        for key, grant in (('no-crown', False), ('no-earning', True)):
            row = self.purchase(key=key, complete=False)
            attempt = self.prepare(row)
            spend_service.finalize(attempt.pk, apply=lambda a: None)
            when = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
            PatronagePurchase.objects.filter(pk=row.pk).update(payment_status='paid', paid_at=when,
                starts_at=when, expires_at=when + timedelta(days=7))
            if not grant:
                from apps.patronage.income import credit_purchase
                credit_purchase(row)
            if grant:
                CrownGrant.objects.create(purchase=row, source='purchase', package_name='周冠',
                    starts_at=when, expires_at=when + timedelta(days=7))
        row = self.purchase(key='pending-attempt', complete=False)
        self.prepare(row, status='unknown')
        PatronagePurchase.objects.filter(pk=row.pk).update(payment_status='paid', paid_at=valid.paid_at,
            starts_at=valid.starts_at, expires_at=valid.expires_at)
        data = self.feed()
        self.assertEqual(data['count'], 1)
        self.assertEqual([r['id'] for r in data['results']], [f'patronage:{valid.pk}'])

    def test_bonus_single_row_and_reliable_renewal_wording(self):
        first = self.purchase(key='first')
        renewed = self.purchase(key='renewed')
        bonus = self.purchase(key='bonus', package='day_pass')
        CrownGrant.objects.create(purchase=bonus, source='purchase', package_name='extra',
            starts_at=bonus.starts_at, expires_at=bonus.expires_at)
        rows = {r['id']: r for r in self.feed()['results']}
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[f'patronage:{bonus.pk}']['text'],
                         f'老板「老板」包天「{bonus.player_name}」，赠送7天冠名')
        self.assertIn('续购「周冠」', rows[f'patronage:{renewed.pk}']['text'])
        self.assertIn('开通「周冠」', rows[f'patronage:{first.pk}']['text'])

    def test_future_and_inconsistent_paid_dates_are_not_completion_events(self):
        valid = self.purchase(key='past')
        self.purchase(key='future', when=timezone.now() + timedelta(days=2))
        wrong = self.purchase(key='wrong-time')
        PatronagePurchase.objects.filter(pk=wrong.pk).update(paid_at=wrong.starts_at + timedelta(days=1))
        self.assertEqual([r['id'] for r in self.feed()['results']], [f'patronage:{valid.pk}'])

    def test_patronage_pagination_is_ordered_unique_and_readonly(self):
        purchases = [self.purchase(key=f'page-{i}') for i in range(5)]
        pages = [self.feed(page=page, page_size=2) for page in (1, 2, 3, 4)]
        self.assertTrue(all(p['count'] == 5 for p in pages))
        self.assertEqual([r['id'] for p in pages for r in p['results']],
                         [f'patronage:{p.pk}' for p in reversed(purchases)])
        self.assertEqual(self.feed(page=10**30)['results'], [])
        assert_readonly_public(self, 'patronage', 5, 2)

    def test_legacy_grant_without_extension_metadata_does_not_claim_renewal(self):
        row = self.purchase(complete=False)
        attempt = self.prepare(row)
        spend_service.finalize(attempt.pk, apply=lambda a: None)
        when = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        PatronagePurchase.objects.filter(pk=row.pk).update(payment_status='paid', paid_at=when,
            starts_at=when, expires_at=when + timedelta(days=7))
        CrownGrant.objects.create(purchase=row, source='purchase', package_name='周冠',
            starts_at=when, expires_at=when + timedelta(days=7))
        from apps.patronage.income import credit_purchase
        credit_purchase(row)
        self.assertIn('开通「周冠」', self.feed()['results'][0]['text'])

    def test_blank_or_missing_boss_profile_never_exposes_login_name(self):
        self.purchase()
        self.profile.nickname = '   '
        self.profile.save(update_fields=['nickname'])
        self.assertEqual(self.feed()['results'][0]['boss_name'], '老板')
        # Missing profile is possible on legacy accounts; use an unprofiled boss
        # with a historical completed record rather than deleting wallet evidence.
        from django.contrib.auth import get_user_model
        boss = get_user_model().objects.create_user('private-unprofiled-login')
        from apps.patronage.announcements import patronage_data
        record = self.purchase(key='missing-profile')
        record.boss = boss
        record.is_renewal = False
        self.assertEqual(patronage_data(record)['boss_name'], '老板')
        self.assertEqual(patronage_data(record)['boss_avatar_url'], '')

    def test_completed_purchase_is_one_historical_public_record(self):
        purchase = self.purchase()
        self.profile.nickname = '昵称老板'
        self.profile.save(update_fields=['nickname'])
        self.player.name = '当前新陪玩名'
        self.player.save(update_fields=['name'])
        data = self.feed()
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['results'], [{
            'id': f'patronage:{purchase.pk}', 'kind': 'patronage', 'boss_name': '昵称老板',
            'boss_avatar_url': '', 'player_avatar_url': '', 'item_image_url': '',
            'player_id': self.player.pk, 'player_name': purchase.player_name,
            'item_name': purchase.package_name, 'quantity': 1, 'occurred_at': purchase.paid_at.isoformat(),
            'text': f'老板「昵称老板」为「{purchase.player_name}」开通「{purchase.package_name}」',
        }])


@override_settings(GIFT_PURCHASE_ENABLED=True, GIFT_INVENTORY_SEND_ENABLED=True,
                   WECHAT_VIRTUALPAY_ENV=1, FISH_CRACKER_EXCHANGE_RATE=10)
class GiftAnnouncementTests(TransactionTestCase):
    buy = gift_fixtures.ImmediateGiftIncomeTests.buy

    def setUp(self):
        gift_fixtures.ImmediateGiftIncomeTests.setUp(self)
        self.client.force_authenticate(None)

    def feed(self, **query):
        response = self.client.get('/api/announcements/', {'kind': 'gift', **query})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_gift_avatars_and_image_use_real_public_sources(self):
        self.profile.avatar_url = 'https://example.test/boss.png'
        self.profile.save(update_fields=['avatar_url'])
        player_profile = self.player.user.client_profile
        player_profile.avatar_url = 'https://example.test/player.png'
        player_profile.save(update_fields=['avatar_url'])
        Gift.objects.filter(pk=self.gift.pk).update(image='gifts/offline-real.png')
        self.buy()
        with CaptureQueriesContext(connection) as captured:
            row = self.feed()['results'][0]
        self.assertEqual(row.get('boss_avatar_url'), self.profile.avatar_url)
        self.assertEqual(row.get('player_avatar_url'), player_profile.avatar_url)
        self.assertEqual(row.get('item_image_url'), 'http://testserver/media/gifts/offline-real.png')
        self.assertLessEqual(len(captured), 3)
        self.assertTrue(all(q['sql'].lstrip(' (\n\t').upper().startswith('SELECT') for q in captured))

    def test_incomplete_reversed_internal_and_inventory_purchase_are_not_announced(self):
        valid = self.buy(key='valid')
        for status in ('created', 'failed', 'reversed'):
            purchase = self.buy(key=status)
            GiftTransfer.objects.filter(purchase=purchase).update(status=status)
        for status in ('created', 'processing', 'unknown', 'failed', 'refunded', 'partially_refunded'):
            purchase = self.buy(key='purchase-'+status)
            GiftPurchase.objects.filter(pk=purchase.pk).update(status=status)
        purchase = self.buy(key='no-attempt')
        GiftPurchase.objects.filter(pk=purchase.pk).update(attempt=None)
        purchase = self.buy(key='internal')
        internal = Gift.objects.create(kind='internal_surcharge', code='order_surcharge', name='内部加价', price_diamonds=None)
        GiftTransfer.objects.filter(purchase=purchase).update(gift=internal)
        self.buy(mode='inventory', key='only-in-backpack')
        data = self.feed()
        self.assertEqual(data['count'], 1)
        self.assertEqual([r['id'] for r in data['results']], [f'gift:{GiftTransfer.objects.get(purchase=valid).pk}'])

    def test_missing_public_profiles_and_image_are_empty_strings_without_repairs(self):
        from django.contrib.auth import get_user_model
        self.buy()
        # Both absent user and absent reverse profile are valid historical shapes.
        unprofiled = get_user_model().objects.create_user('private-unprofiled-recipient')
        for user in (None, unprofiled):
            with self.subTest(user=user):
                self.player.user = user
                self.player.save(update_fields=['user'])
                assert_readonly_public(self, 'gift', 1, 3)
                row = self.feed()['results'][0]
                for field in ('boss_avatar_url', 'player_avatar_url', 'item_image_url'):
                    self.assertEqual(row[field], '')

    def test_inventory_split_lots_and_free_grants_are_one_transfer_each(self):
        self.buy(mode='inventory', key='lot-one', quantity=1)
        self.buy(mode='inventory', key='lot-two', quantity=2)
        Gift.objects.filter(pk=self.gift.pk).update(name='已改名礼物')
        sent = transfers.transfer(self.buyer, gift_code=self.gift.code, quantity=3,
            recipient_id=self.player.pk, commission_version=1, idempotency_key='split-send')
        self.assertEqual(sent.allocations.count(), 2)
        self.assertEqual(GiftEarning.objects.filter(transfer=sent).count(), 2)
        GiftInventoryLot.objects.create(owner=self.buyer, gift=self.gift, source='admin_free',
            source_reference='offline-free', quantity=1, remaining=1, snapshot={'name': '免费历史礼物'})
        free = transfers.transfer(self.buyer, gift_code=self.gift.code, quantity=1,
            recipient_id=self.player.pk, commission_version=1, idempotency_key='free-send')
        data = self.feed()
        self.assertEqual(data['count'], 2)
        by_id = {r['id']: r for r in data['results']}
        self.assertEqual(by_id[f'gift:{sent.pk}']['quantity'], 3)
        self.assertEqual(by_id[f'gift:{sent.pk}']['item_name'], self.gift.name)
        self.assertEqual(by_id[f'gift:{free.pk}']['item_name'], '免费历史礼物')

    def test_future_or_incomplete_inventory_deliveries_are_not_public(self):
        purchase = self.buy(key='future')
        GiftTransfer.objects.filter(purchase=purchase).update(created_at=timezone.now() + timedelta(days=1))
        self.buy(mode='inventory', key='stock', quantity=3)
        sent = transfers.transfer(self.buyer, gift_code=self.gift.code, quantity=3,
            recipient_id=self.player.pk, commission_version=1, idempotency_key='stock-send')
        GiftTransferAllocation.objects.filter(transfer=sent).update(quantity=1)
        GiftTransfer.objects.create(transfer_no='offline-orphan', sender=self.buyer, recipient=self.player,
            gift=self.gift, quantity=1, idempotency_key='orphan', request_digest='a'*64,
            commission_snapshot={'offline': True}, status='delivered')
        self.assertEqual(self.feed()['count'], 0)

    def test_gift_pagination_order_privacy_query_budget_and_readonly(self):
        purchases = [self.buy(key=f'page-{i}') for i in range(5)]
        rows = list(GiftTransfer.objects.order_by('pk'))
        # Equal success time exercises deterministic ID tie breaking.
        GiftTransfer.objects.update(created_at=rows[0].created_at)
        pages = [self.feed(page=page, page_size=2) for page in (1, 2, 3, 4)]
        self.assertTrue(all(p['count'] == 5 for p in pages))
        self.assertEqual([r['id'] for p in pages for r in p['results']],
                         [f'gift:{r.pk}' for r in reversed(rows)])
        self.assertEqual(self.feed(page=10**30)['results'], [])
        assert_readonly_public(self, 'gift', len(purchases), 3)

    def test_missing_or_malformed_snapshot_name_has_catalog_fallback(self):
        purchase = self.buy()
        for snapshot in ({}, {'name': ' '}, {'name': 42}, [], 'legacy'):
            with self.subTest(snapshot=snapshot):
                GiftPurchase.objects.filter(pk=purchase.pk).update(snapshot=snapshot)
                self.assertEqual(self.feed()['results'][0]['item_name'], self.gift.name)
        self.profile.nickname = '   '
        self.profile.save(update_fields=['nickname'])
        self.assertEqual(self.feed()['results'][0]['boss_name'], '老板')

    def test_inventory_uses_first_nonblank_allocation_name(self):
        self.buy(mode='inventory', key='first', quantity=1)
        self.buy(mode='inventory', key='second', quantity=1)
        sent = transfers.transfer(self.buyer, gift_code=self.gift.code, quantity=2,
            recipient_id=self.player.pk, commission_version=1, idempotency_key='send')
        first, second = sent.allocations.order_by('pk')
        GiftTransferAllocation.objects.filter(pk=first.pk).update(snapshot={'name': ' '})
        GiftTransferAllocation.objects.filter(pk=second.pk).update(snapshot={'name': '第二批历史名'})
        self.assertEqual(self.feed()['results'][0]['item_name'], '第二批历史名')
        assert_readonly_public(self, 'gift', 1, 3)

    def test_delivered_direct_with_unknown_attempt_is_not_public(self):
        from apps.wallet.models import WalletSpendAttempt
        paid = self.buy()
        attempt = WalletSpendAttempt.objects.create(wallet=self.wallet, kind='gift',
            business_no='offline-unknown', idempotency_key='offline-unknown', request_digest='a'*64,
            amount='2.00', reserved_amount='2.00', source_snapshot={'offline': True}, status='unknown')
        purchase = GiftPurchase.objects.create(purchase_no='offline-unknown', attempt=attempt,
            buyer=self.buyer, gift=self.gift, mode='direct', recipient=self.player, quantity=2,
            snapshot=paid.snapshot, idempotency_key='offline-unknown', request_digest='a'*64, status='paid')
        GiftTransfer.objects.create(transfer_no='offline-unknown', sender=self.buyer, recipient=self.player,
            gift=self.gift, quantity=2, source='direct', purchase=purchase,
            idempotency_key='offline-unknown', request_digest='a'*64,
            commission_snapshot={'offline': True}, status='delivered')
        self.assertEqual(self.feed()['count'], 1)
        self.assertEqual(self.feed()['results'][0]['id'], f'gift:{GiftTransfer.objects.get(purchase=paid).pk}')

    def test_transfer_with_mismatched_paid_purchase_is_not_public(self):
        purchase = self.buy()
        from apps.players.models import Player
        alternate = Player.objects.create(name='alternate recipient', player_type=self.player.player_type)
        other_gift = Gift.objects.create(name='alternate gift', price_diamonds=10)
        transfer = GiftTransfer.objects.get(purchase=purchase)
        original = {'quantity': transfer.quantity, 'sender_id': transfer.sender_id,
                    'recipient_id': transfer.recipient_id, 'gift_id': transfer.gift_id}
        for changed in ({'quantity': 1}, {'sender_id': self.player.user_id},
                        {'recipient_id': alternate.pk}, {'gift_id': other_gift.pk}):
            with self.subTest(changed=changed):
                GiftTransfer.objects.filter(pk=transfer.pk).update(**{**original, **changed})
                self.assertEqual(self.feed()['count'], 0)

    def test_real_direct_delivery_is_one_public_historical_gift(self):
        purchase = self.buy()
        transfer = GiftTransfer.objects.get(purchase=purchase)
        self.assertEqual(purchase.status, 'paid')
        self.assertEqual(purchase.attempt.status, 'completed')
        Gift.objects.filter(pk=self.gift.pk).update(name='目录新名', is_active=False)
        data = self.feed()
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['results'], [{
            'id': f'gift:{transfer.pk}', 'kind': 'gift', 'boss_name': '老板',
            'boss_avatar_url': '', 'player_avatar_url': '', 'item_image_url': '',
            'player_id': self.player.pk, 'player_name': self.player.name,
            'item_name': self.gift.name, 'quantity': 2, 'occurred_at': transfer.created_at.isoformat(),
            'text': f'老板「老板」赠送「{self.player.name}」2个「{self.gift.name}」',
        }])


@override_settings(GIFT_PURCHASE_ENABLED=True, GIFT_INVENTORY_SEND_ENABLED=True,
                   WECHAT_VIRTUALPAY_ENV=1, FISH_CRACKER_EXCHANGE_RATE=10)
class AllAnnouncementTests(TransactionTestCase):
    buy = gift_fixtures.ImmediateGiftIncomeTests.buy
    prepare = income_fixtures.PatronageIncomeTests.prepare
    purchase = PatronageAnnouncementTests.purchase

    def setUp(self):
        gift_fixtures.ImmediateGiftIncomeTests.setUp(self)
        self.client.force_authenticate(None)
        self.boss = self.buyer
        self.buyer_wallet = self.wallet
        self.wallet.balance = '5000.00'
        self.wallet.save(update_fields=['balance'])

    def test_all_merges_real_timestamps_with_stable_ties_and_exact_pagination(self):
        when = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        expected = []
        for index, offset in enumerate((0, 2, 2, 4)):
            event_at = when + timedelta(hours=offset)
            patronage = self.purchase(key=f'patronage-{index}', when=event_at)
            expected.append((event_at, 'patronage', patronage.pk))
            purchase = self.buy(key=f'gift-{index}')
            gift = GiftTransfer.objects.get(purchase=purchase)
            # Deliberately inverse PK/time relationship; sort by event, not ID.
            gift_at = when + timedelta(hours=4-offset)
            GiftTransfer.objects.filter(pk=gift.pk).update(created_at=gift_at)
            expected.append((gift_at, 'gift', gift.pk))
        expected_ids = [f'{kind}:{pk}' for _, kind, pk in sorted(expected, reverse=True)]
        seen = []
        for page in range(1, 6):
            with CaptureQueriesContext(connection) as captured:
                response = self.client.get('/api/announcements/', {'kind': 'all', 'page': page, 'page_size': 2})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data['count'], 8)
            self.assertEqual((data['page'], data['page_size']), (page, 2))
            self.assertLessEqual(len(captured), 5)
            self.assertTrue(all(q['sql'].lstrip(' (\n\t').upper().startswith('SELECT') for q in captured))
            seen.extend(row['id'] for row in data['results'])
        self.assertEqual(seen, expected_ids)
        self.assertEqual(len(set(seen)), 8)
        self.assertEqual(self.client.get('/api/announcements/', {'kind': 'all', 'page': 10**30}).json()['results'], [])
        default = self.client.get('/api/announcements/').json()
        self.assertEqual(default['count'], 4)
        self.assertTrue(all(row['kind'] == 'patronage' for row in default['results']))
        assert_readonly_public(self, 'all', 8, 5)

    def test_all_preserves_both_projection_eligibility_and_serialized_fields(self):
        self.purchase(key='valid-crown')
        revoked = self.purchase(key='revoked-crown')
        CrownGrant.objects.filter(purchase=revoked).update(revoked_at=revoked.paid_at)
        self.purchase(key='unpaid-crown', complete=False)
        self.buy(key='valid-gift')
        reversed_purchase = self.buy(key='reversed-gift')
        GiftTransfer.objects.filter(purchase=reversed_purchase).update(status='reversed')
        self.buy(mode='inventory', key='not-yet-sent')
        single_rows = []
        for kind in ('gift', 'patronage'):
            data = self.client.get('/api/announcements/', {'kind': kind}).json()
            self.assertEqual(data['count'], 1)
            single_rows.extend(data['results'])
        combined = self.client.get('/api/announcements/', {'kind': 'all'}).json()
        self.assertEqual(combined['count'], 2)
        self.assertEqual({r['id']: r for r in combined['results']}, {r['id']: r for r in single_rows})
        assert_readonly_public(self, 'all', 2, 5)

    def test_all_handles_empty_and_single_source_sets(self):
        assert_readonly_public(self, 'all', 0, 1)
        crown = self.purchase()
        response = self.client.get('/api/announcements/', {'kind': 'all'}).json()
        self.assertEqual([r['id'] for r in response['results']], [f'patronage:{crown.pk}'])
        assert_readonly_public(self, 'all', 1, 3)
        CrownGrant.objects.filter(purchase=crown).update(revoked_at=crown.paid_at)
        purchase = self.buy()
        transfer = GiftTransfer.objects.get(purchase=purchase)
        response = self.client.get('/api/announcements/', {'kind': 'all'}).json()
        self.assertEqual([r['id'] for r in response['results']], [f'gift:{transfer.pk}'])
        assert_readonly_public(self, 'all', 1, 4)


class AnnouncementContractTests(TestCase):
    def test_public_empty_default_contract(self):
        response = self.client.get('/api/announcements/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'count': 0, 'page': 1, 'page_size': 20, 'results': []})

    def test_invalid_query_rejected_and_valid_pagination_echoed(self):
        for query in ({'kind': 'unknown'}, {'kind': ''}, {'page': '0'}, {'page': '-1'},
                      {'page': '1.2'}, {'page': 'x'}, {'page_size': '0'}, {'page_size': '51'},
                      {'page_size': ''}, {'page': ' 1'}, {'page': '+1'}):
            with self.subTest(query=query):
                self.assertEqual(self.client.get('/api/announcements/', query).status_code, 400)
        self.assertEqual(self.client.get('/api/announcements/', {'kind': 'gift', 'page': 2, 'page_size': 50}).json(),
                         {'count': 0, 'page': 2, 'page_size': 50, 'results': []})

    def test_only_get_allowed(self):
        for method in ('post', 'put', 'patch', 'delete', 'head', 'options'):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)('/api/announcements/').status_code, 405)
