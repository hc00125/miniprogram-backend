from django.test import TestCase
from rest_framework.test import APIClient

from apps.catalog.models import PlayerType

from .models import Player


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
