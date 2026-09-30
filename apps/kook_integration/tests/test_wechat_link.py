from datetime import timedelta
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from apps.kook_integration import navigation


class WechatLinkTests(SimpleTestCase):
    @override_settings(KOOK_WECHAT_URL_LINK_ENABLED=True,
                       KOOK_TEST_SECRETS={'wechat_access_token': 'offline-token'})
    def test_real_adapter_fixed_endpoint_and_navigation_only_payload(self):
        adapter = getattr(navigation, 'wechat_url_link', None)
        self.assertTrue(callable(adapter), 'Missing real WeChat URL Link adapter')
        transport = Mock()
        transport.post.return_value = Mock(status_code=200, json=lambda: {'errcode': 0, 'url_link': 'https://wxaurl.cn/offline'})
        expires = timezone.now() + timedelta(minutes=20)
        with patch('apps.kook_integration.wechat_token.get_access_token', return_value='offline-token'), patch('requests.Session', return_value=transport):
            result = adapter(path=navigation.SAFE_TARGET, query='kook_intent='+'a'*32, expires_at=expires)
        self.assertEqual(result, 'https://wxaurl.cn/offline')
        args, kw = transport.post.call_args
        self.assertEqual(args, ('https://api.weixin.qq.com/wxa/generate_urllink',))
        self.assertEqual(kw['params'], {'access_token': 'offline-token'})
        self.assertEqual(kw['json'], {'path': navigation.SAFE_TARGET, 'query': 'kook_intent='+'a'*32,
            'expire_type': 0, 'expire_time': int(expires.timestamp()), 'is_expire': True, 'env_version': 'release'})
        self.assertFalse(kw['allow_redirects'])
        self.assertFalse(transport.trust_env)
        self.assertNotIn('offline-token', result)

    @override_settings(KOOK_WECHAT_URL_LINK_ENABLED=True, KOOK_TEST_SECRETS={'wechat_access_token':'offline-token'})
    def test_link_errors_redirects_and_invalid_host_fail_closed(self):
        transport = Mock()
        kwargs = dict(path=navigation.SAFE_TARGET, query='kook_intent='+'a'*32,
                      expires_at=timezone.now()+timedelta(minutes=20))
        with patch('apps.kook_integration.wechat_token.get_access_token', return_value='offline-token'), patch('requests.Session', return_value=transport):
            for status, data in [(302, {}), (200, {'errcode':40001}), (200, []),
                    (200, {'errcode':0, 'url_link':'https://evil.test/link'}),
                    (200, {'errcode':0, 'url_link':'https://wxaurl.cn/link?access_token=secret'}),
                    (200, {'errcode':0, 'url_link':'https://user@wxaurl.cn/link'}),
                    (200, {'errcode':0, 'url_link':'https://wxaurl.cn:443/link'})]:
                transport.post.return_value = Mock(status_code=status, json=lambda: data)
                self.assertEqual(navigation.wechat_url_link(**kwargs), '')
            transport.post.side_effect = RuntimeError('sensitive URL')
            self.assertEqual(navigation.wechat_url_link(**kwargs), '')
            transport.post.reset_mock()
            kwargs['path'] = 'https://evil.test'
            self.assertEqual(navigation.wechat_url_link(**kwargs), '')
            transport.post.assert_not_called()

    @override_settings(KOOK_WECHAT_URL_LINK_ENABLED=True)
    def test_invalid_token_retries_link_only_once_via_renewable_provider(self):
        transport = Mock()
        transport.post.side_effect = [Mock(status_code=200, json=lambda: {'errcode': 40001}),
            Mock(status_code=200, json=lambda: {'errcode': 0, 'url_link': 'https://wxaurl.cn/renewed'})]
        with patch('apps.kook_integration.wechat_token.get_access_token', side_effect=['old', 'new']) as token, patch('requests.Session', return_value=transport):
            result = navigation.wechat_url_link(path=navigation.SAFE_TARGET, query='kook_intent='+'a'*32,
                expires_at=timezone.now()+timedelta(minutes=20))
        self.assertEqual(result, 'https://wxaurl.cn/renewed')
        self.assertEqual(token.call_count, 2)
        token.assert_called_with(rejected_token='old')
        self.assertEqual(transport.post.call_count, 2)

    @override_settings(KOOK_WECHAT_URL_LINK_ENABLED=True)
    def test_repeated_invalid_token_stops_after_one_retry_other_errors_never_retry(self):
        for code, count in [(40001, 2), (40014, 2), (42001, 2), (45009, 1), (-1, 1)]:
            transport = Mock()
            transport.post.return_value = Mock(status_code=200, json=lambda: {'errcode': code})
            with patch('apps.kook_integration.wechat_token.get_access_token', side_effect=['old', 'new']) as token, patch('requests.Session', return_value=transport):
                self.assertEqual(navigation.wechat_url_link(path=navigation.SAFE_TARGET, query='kook_intent='+'a'*32,
                    expires_at=timezone.now()+timedelta(minutes=20)), '')
            self.assertEqual(transport.post.call_count, count)
            self.assertEqual(token.call_count, count)

    def test_disabled_adapter_does_not_read_credentials_or_network(self):
        adapter = getattr(navigation, 'wechat_url_link', None)
        self.assertTrue(callable(adapter))
        with patch('requests.Session') as session:
            self.assertEqual(adapter(path=navigation.SAFE_TARGET, query='kook_intent='+'a'*32,
                expires_at=timezone.now()+timedelta(minutes=1)), '')
            session.assert_not_called()
