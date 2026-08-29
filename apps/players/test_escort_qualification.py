from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from apps.catalog.models import PlayerType

from .escort_qualification import approve_escort_application, reject_escort_application
from .models import Player, PlayerEscortApplication, PlayerEscortQualification


class EscortQualificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='escort-player')
        self.admin = User.objects.create_user(username='escort-admin', is_staff=True)
        self.player_type = PlayerType.objects.create(name='技术陪', priority=2)
        self.player = Player.objects.create(
            user=self.user,
            name='护航申请测试',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_player_can_submit_and_update_pending_application(self):
        response = self.client.post('/api/player/escort-qualification', {
            'experience': '有稳定组队经验，熟悉护航路线和沟通流程。',
            'evidence_urls': ['https://example.com/proof-1'],
        }, format='json')
        self.assertEqual(response.status_code, 200)
        qualification = PlayerEscortQualification.objects.get(player=self.player)
        application = PlayerEscortApplication.objects.get(player=self.player)
        self.assertEqual(qualification.status, PlayerEscortQualification.STATUS_PENDING)
        self.assertEqual(application.status, PlayerEscortApplication.STATUS_PENDING)

        response = self.client.post('/api/player/escort-qualification', {
            'experience': '补充说明：能够稳定完成护航任务，并可配合复盘。',
            'evidence_urls': ['https://example.com/proof-2'],
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PlayerEscortApplication.objects.filter(player=self.player).count(), 1)
        application.refresh_from_db()
        self.assertEqual(application.evidence_urls, ['https://example.com/proof-2'])

    def test_approval_grants_independent_qualification(self):
        self.client.post('/api/player/escort-qualification', {
            'experience': '具备护航经验并熟悉平台服务流程。',
            'evidence_urls': [],
        }, format='json')
        application = PlayerEscortApplication.objects.get(player=self.player)
        approve_escort_application(application, self.admin, '审核通过')

        qualification = PlayerEscortQualification.objects.get(player=self.player)
        self.assertEqual(qualification.status, PlayerEscortQualification.STATUS_APPROVED)
        self.assertEqual(self.player.player_type, self.player_type)

        response = self.client.get('/api/player/escort-qualification')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['qualification']['has_qualification'])
        self.assertFalse(response.data['qualification']['can_submit'])

    def test_rejection_allows_resubmission(self):
        self.client.post('/api/player/escort-qualification', {
            'experience': '具备一定护航经验，提交审核材料。',
            'evidence_urls': [],
        }, format='json')
        application = PlayerEscortApplication.objects.get(player=self.player)
        reject_escort_application(application, '材料不足', self.admin)

        qualification = PlayerEscortQualification.objects.get(player=self.player)
        self.assertEqual(qualification.status, PlayerEscortQualification.STATUS_REJECTED)

        response = self.client.post('/api/player/escort-qualification', {
            'experience': '重新补充完整护航经历和服务能力说明。',
            'evidence_urls': ['https://example.com/new-proof'],
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PlayerEscortApplication.objects.filter(player=self.player).count(), 2)
        qualification.refresh_from_db()
        self.assertEqual(qualification.status, PlayerEscortQualification.STATUS_PENDING)


class PlayerPublicListOrderingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.player_type = PlayerType.objects.create(
            name='排序测试类型',
            priority=1,
            is_active=True,
        )

    def create_player(self, name, *, total_rating, rating_count, total_orders):
        return Player.objects.create(
            name=name,
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
            is_publicly_visible=True,
            total_rating=total_rating,
            rating_count=rating_count,
            total_orders=total_orders,
        )

    def test_default_ordering_prefers_rating_then_review_count_then_orders_then_id(self):
        low_rating = self.create_player(
            '低评分',
            total_rating=9,
            rating_count=2,
            total_orders=100,
        )
        high_rating_fewer_reviews = self.create_player(
            '高评分少评价',
            total_rating=10,
            rating_count=2,
            total_orders=100,
        )
        high_rating_more_reviews_fewer_orders = self.create_player(
            '高评分多评价少接单',
            total_rating=25,
            rating_count=5,
            total_orders=3,
        )
        high_rating_more_reviews_more_orders_earlier = self.create_player(
            '高评分多评价多接单较早',
            total_rating=25,
            rating_count=5,
            total_orders=10,
        )
        high_rating_more_reviews_more_orders_later = self.create_player(
            '高评分多评价多接单较晚',
            total_rating=25,
            rating_count=5,
            total_orders=10,
        )

        response = self.client.get('/api/player/list')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item['id'] for item in response.data],
            [
                high_rating_more_reviews_more_orders_earlier.id,
                high_rating_more_reviews_more_orders_later.id,
                high_rating_more_reviews_fewer_orders.id,
                high_rating_fewer_reviews.id,
                low_rating.id,
            ],
        )

    def test_explicit_total_orders_ordering_remains_supported(self):
        fewer_orders = self.create_player(
            '接单较少',
            total_rating=10,
            rating_count=2,
            total_orders=2,
        )
        more_orders = self.create_player(
            '接单较多',
            total_rating=0,
            rating_count=0,
            total_orders=8,
        )

        response = self.client.get('/api/player/list', {'ordering': '-total_orders'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item['id'] for item in response.data],
            [more_orders.id, fewer_orders.id],
        )
