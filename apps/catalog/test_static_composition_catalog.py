from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.players.models import Player

from .composition import calculate_static_composition
from .models import (
    CompositionSku,
    GameService,
    Package,
    PackageFamily,
    PackageGroup,
    PackageSpec,
    PlayerOffer,
    PlayerType,
)


class StaticCompositionCatalogTests(TestCase):
    def setUp(self):
        self.entertainment = PlayerType.objects.create(name='娱乐陪', priority=10)
        self.technical = PlayerType.objects.create(name='技术陪', priority=20)
        self.gold = PlayerType.objects.create(name='金牌陪', priority=30)
        self.star = PlayerType.objects.create(name='明星陪', priority=40)
        self.game = GameService.objects.create(name='测试游戏', code='catalog-composition-test')
        self.group = PackageGroup.objects.create(game_service=self.game, name='测试套餐')
        self.family = PackageFamily.objects.create(
            game_service=self.game,
            name='四套四弹',
            code='test-four-sets-four-bullets',
        )
        self.packages = {}
        prices = {
            self.entertainment.id: {1: 20, 2: 40, 3: 60},
            self.technical.id: {1: 22, 2: 44, 3: 66},
            self.gold.id: {1: 25, 2: 50, 3: 75},
            self.star.id: {1: 30, 2: 60, 3: 90},
        }
        for player_count in (1, 2, 3):
            package = Package.objects.create(
                name=f'四套四弹 {player_count} 人',
                group=self.group,
                package_family=self.family,
                player_count=player_count,
                base_price=prices[self.entertainment.id][player_count],
                is_active=True,
            )
            self.packages[player_count] = package
            for player_type in (self.entertainment, self.technical, self.gold, self.star):
                PackageSpec.objects.create(
                    package=package,
                    name=f'{player_type.name} {player_count} 人',
                    price=prices[player_type.id][player_count],
                    required_player_type=player_type,
                    is_active=True,
                )

    def test_static_price_replaces_one_public_slot_with_designated_single_sku(self):
        quote = calculate_static_composition(
            package_family=self.family,
            base_player_type=self.entertainment,
            required_players=3,
            designated_type_signature=[{'player_type_id': self.gold.id, 'count': 1}],
        )

        self.assertEqual(quote['total_price_per_hour'], Decimal('65.00'))
        self.assertEqual(quote['public_players'], 2)
        self.assertEqual(
            [(line['kind'], line['player_count'], line['amount_per_hour']) for line in quote['lines']],
            [('designated', 1, Decimal('25.00')), ('public', 2, Decimal('40.00'))],
        )

    def test_composition_sku_canonicalizes_key_and_resolves(self):
        sku = CompositionSku.objects.create(
            package_family=self.family,
            base_player_type=self.entertainment,
            required_players=3,
            designated_type_signature=[
                {'player_type_id': self.gold.id, 'count': 1},
                {'player_type_id': self.technical.id, 'count': 1},
            ],
            total_price_per_hour=Decimal('67.00'),
        )

        self.assertEqual(
            sku.designated_type_signature,
            [
                {'player_type_id': self.technical.id, 'count': 1},
                {'player_type_id': self.gold.id, 'count': 1},
            ],
        )
        self.assertEqual(
            CompositionSku.resolve_for(
                package_family=self.family.id,
                base_player_type=self.entertainment,
                required_players=3,
                designated_type_signature=[
                    {'player_type_id': self.gold.id, 'count': 1},
                    {'player_type_id': self.technical.id, 'count': 1},
                ],
                active_only=False,
            ),
            sku,
        )

    def test_designated_type_cannot_be_lower_than_base_type(self):
        with self.assertRaises(ValidationError):
            calculate_static_composition(
                package_family=self.family,
                base_player_type=self.technical,
                required_players=2,
                designated_type_signature=[{'player_type_id': self.entertainment.id, 'count': 1}],
            )

    def test_offer_manager_filters_disabled_assignments_and_unavailable_players(self):
        player = Player.objects.create(
            name='商品族测试陪玩',
            player_type=self.gold,
            status=Player.STATUS_APPROVED,
            can_be_designated=True,
        )
        active_offer = PlayerOffer.objects.create(player=player, package_family=self.family)
        disabled_offer = PlayerOffer.objects.create(
            player=player,
            package_family=PackageFamily.objects.create(name='备用装备', code='test-backup-equipment'),
            is_active=False,
        )

        self.assertEqual(list(PlayerOffer.objects.active()), [active_offer])
        self.assertEqual(list(PlayerOffer.objects.available()), [active_offer])
        player.can_be_designated = False
        player.save(update_fields=['can_be_designated'])
        self.assertFalse(PlayerOffer.objects.available().exists())
        self.assertEqual(disabled_offer.is_active, False)

    def test_player_offer_api_returns_only_available_equipment_families(self):
        player = Player.objects.create(
            name='商品族接口测试陪玩',
            player_type=self.gold,
            status=Player.STATUS_APPROVED,
            can_be_designated=True,
        )
        PlayerOffer.objects.create(player=player, package_family=self.family)
        hidden_family = PackageFamily.objects.create(name='隐藏装备', code='test-hidden-equipment')
        PlayerOffer.objects.create(player=player, package_family=hidden_family, is_active=False)

        response = self.client.get(f'/api/catalog/players/{player.id}/offers')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['player_id'], player.id)
        self.assertEqual(len(payload['offers']), 1)
        family = payload['offers'][0]['package_family']
        self.assertEqual(family['id'], self.family.id)
        self.assertEqual([package['player_count'] for package in family['packages']], [1, 2, 3])
