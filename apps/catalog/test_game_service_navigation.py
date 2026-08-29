from django.contrib import admin
from django.test import TestCase

from apps.catalog.models import GameService, Package, PackageGroup


class GameServiceNavigationTests(TestCase):
    def setUp(self):
        self.arena = GameService.objects.get(code='arena-breakout')
        self.delta = GameService.objects.get(code='delta-force')
        self.arena.icon_url = 'https://cdn.example.com/arena.png'
        self.arena.save(update_fields=['icon_url', 'updated_at'])

        self.arena_group = PackageGroup.objects.create(
            game_service=self.arena,
            name='推荐套餐',
            sort_order=10,
            is_active=True,
        )
        self.delta_group = PackageGroup.objects.create(
            game_service=self.delta,
            name='烽火地带',
            sort_order=10,
            is_active=True,
        )
        PackageGroup.objects.create(
            game_service=self.delta,
            name='隐藏分类',
            sort_order=99,
            is_active=False,
        )
        self.package = Package.objects.create(
            name='暗区四套四弹',
            group=self.arena_group,
            base_price=15,
            player_count=4,
            is_active=True,
        )

    def test_seeded_games_exist(self):
        self.assertEqual(self.arena.name, '暗区突围')
        self.assertEqual(self.delta.name, '三角洲行动')

    def test_catalog_navigation_groups_by_game(self):
        response = self.client.get('/api/boss/catalog-navigation')
        self.assertEqual(response.status_code, 200)
        games = response.json()['games']
        self.assertEqual([item['code'] for item in games[:2]], ['arena-breakout', 'delta-force'])

        arena = next(item for item in games if item['code'] == 'arena-breakout')
        delta = next(item for item in games if item['code'] == 'delta-force')
        self.assertTrue(arena['icon_url'].endswith('/arena.png'))
        self.assertIn('推荐套餐', [group['name'] for group in arena['groups']])
        self.assertIn('烽火地带', [group['name'] for group in delta['groups']])
        self.assertNotIn('隐藏分类', [group['name'] for group in delta['groups']])

    def test_package_payload_exposes_game_service(self):
        response = self.client.get('/api/boss/packages')
        self.assertEqual(response.status_code, 200)
        item = next(row for row in response.json() if row['id'] == self.package.id)
        self.assertEqual(item['game_service_id'], self.arena.id)
        self.assertEqual(item['game_service_name'], '暗区突围')

    def test_game_service_is_registered_in_django_admin(self):
        self.assertTrue(admin.site.is_registered(GameService))
