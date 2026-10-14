from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.catalog.models import Package

from .models import Order


class BossTimerAccessTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_user(username='timer-boss')
        self.outsider = User.objects.create_user(username='timer-outsider')
        package = Package.objects.create(name='计时权限商品', player_count=1, base_price=10)
        started_at = timezone.now() - timedelta(minutes=5)
        self.order = Order.objects.create(
            order_no='BOSSTIMERACCESS001',
            boss_user=self.boss,
            boss_wechat='timer-boss',
            package=package,
            required_players=1,
            total_price_per_hour=10,
            total_amount=10,
            status=Order.STATUS_IN_PROGRESS,
            paid=True,
            timer_started_at=started_at,
            start_time=started_at,
            is_paused=False,
            paused_duration=0,
        )
        self.client = APIClient()

    def test_anonymous_cannot_pause_order(self):
        response = self.client.post(f'/api/boss/order/{self.order.order_no}/pause')
        self.assertIn(response.status_code, {401, 403})
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paused)

    def test_unrelated_user_cannot_pause_order(self):
        self.client.force_authenticate(self.outsider)
        response = self.client.post(f'/api/boss/order/{self.order.order_no}/pause')
        self.assertEqual(response.status_code, 403)
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paused)

    def test_order_owner_can_pause_and_resume(self):
        self.client.force_authenticate(self.boss)
        pause_response = self.client.post(f'/api/boss/order/{self.order.order_no}/pause')
        self.assertEqual(pause_response.status_code, 200, pause_response.data)
        self.order.refresh_from_db()
        self.assertTrue(self.order.is_paused)

        resume_response = self.client.post(f'/api/boss/order/{self.order.order_no}/resume')
        self.assertEqual(resume_response.status_code, 200, resume_response.data)
        self.order.refresh_from_db()
        self.assertFalse(self.order.is_paused)
