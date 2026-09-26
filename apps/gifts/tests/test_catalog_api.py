from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from apps.gifts.models import Gift
from .test_images import image_file


class CatalogTests(TestCase):
    def test_public_read_only_catalog_hides_drafts_and_invalid_images(self):
        Gift.objects.create(name='draft', price_diamonds=1)
        good = Gift.objects.create(name='rose', price_diamonds=20, image=image_file(), is_active=True)
        bad = Gift.objects.create(name='missing', price_diamonds=1)
        Gift.objects.filter(pk=bad.pk).update(image='missing.png', is_active=True)
        response = self.client.get('/api/gifts/catalog/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([x['code'] for x in response.json()['results']], [good.code])
        self.assertEqual(response.json()['results'][0]['price_diamonds'], 20)
        self.assertEqual(self.client.post('/api/gifts/catalog/', {}).status_code, 405)
        for endpoint in ('inventory', 'purchases', 'transfers', 'earnings'):
            self.assertIn(self.client.get('/api/gifts/' + endpoint + '/').status_code, (401, 403))

    @override_settings(GIFT_CATALOG_ENABLED=False)
    def test_catalog_switch(self):
        self.assertEqual(self.client.get('/api/gifts/catalog/').status_code, 503)

    def test_admin_create_edit_and_publication_validation(self):
        url = '/admin/gifts/gift/add/'
        self.assertEqual(self.client.get(url).status_code, 302)
        user = get_user_model().objects.create_user(username='operator', password='test', is_staff=True, is_superuser=True)
        self.client.force_login(user)
        response = self.client.post(url, {'name': 'gift', 'kind': 'gift', 'code': 'rose', 'price_diamonds': 11, 'sort_order': 0, '_save': 'Save'})
        self.assertEqual(response.status_code, 302)
        gift = Gift.objects.get(code='rose')
        response = self.client.post('/admin/gifts/gift/%s/change/' % gift.pk,
                                    {'name': 'new', 'price_diamonds': 12, 'sort_order': 0, 'is_active': 'on', '_save': 'Save'})
        self.assertEqual(response.status_code, 200)
        gift.refresh_from_db()
        self.assertFalse(gift.is_active)
        response = self.client.post('/admin/gifts/gift/%s/change/' % gift.pk,
                                    {'name': 'new', 'price_diamonds': 12, 'sort_order': 0, 'image': image_file(), 'is_active': 'on', '_save': 'Save'})
        self.assertEqual(response.status_code, 302)
        gift.refresh_from_db()
        self.assertEqual(gift.code, 'rose')
        self.assertEqual(self.client.get('/api/gifts/catalog/').json()['results'][0]['name'], 'new')
