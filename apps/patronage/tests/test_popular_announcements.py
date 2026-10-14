from datetime import timedelta
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from apps.accounts.models import VipTier, VipUpgradeEvent
from apps.accounts.vip import record_consumption
from apps.gifts.models import GiftTransfer
from . import test_announcements as fixtures


@override_settings(GIFT_PURCHASE_ENABLED=True, GIFT_INVENTORY_SEND_ENABLED=True,
                   WECHAT_VIRTUALPAY_ENV=1, FISH_CRACKER_EXCHANGE_RATE=10)
class PopularAnnouncementTests(TransactionTestCase):
    setUp = fixtures.AllAnnouncementTests.setUp
    buy = fixtures.AllAnnouncementTests.buy
    prepare = fixtures.AllAnnouncementTests.prepare
    purchase = fixtures.AllAnnouncementTests.purchase

    def test_global_ties_pagination_and_gift_consumption_boundary(self):
        VipTier.objects.all().delete()
        VipTier.objects.create(code='zero', name='普通', min_consumption=0)
        VipTier.objects.create(code='up', name='高级', min_consumption=100)
        when = timezone.now() - timedelta(days=1)
        expected = []
        for i, offset in enumerate((0, 2, 2, 4)):
            at = when + timedelta(hours=offset)
            with patch('django.utils.timezone.now', return_value=at):
                record_consumption(profile=self.profile, amount=100, source_type='order', reference_id=f'consume-{i}')
                event = VipUpgradeEvent.objects.latest('pk')
                expected.append((at, 'vip_upgrade', event.pk))
                record_consumption(profile=self.profile, amount=-100, source_type='refund', reference_id=f'refund-{i}')
            purchase = self.buy(key=f'gift-{i}')
            gift = GiftTransfer.objects.get(purchase=purchase)
            gift_at = when + timedelta(hours=4-offset)
            GiftTransfer.objects.filter(pk=gift.pk).update(created_at=gift_at)
            expected.append((gift_at, 'gift', gift.pk))
        # Neither real gift purchase nor real patronage currently counts toward VIP.
        self.purchase(key='not-popular')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.cumulative_consumption, 0)
        self.assertEqual(VipUpgradeEvent.objects.count(), 4)
        seen=[]
        for page in range(1, 6):
            response=self.client.get('/api/announcements/', {'kind':'popular', 'page':page, 'page_size':2})
            self.assertEqual(response.status_code, 200)
            data=response.json()
            self.assertEqual(data['count'], 8)
            seen.extend(r['id'] for r in data['results'])
        self.assertEqual(seen, [f'{kind}:{pk}' for _,kind,pk in sorted(expected, reverse=True)])
        self.assertEqual(len(set(seen)),8)
        self.assertEqual(self.client.get('/api/announcements/', {'kind':'all'}).json()['count'],5)
        self.assertEqual(self.client.get('/api/announcements/', {'kind':'gift'}).json()['count'],4)
        self.assertEqual(self.client.get('/api/announcements/', {'kind':'patronage'}).json()['count'],1)
        # Optional actual Django HTTP JSON export for the TS/SFC integration probe.
        import os
        from pathlib import Path
        if os.environ.get('VIP_WIRE_OUTPUT'):
            out=Path(os.environ['VIP_WIRE_OUTPUT']);out.mkdir(parents=True,exist_ok=True)
            for kind in ('popular','gift','patronage'):
                for size in (10,20):
                    response=self.client.get('/api/announcements/',{'kind':kind,'page_size':size})
                    (out/f'{kind}-{size}.json').write_bytes(response.content)
