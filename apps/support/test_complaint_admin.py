"""Admin permission tests are database-free; integration tests use isolated test DB only."""
from types import SimpleNamespace

from django.contrib import admin
from django.test import SimpleTestCase, RequestFactory, TestCase
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse

from .models import Complaint


class ComplaintAdminPermissionTests(SimpleTestCase):
    def test_registered_with_explicit_staff_permission_and_immutable_original(self):
        self.assertIn(Complaint, admin.site._registry)
        handler = admin.site._registry[Complaint]
        for staff, active, perms, allowed in [
            (False, True, {'support.change_complaint'}, False),
            (True, True, set(), False),
            (True, False, {'support.change_complaint'}, False),
            (True, True, {'support.view_complaint'}, True),
            (True, True, {'support.change_complaint'}, True),
        ]:
            request = RequestFactory().get('/')
            request.user = SimpleNamespace(is_staff=staff, is_active=active,
                has_perm=lambda perm: perm in perms)
            self.assertEqual(handler.has_view_permission(request), allowed)
            self.assertEqual(handler.has_change_permission(request),
                             allowed and 'support.change_complaint' in perms)
            self.assertFalse(handler.has_add_permission(request))
            self.assertFalse(handler.has_delete_permission(request))
        for field in ('description', 'user', 'order', 'category', 'contact', 'status', 'assigned_to'):
            self.assertIn(field, handler.readonly_fields)
        self.assertEqual(handler.actions, None)


class ComplaintAdminWorkflowTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username='ticket-owner')
        self.staff = get_user_model().objects.create_superuser(username='ticket-agent', password='x')
        self.ticket = Complaint.objects.create(user=self.owner, category='service',
            description='原始材料不可覆盖', client_request_id='admin-test')
        self.url = reverse('admin:support_complaint_change', args=[self.ticket.pk])
        self.client.force_login(self.staff)

    def test_handling_copy_marks_status_reason_as_internal(self):
        response = self.client.get(self.url)
        for text in (
            '内部状态变更说明（仅客服可见）',
            '状态和变更时间对投诉用户可见；处理说明仅客服可见。',
            '仅“公开回复”的内容会展示给投诉用户；处理说明和内部备注仅客服可见。',
        ):
            with self.subTest(text=text):
                self.assertContains(response, text)
        response = self.client.post(self.url, {
            'operation': 'handle', 'status': 'processing', 'visibility': 'internal',
        })
        self.assertContains(response, '变更状态时请填写内部状态变更说明（仅客服可见）。')
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, 'pending')
        self.assertFalse(self.ticket.status_history.exists())

    def test_process_reply_keeps_original_and_records_history(self):
        response = self.client.post(self.url, {'operation': 'handle', 'status': 'processing',
            'content': '已经受理您的问题', 'visibility': 'public', 'claim': 'on',
            'reason': '开始调查', 'description': '恶意替换原始材料'})
        self.assertEqual(response.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, 'processing')
        self.assertEqual(self.ticket.assigned_to, self.staff)
        self.assertEqual(self.ticket.description, '原始材料不可覆盖')
        self.assertTrue(self.ticket.messages.filter(content='已经受理您的问题', is_internal=False).exists())
        self.assertContains(self.client.get(self.url), '开始调查')

    def test_view_only_cannot_write_or_post_default_admin_save(self):
        viewer = get_user_model().objects.create_user(username='ticket-viewer', is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename='view_complaint'))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.client.post(self.url, {'operation': 'handle', 'content': '禁止'}).status_code, 403)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self.url, {'description': '禁止默认保存'}).status_code, 403)


    def test_unprivileged_staff_and_owner_cannot_browse_ticket(self):
        for user in [self.owner, get_user_model().objects.create_user(username='unprivileged', is_staff=True)]:
            self.client.force_login(user)
            self.assertIn(self.client.get(self.url).status_code, (302, 403))
            self.assertIn(self.client.post(self.url, {'operation': 'handle'}).status_code, (302, 403))

    def test_status_reason_stays_internal_even_with_public_reply(self):
        from rest_framework.test import APIClient
        reason = 'SECRET-STATUS-REASON-仅内部调查'
        public_content = '已经受理您的问题'
        response = self.client.post(self.url, {
            'operation': 'handle', 'status': 'processing', 'reason': reason,
            'content': public_content, 'visibility': 'public',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.ticket.status_history.get().reason, reason)
        self.assertContains(self.client.get(self.url), reason)
        api = APIClient()
        api.force_authenticate(self.owner)
        response = api.get(f'/api/support/complaints/{self.ticket.number}/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(reason, str(response.data))
        self.assertEqual(len(response.data['status_history']), 1)
        event = response.data['status_history'][0]
        self.assertEqual(set(event), {'from_status', 'to_status', 'created_at'})
        self.assertEqual(event['from_status'], 'pending')
        self.assertEqual(event['to_status'], 'processing')
        self.assertTrue(event['created_at'])
        self.assertEqual([message['content'] for message in response.data['messages']],
                         [public_content])

    def test_internal_notes_do_not_leak_in_user_api(self):
        from rest_framework.test import APIClient
        response = self.client.post(self.url, {'operation': 'handle', 'status': 'pending',
            'content': 'SECRET-NOTE-不对用户公开', 'visibility': 'internal'})
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(self.url), 'SECRET-NOTE-不对用户公开')
        api = APIClient()
        api.force_authenticate(self.owner)
        response = api.get(f'/api/support/complaints/{self.ticket.number}/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('SECRET-NOTE', str(response.data))

    def test_closed_complaint_rejects_additional_admin_writes(self):
        self.ticket.status = 'closed'
        self.ticket.save(update_fields=['status'])
        response = self.client.post(self.url, {'operation': 'handle', 'status': 'processing',
            'reason': '重新打开', 'content': '不能追加', 'visibility': 'internal'})
        self.assertContains(response, '工单已关闭')
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, 'closed')
        self.assertFalse(self.ticket.messages.exists())

    def test_authenticated_attachment_route_checks_ticket_permission_first(self):
        url = reverse('admin:support_complaint_attachment', args=[self.ticket.pk, 999])
        user = get_user_model().objects.create_user(username='no-file-access', is_staff=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_csrf_required(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(self.url, {'operation': 'handle'}).status_code, 403)


    def test_private_attachment_streams_only_for_its_ticket_and_permission(self):
        import io
        import tempfile
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .services import upload_attachment
        with tempfile.TemporaryDirectory(prefix='complaint-admin-test-') as root:
            with self.settings(COMPLAINT_PRIVATE_ROOT=root):
                image = io.BytesIO()
                Image.new('RGB', (2, 2)).save(image, format='PNG')
                attachment = upload_attachment(self.owner,
                    SimpleUploadedFile('evidence.png', image.getvalue(), content_type='image/png'))
                attachment.complaint = self.ticket
                attachment.save(update_fields=['complaint'])
                url = reverse('admin:support_complaint_attachment', args=[self.ticket.pk, attachment.pk])
                self.assertContains(self.client.get(self.url), url)
                response = self.client.get(url)
                try:
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('private', response['Cache-Control'])
                    self.assertIn('no-store', response['Cache-Control'])
                    self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
                    self.assertTrue(b''.join(response.streaming_content).startswith(b'\xff\xd8'))
                finally:
                    # Exhausting the test client's iterator closes the file/response
                    # with its DB-signal guard, even if an assertion above fails.
                    # A second response.close() would bypass that guard.
                    for _ in response.streaming_content:
                        pass
                self.assertTrue(response.closed)
                wrong = reverse('admin:support_complaint_attachment', args=[self.ticket.pk + 99, attachment.pk])
                self.assertEqual(self.client.get(wrong).status_code, 404)
                self.client.logout()
                self.assertEqual(self.client.get(url).status_code, 302)
