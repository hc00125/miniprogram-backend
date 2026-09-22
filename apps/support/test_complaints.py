from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient


class ComplaintAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='complainer')
        self.other = get_user_model().objects.create_user(username='other')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def payload(self, **changes):
        data = dict(category='service', description='服务过程中遇到问题需要处理', client_request_id='request-1')
        data.update(changes)
        return data

    def test_idempotent_creation_conflict_and_order_ownership(self):
        first = self.client.post('/api/support/complaints/', self.payload(), format='json')
        again = self.client.post('/api/support/complaints/', self.payload(), format='json')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(first.data['number'], again.data['number'])
        changed = self.client.post('/api/support/complaints/', self.payload(category='fee'), format='json')
        self.assertEqual(changed.status_code, 409)
        invalid = self.client.post('/api/support/complaints/', self.payload(client_request_id='second', order_no='not-my-order'), format='json')
        self.assertEqual(invalid.status_code, 400)

    def test_message_public_visibility_and_state_history(self):
        from .models import Complaint
        response = self.client.post('/api/support/complaints/', self.payload(), format='json')
        number = response.data['number']
        url = f'/api/support/complaints/{number}/messages/'
        reply = self.client.post(url, {'content': '用户补充说明', 'attachment_ids': []}, format='json')
        self.assertEqual(reply.status_code, 201)
        from .models import ComplaintMessage
        from .services import transition_complaint
        complaint = Complaint.objects.get(number=number)
        ComplaintMessage.objects.create(complaint=complaint, author=self.other, content='内部秘密', is_internal=True)
        staff = get_user_model().objects.create_superuser(username='staff', password='x')
        transition_complaint(complaint, 'awaiting_user', staff, '需要补充')
        self.client.post(url, {'content': '新的补充'}, format='json')
        detail = self.client.get(f'/api/support/complaints/{number}/').data
        self.assertEqual(detail['status'], 'processing')
        self.assertEqual(len(detail['messages']), 2)
        self.assertEqual(len(detail['status_history']), 3)
        self.assertNotIn('内部秘密', str(detail))
        transition_complaint(complaint, 'closed', staff, '已关闭')
        self.assertEqual(self.client.post(url, {'content': '不能覆盖'}, format='json').status_code, 409)

    def test_private_image_upload_attach_and_access(self):
        import io
        import tempfile
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        image = io.BytesIO()
        Image.new('RGB', (10, 10)).save(image, 'PNG')
        with tempfile.TemporaryDirectory() as directory, override_settings(COMPLAINT_PRIVATE_ROOT=directory):
            upload = self.client.post('/api/support/complaint-attachments/', {'file': SimpleUploadedFile('photo.png', image.getvalue(), 'image/png')})
            self.assertEqual(upload.status_code, 201)
            attachment = upload.data
            self.assertEqual(set(attachment), {'id', 'name', 'content_url'})
            self.assertNotIn('/media/', attachment['content_url'])
            self.assertEqual(self.client.get(attachment['content_url']).status_code, 200)
            self.client.force_authenticate(self.other)
            self.assertEqual(self.client.get(attachment['content_url']).status_code, 404)
            denied = self.client.post('/api/support/complaints/', self.payload(attachment_ids=[attachment['id']]), format='json')
            self.assertEqual(denied.status_code, 400)
            self.client.force_authenticate(self.user)
            created = self.client.post('/api/support/complaints/', self.payload(attachment_ids=[attachment['id']]), format='json')
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.data['attachments'], [attachment])
            retry = self.client.post('/api/support/complaints/', self.payload(attachment_ids=[attachment['id']]), format='json')
            self.assertEqual(retry.status_code, 200)
            reused = self.client.post('/api/support/complaints/', self.payload(client_request_id='reuse', attachment_ids=[attachment['id']]), format='json')
            self.assertEqual(reused.status_code, 400)
            bad = self.client.post('/api/support/complaint-attachments/', {'file': SimpleUploadedFile('fake.png', b'<script>evil</script>', 'image/png')})
            self.assertEqual(bad.status_code, 400)

    def test_cleanup_dry_run_and_retention(self):
        import tempfile
        import io
        from datetime import timedelta
        from django.utils import timezone
        from django.core.management import call_command
        from django.test import override_settings
        from django.core.files.base import ContentFile
        from .models import ComplaintAttachment
        from .services import private_storage
        with tempfile.TemporaryDirectory() as directory, override_settings(COMPLAINT_PRIVATE_ROOT=directory, COMPLAINT_ORPHAN_RETENTION_HOURS=48):
            storage = private_storage()
            name = storage.save('old.jpg', ContentFile(b'old'))
            old = ComplaintAttachment.objects.create(uploader=self.user, name='old', storage_name=name, content_type='image/jpeg', size=3)
            ComplaintAttachment.objects.filter(pk=old.pk).update(created_at=timezone.now()-timedelta(days=3))
            call_command('cleanup_complaint_attachments', stdout=io.StringIO())
            self.assertTrue(storage.exists(name))
            call_command('cleanup_complaint_attachments', execute=True, stdout=io.StringIO())
            self.assertFalse(storage.exists(name))
            self.assertFalse(ComplaintAttachment.objects.filter(pk=old.pk).exists())

    def test_create_rate_limit_shared_database(self):
        from django.test import override_settings
        with override_settings(COMPLAINT_CREATE_LIMIT_PER_HOUR=1):
            self.assertEqual(self.client.post('/api/support/complaints/', self.payload(), format='json').status_code, 201)
            self.assertEqual(self.client.post('/api/support/complaints/', self.payload(), format='json').status_code, 200)
            self.assertEqual(self.client.post('/api/support/complaints/', self.payload(client_request_id='new'), format='json').status_code, 429)

    def test_message_and_upload_rate_limits(self):
        import io
        import tempfile
        from PIL import Image
        from django.test import override_settings
        from django.core.files.uploadedfile import SimpleUploadedFile
        number = self.client.post('/api/support/complaints/', self.payload(), format='json').data['number']
        with override_settings(COMPLAINT_MESSAGE_LIMIT_PER_HOUR=1):
            url = f'/api/support/complaints/{number}/messages/'
            self.assertEqual(self.client.post(url, {'content': 'first'}, format='json').status_code, 201)
            self.assertEqual(self.client.post(url, {'content': 'second'}, format='json').status_code, 429)
        with tempfile.TemporaryDirectory() as directory, override_settings(COMPLAINT_PRIVATE_ROOT=directory, COMPLAINT_UPLOAD_LIMIT_PER_HOUR=1):
            image = io.BytesIO()
            Image.new('RGB', (2, 2)).save(image, 'PNG')
            for expected in (201, 429):
                response = self.client.post('/api/support/complaint-attachments/', {'file': SimpleUploadedFile('photo.png', image.getvalue(), 'image/png')})
                self.assertEqual(response.status_code, expected)

    def test_inactive_legacy_account_cannot_submit(self):
        from apps.catalog.models import PlayerType
        from apps.players.models import Player
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        kind = PlayerType.objects.create(name='complaint-test-kind', priority=1)
        Player.objects.create(user=self.user, name='complaint-test-player', player_type=kind, session_token='inactive-token')
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer inactive-token')
        self.assertEqual(self.client.post('/api/support/complaints/', self.payload(), format='json').status_code, 401)

    def test_real_order_owner_and_participant_jwt(self):
        from apps.catalog.models import Package, PlayerType
        from apps.orders.models import Order, OrderPlayer
        from apps.players.models import Player
        from rest_framework_simplejwt.tokens import RefreshToken
        package = Package.objects.create(name='test', base_price=1)
        order = Order.objects.create(order_no='complaint-order', boss_user=self.other, boss_wechat='x', package=package, required_players=1)
        body = self.payload(order_no=order.order_no)
        self.assertEqual(self.client.post('/api/support/complaints/', body, format='json').status_code, 400)
        player = Player.objects.create(user=self.user, name='participant', player_type=PlayerType.objects.create(name='kind', priority=1))
        OrderPlayer.objects.create(order=order, player=player)
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.user).access_token))
        self.assertEqual(self.client.post('/api/support/complaints/', body, format='json').status_code, 201)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.other).access_token))
        self.assertEqual(self.client.post('/api/support/complaints/', body, format='json').status_code, 201)

    def test_validation_boundaries_and_invalid_token(self):
        for changes in ({'description': 'short'}, {'description': 'x'*1001}, {'category': 'unknown'}, {'attachment_ids': [1, 2, 3, 4]}, {'attachment_ids': [1, 1]}, {'contact': 'x'*101}, {'client_request_id': ''}):
            with self.subTest(changes=changes):
                self.assertEqual(self.client.post('/api/support/complaints/', self.payload(**changes), format='json').status_code, 400)
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer invalid')
        self.assertEqual(self.client.get('/api/support/complaints/').status_code, 401)

    def test_create_persist_read_paginate_and_isolate(self):
        response = self.client.post('/api/support/complaints/', self.payload(), format='json')
        self.assertEqual(response.status_code, 201)
        number = response.data['number']
        detail = self.client.get(f'/api/support/complaints/{number}/')
        self.assertEqual(detail.data['description'], self.payload()['description'])
        self.assertEqual(detail.data['status'], 'pending')
        self.assertEqual(self.client.get('/api/support/complaints/').data['count'], 1)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f'/api/support/complaints/{number}/').status_code, 404)
        self.assertEqual(self.client.get('/api/support/complaints/').data['results'], [])
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/support/complaints/').status_code, 401)
