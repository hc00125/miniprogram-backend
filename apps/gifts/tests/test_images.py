from io import BytesIO
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.test import TestCase
from apps.gifts.models import Gift


def image_file(fmt='PNG'):
    stream = BytesIO()
    Image.new('RGB', (8, 8)).save(stream, format=fmt)
    return SimpleUploadedFile('gift.' + fmt.lower(), stream.getvalue())


class ImageTests(TestCase):
    def test_upload_decode_size_and_type_are_validated(self):
        for upload in (SimpleUploadedFile('fake.jpg', b'not an image'),
                       SimpleUploadedFile('bad.svg', b'<svg></svg>'),
                       SimpleUploadedFile('huge.png', b'x' * (2 * 1024 * 1024 + 1))):
            with self.subTest(name=upload.name), self.assertRaises(ValidationError):
                Gift.objects.create(name='bad', image=upload, price_diamonds=10)
        for fmt in ('PNG', 'JPEG', 'WEBP'):
            gift = Gift.objects.create(name=fmt, image=image_file(fmt), price_diamonds=10, is_active=True)
            gift.save()

    def test_blank_name_and_missing_storage_file_rejected(self):
        with self.assertRaises(ValidationError):
            Gift.objects.create(name='  ', price_diamonds=1)
        with self.assertRaises(ValidationError):
            Gift.objects.create(name='missing', price_diamonds=1, image='missing.png', is_active=True)
