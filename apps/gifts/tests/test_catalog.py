from django.apps import apps
from django.test import TestCase
from django.core.exceptions import ValidationError
from apps.gifts.models import Gift


class GiftInvariantTests(TestCase):
    def test_rejects_negative_or_non_integer_diamond_price(self):
        for price in (-1, 1.5, True, '2.5', 'not-a-price'):
            with self.subTest(price=price), self.assertRaises(ValidationError):
                gift = Gift(name='测试', price_diamonds=price)
                gift.full_clean()
                gift.save()

    def test_generated_code_is_unique_and_stable_across_price_edit(self):
        gift = Gift.objects.create(name='测试', price_diamonds=17)
        other = Gift.objects.create(name='测试', price_diamonds=17)
        self.assertNotEqual(gift.code, other.code)
        original = gift.code
        gift.price_diamonds = 29
        gift.save()
        gift.refresh_from_db()
        self.assertEqual(gift.code, original)
        self.assertEqual(gift.price_diamonds, 29)
        gift.code = other.code
        with self.assertRaises(ValidationError):
            gift.save()

    def test_malformed_and_duplicate_codes_are_rejected(self):
        gift = Gift.objects.create(name='测试', price_diamonds=1)
        for code in ('unsafe code!', 'UPPERCASE', '', gift.code):
            with self.subTest(code=code), self.assertRaises(ValidationError):
                Gift.objects.create(name='测试', price_diamonds=1, code=code)

    def test_active_requires_image(self):
        with self.assertRaises(ValidationError):
            Gift.objects.create(name='测试', price_diamonds=1, is_active=True)


class GiftModelTests(TestCase):
    def test_catalog_model_and_draft_defaults(self):
        self.assertTrue(apps.is_installed('apps.gifts'), 'gift app must be installed')
        Gift = apps.get_model('gifts', 'Gift')
        gift = Gift.objects.create(name='测试礼物', price_diamonds=17)
        self.assertFalse(gift.is_active)
        self.assertFalse(gift.image)
        self.assertRegex(gift.code, r'^gift_[0-9a-f]{32}$')
        self.assertEqual(gift.price_diamonds, 17)
        self.assertEqual(gift.description, '')
        self.assertEqual(gift.sort_order, 0)
        self.assertIsNotNone(gift.created_at)
        self.assertIsNotNone(gift.updated_at)
