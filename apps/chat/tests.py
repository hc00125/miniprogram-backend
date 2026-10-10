from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order

from .models import ChatMessage, ChatReadStatus


class ChatAccessControlTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_user(username='chat-boss')
        ClientProfile.objects.create(
            user=self.boss,
            openid='openid_chat_boss',
            nickname='真实老板昵称',
        )
        self.outsider = User.objects.create_user(username='chat-outsider')
        ClientProfile.objects.create(
            user=self.outsider,
            openid='openid_chat_outsider',
            nickname='陌生人',
        )
        package = Package.objects.create(name='聊天权限商品', player_count=1, base_price=10)
        self.order = Order.objects.create(
            order_no='CHATACCESS001',
            boss_user=self.boss,
            boss_wechat='openid_chat_boss',
            package=package,
            required_players=1,
            total_price_per_hour=10,
            total_amount=10,
            status=Order.STATUS_WAITING,
        )
        self.client = APIClient()

    def test_anonymous_user_cannot_read_order_chat(self):
        response = self.client.get(f'/api/chat/{self.order.order_no}/messages')
        self.assertIn(response.status_code, {401, 403})

    def test_unrelated_authenticated_user_cannot_read_or_write_chat(self):
        self.client.force_authenticate(self.outsider)
        read_response = self.client.get(f'/api/chat/{self.order.order_no}/messages')
        send_response = self.client.post(
            f'/api/chat/{self.order.order_no}/send',
            {'content': '越权消息'},
            format='json',
        )
        self.assertEqual(read_response.status_code, 403)
        self.assertEqual(send_response.status_code, 403)
        self.assertFalse(ChatMessage.objects.exists())

    def test_boss_sender_identity_is_derived_server_side(self):
        self.client.force_authenticate(self.boss)
        with patch('apps.chat.views.ensure_texts_safe', return_value=None):
            response = self.client.post(
                f'/api/chat/{self.order.order_no}/send',
                {
                    'sender_type': 'admin',
                    'sender_id': 'forged-admin-id',
                    'sender_name': '伪造管理员',
                    'content': '正常消息',
                },
                format='json',
            )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['sender_type'], 'boss')
        self.assertEqual(response.data['sender_id'], str(self.boss.pk))
        self.assertEqual(response.data['sender_name'], '真实老板昵称')
        message = ChatMessage.objects.get()
        self.assertEqual(message.sender_type, 'boss')
        self.assertEqual(message.sender_id, str(self.boss.pk))
        self.assertEqual(message.sender_name, '真实老板昵称')

    def test_read_identity_is_derived_server_side(self):
        self.client.force_authenticate(self.boss)
        response = self.client.post(
            f'/api/chat/{self.order.order_no}/read',
            {'reader_type': 'admin', 'reader_id': 'forged'},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        status_row = ChatReadStatus.objects.get()
        self.assertEqual(status_row.reader_type, 'boss')
        self.assertEqual(status_row.reader_id, str(self.boss.pk))
