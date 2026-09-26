import json
from types import SimpleNamespace
from django.test import SimpleTestCase
from apps.kook_integration.rendering import render


class CardTests(SimpleTestCase):
    def test_production_source_callbacks_wired_but_all_flags_off(self):
        # Parse source, NEVER import production settings or load .env.
        import ast
        from pathlib import Path
        tree = ast.parse((Path(__file__).resolve().parents[3] / 'config' / 'settings.py').read_text())
        values = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                  if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                  and node.targets[0].id.startswith('KOOK_')}
        self.assertEqual(values.get('KOOK_ORDER_REVALIDATOR'), 'apps.orders.kook_notifications.revalidate_order')
        self.assertEqual(values.get('KOOK_ENTRY_RESOLVER'), 'apps.orders.kook_notifications.resolve_entry')
        for key in ['KOOK_ENABLED', 'KOOK_SEND_ENABLED', 'KOOK_DM_SEND_ENABLED',
                    'KOOK_CHANNEL_SEND_ENABLED', 'KOOK_PRODUCTION_ENABLED',
                    'KOOK_MENTION_ALL_VERIFIED', 'KOOK_ORDER_EVENTS_ENABLED', 'KOOK_WECHAT_URL_LINK_ENABLED']:
            self.assertIs(values.get(key), False, key)

    def event(self, mode='public', audience='public_channel'):
        return SimpleNamespace(event_type='public_slots.opened', payload={
            'fulfillment_mode': mode, 'audience': audience,
            'safe_snapshot': {'participant_count': 0, 'slot_summary': ['0/2人'],
                'package_label': '离线商品', 'price_label': '150钻石',
                'duration_label': '1小时', 'boss_note': '@all 联系13800138000'}})

    def test_legacy_public_terminal_event_overrides_misleading_snapshot_status(self):
        for kind in ['public_slots.closed', 'replacement.resolved', 'order.cancelled']:
            event = self.event()
            event.event_type = kind
            event.payload['safe_snapshot']['status'] = '进行中'
            content = render(event)
            label = '已取消' if kind == 'order.cancelled' else '已关闭'
            self.assertIn('状态：' + label + ' · 不可接单', content)
            self.assertNotIn('状态：进行中', content)
            self.assertIn('＠all 联系13800138000', content)
            self.assertNotIn('(met)all(met)', content)
        event = self.event()
        event.payload['safe_snapshot']['status'] = '进行中'
        self.assertIn('状态：可接单', render(event))
        self.assertNotIn('状态：进行中', render(event))

    def test_current_total_zero_and_targeted_invitation_match_page(self):
        public = render(self.event())
        self.assertIn('人数：0/2人', public)
        self.assertNotIn('人数：无', public)
        private = render(self.event('targeted', 'designated_dm'))
        for text in ['服务对象：仅本人', '订单类型：专属服务', '订单钻石：150钻石', '预订时长：1小时']:
            self.assertIn(text, private)
        self.assertNotIn('人数：', private)
        self.assertIn('＠all 联系13800138000', private)
