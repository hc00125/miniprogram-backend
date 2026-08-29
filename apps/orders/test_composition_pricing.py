from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.catalog.models import (
    CompositionSku,
    Package,
    PackageFamily,
    PackageSpec,
    PlayerOffer,
    PlayerType,
)
from apps.players.models import Player

from .composition_pricing import quote_composition
from .designated_drafts import create_draft, submit_draft
from .designations import decline_designation
from .models import Order, OrderPricingLine
from .services import grab_order


class StaticCompositionPricingTests(TestCase):
    """Focused regression coverage for identity-free static composition pricing."""

    def setUp(self):
        self.boss = User.objects.create_user(username='composition-boss')
        self.entertainment = PlayerType.objects.create(name='组合娱乐陪', priority=501)
        self.technical = PlayerType.objects.create(name='组合技术陪', priority=502)
        self.gold = PlayerType.objects.create(name='组合金牌陪', priority=503)
        self.family = PackageFamily.objects.create(name='四套四弹组合', code='composition-four-sets')
        self.packages = {}
        # Every 1/2/3-person sibling contains every tier used by the static
        # calculator.  Base entertainment totals: 10 / 20 / 30.
        prices = {
            1: (10, 20, 30),
            2: (20, 40, 60),
            3: (30, 60, 90),
        }
        for count, (entertainment_price, technical_price, gold_price) in prices.items():
            package = Package.objects.create(
                name=f'{count}人四套四弹',
                base_price=entertainment_price,
                player_count=count,
                package_family=self.family,
                is_active=True,
            )
            self.packages[count] = package
            for player_type, price in (
                (self.entertainment, entertainment_price),
                (self.technical, technical_price),
                (self.gold, gold_price),
            ):
                PackageSpec.objects.create(
                    package=package,
                    name=f'{count}人{player_type.name}',
                    display_name=player_type.name,
                    price=price,
                    required_player_type=player_type,
                    is_active=True,
                )

        self.base_spec = PackageSpec.objects.get(
            package=self.packages[3],
            required_player_type=self.entertainment,
        )
        self.gold_player = self.make_player('composition-gold', '金牌A', self.gold)
        self.public_gold = self.make_player('composition-public-gold', '金牌B', self.gold)
        PlayerOffer.objects.create(player=self.gold_player, package_family=self.family, is_active=True)

        self.sku_gold = self.make_sku(
            signature=[{'player_type_id': self.gold.id, 'count': 1}],
            price=50,
            product_id='composition_gold_50',
        )
        self.sku_public = self.make_sku(signature=[], price=30, product_id='composition_public_30')

    def make_player(self, username, name, player_type):
        user = User.objects.create_user(username=username)
        return Player.objects.create(
            user=user,
            name=name,
            player_type=player_type,
            status=Player.STATUS_APPROVED,
            is_online=True,
        )

    def make_sku(self, *, signature, price, product_id):
        virtual_package = Package.objects.create(
            name=f'内部静态组合{product_id}',
            base_price=price,
            player_count=1,
            is_active=True,
        )
        virtual_spec = PackageSpec.objects.create(
            package=virtual_package,
            name=f'内部静态组合规格{product_id}',
            price=price,
            is_active=True,
        )
        sku = CompositionSku.objects.create(
            package_family=self.family,
            base_player_type=self.entertainment,
            required_players=3,
            designated_type_signature=signature,
            total_price_per_hour=Decimal(str(price)),
            virtual_package_spec=virtual_spec,
            is_active=True,
        )
        # Imported late to keep this test about the order flow rather than the
        # payment model's setup details.
        from apps.payments.models import VirtualProductBinding

        VirtualProductBinding.objects.create(
            spec=virtual_spec,
            product_id=product_id,
            goods_price_fen=price * 100,
            is_active=True,
        )
        return sku

    def create_gold_draft(self):
        return create_draft(user=self.boss, payload={
            'boss_wechat': 'composition-boss-openid',
            'game_id': 'COMPOSITION-GAME',
            'base_spec_id': self.base_spec.id,
            'designated_player_ids': [self.gold_player.id],
            'booked_hours': 2,
        })

    def test_designated_gold_uses_single_person_sku_and_public_remainder(self):
        quote = quote_composition(
            base_spec=self.base_spec,
            designated_player_ids=[self.gold_player.id],
            booked_hours=2,
            require_virtual_binding=True,
        )

        self.assertEqual(quote['composition_sku'].id, self.sku_gold.id)
        self.assertEqual(quote['total_price_per_hour'], Decimal('50.00'))
        self.assertEqual(quote['total_amount'], Decimal('100.00'))
        self.assertEqual(quote['public_players'], 2)
        self.assertEqual(
            [(line['kind'], line['player_count'], line['amount_per_hour']) for line in quote['lines']],
            [('designated', 1, Decimal('30.00')), ('public', 2, Decimal('20.00'))],
        )

    def test_public_high_tier_grab_does_not_change_static_composition_price(self):
        draft = self.create_gold_draft()
        order, created = submit_draft(user=self.boss, draft_id=draft.id)
        self.assertTrue(created)
        self.assertEqual(order.total_price_per_hour, 50.0)
        self.assertEqual(order.total_amount, 100.0)

        grab_order(order.order_no, self.public_gold, self.public_gold.user)
        order.refresh_from_db()

        self.assertEqual(order.composition_sku_id, self.sku_gold.id)
        self.assertEqual(order.total_price_per_hour, 50.0)
        self.assertEqual(order.total_amount, 100.0)

    def test_declined_designation_reprices_to_static_public_combination(self):
        draft = self.create_gold_draft()
        order, _ = submit_draft(user=self.boss, draft_id=draft.id)

        decline_designation(order.order_no, self.gold_player, self.gold_player.user)
        order.refresh_from_db()

        self.assertEqual(order.composition_sku_id, self.sku_public.id)
        self.assertEqual(order.total_price_per_hour, 30.0)
        self.assertEqual(order.total_amount, 60.0)
        lines = list(order.pricing_lines.all())
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].source, OrderPricingLine.SOURCE_PUBLIC)
        self.assertEqual(lines[0].quantity, 3)
        self.assertEqual(lines[0].amount, Decimal('30.00'))

    def test_missing_virtual_binding_blocks_invites_and_paymentable_order(self):
        # The gold SKU starts bound.  Deactivate just its binding and verify
        # submit fails before it creates an order/designation.
        self.sku_gold.virtual_package_spec.virtual_payment_bindings.update(is_active=False)
        draft = self.create_gold_draft()

        with self.assertRaises(ValidationError):
            submit_draft(user=self.boss, draft_id=draft.id)

        self.assertEqual(Order.objects.count(), 0)

    def test_removed_binding_blocks_virtual_payment_for_existing_composition_order(self):
        draft = self.create_gold_draft()
        order, _ = submit_draft(user=self.boss, draft_id=draft.id)
        self.sku_gold.virtual_package_spec.virtual_payment_bindings.update(is_active=False)

        from apps.payments.virtualpay import resolve_virtual_product

        with self.assertRaises(ValidationError):
            resolve_virtual_product(order)

    def test_virtual_payment_uses_static_sku_price_and_hours_as_quantity(self):
        """The WeChat product is one static SKU; duration is its quantity."""
        draft = self.create_gold_draft()
        order, _ = submit_draft(user=self.boss, draft_id=draft.id)

        from apps.payments.virtualpay import resolve_virtual_product

        product = resolve_virtual_product(order)

        self.assertEqual(product['product_id'], 'composition_gold_50')
        self.assertEqual(product['goods_price_fen'], 5000)
        self.assertEqual(product['quantity'], 2)
        self.assertEqual(product['expected_total_fen'], 10000)

    def test_decline_releases_slot_when_fallback_sku_is_missing(self):
        # Operations may accidentally omit the all-public fallback.  A player
        # still must be able to decline; the order becomes explicitly
        # unpayable instead of rolling the decline back or using legacy price.
        CompositionSku.objects.filter(pk=self.sku_public.pk).update(is_active=False)
        draft = self.create_gold_draft()
        order, _ = submit_draft(user=self.boss, draft_id=draft.id)

        decline_designation(order.order_no, self.gold_player, self.gold_player.user)
        order.refresh_from_db()

        self.assertIsNone(order.composition_sku_id)
        self.assertTrue(order.composition_pricing_error)
        self.assertIsNone(order.designated_players)
        self.assertEqual(order.pricing_lines.count(), 0)
