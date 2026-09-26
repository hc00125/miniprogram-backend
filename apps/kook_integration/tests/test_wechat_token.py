import importlib
import tempfile
from unittest.mock import Mock, patch
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings


@override_settings(WECHAT_APP_ID='offline-app', KOOK_TEST_SECRETS={'wechat_app_id': 'offline-app', 'wechat_app_secret': 'offline-secret'})
class TokenTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.setting = override_settings(KOOK_WECHAT_TOKEN_LOCK_DIR=self.directory.name)
        self.setting.enable()
        self.addCleanup(self.setting.disable)

    def provider(self):
        name = 'apps.kook_integration.wechat_token'
        self.assertIsNotNone(importlib.util.find_spec(name), 'Missing renewable token provider')
        return importlib.import_module(name)

    def test_existing_subscription_cache_reused_without_http(self):
        provider = self.provider()
        cache.set('wechat-stable-access-token:offline-app', 'subscription-token')
        with patch('requests.Session') as session:
            self.assertEqual(provider.get_access_token(), 'subscription-token')
        session.assert_not_called()

    def test_concurrent_threads_fetch_once(self):
        from concurrent.futures import ThreadPoolExecutor
        provider = self.provider()
        transport = Mock()
        transport.post.return_value = Mock(status_code=200, json=lambda: {'access_token': 'concurrent', 'expires_in': 7200})
        with patch('requests.Session', return_value=transport), ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: provider.get_access_token(), range(8)))
        self.assertEqual(results, ['concurrent'] * 8)
        self.assertEqual(transport.post.call_count, 1)

    def test_process_lock_contention_fails_bounded_without_http(self):
        import fcntl
        import hashlib
        from pathlib import Path
        provider = self.provider()
        path = Path(self.directory.name) / ('wechat-' + hashlib.sha256(b'offline-app').hexdigest() + '.lock')
        with path.open('w') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('requests.Session') as session:
                with self.assertRaisesRegex(RuntimeError, 'busy'):
                    provider.get_access_token()
            session.assert_not_called()

    def test_rejected_same_token_never_forces_refresh_or_overwrites_cache(self):
        provider = self.provider()
        cache.set('wechat-stable-access-token:offline-app', 'old')
        transport = Mock()
        transport.post.return_value = Mock(status_code=200, json=lambda: {'access_token': 'old', 'expires_in': 7200})
        with patch('requests.Session', return_value=transport):
            with self.assertRaisesRegex(RuntimeError, '^WeChat token unavailable$'):
                provider.get_access_token(rejected_token='old')
        self.assertEqual(transport.post.call_count, 1)
        self.assertEqual(cache.get('wechat-stable-access-token:offline-app'), 'old')

    def test_failures_short_expiry_redirect_malformed_and_secret_exception_are_sanitized(self):
        provider = self.provider()
        transport = Mock()
        with patch('requests.Session', return_value=transport):
            for status, data in [(302, {}), (200, []), (200, {'errcode': 40125}),
                    (200, {'access_token': 'unsafe', 'expires_in': 30}),
                    (200, {'access_token': 'unsafe', 'expires_in': '7200'})]:
                transport.post.return_value = Mock(status_code=status, json=lambda: data)
                with self.assertRaisesRegex(RuntimeError, '^WeChat token unavailable$'):
                    provider.get_access_token()
                self.assertIsNone(cache.get('wechat-stable-access-token:offline-app'))
            transport.post.side_effect = RuntimeError('offline-secret credential-bearing URL')
            with self.assertRaisesRegex(RuntimeError, '^WeChat token unavailable$'):
                provider.get_access_token()

    def test_missing_credentials_runtime_and_app_mismatch_no_http(self):
        provider = self.provider()
        with patch('requests.Session') as session:
            for options in [dict(KOOK_TEST_SECRETS={'wechat_access_token': 'obsolete'}),
                    dict(KOOK_WECHAT_TOKEN_LOCK_DIR=''), dict(WECHAT_APP_ID='different')]:
                with override_settings(**options), self.assertRaises(RuntimeError):
                    provider.get_access_token()
        session.assert_not_called()

    @override_settings(KOOK_WECHAT_URL_LINK_ENABLED=True)
    def test_real_token_and_link_path_mock_only_http(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.kook_integration.navigation import wechat_url_link, SAFE_TARGET
        self.provider()
        transport = Mock()
        transport.post.side_effect = [
            Mock(status_code=200, json=lambda: {'access_token': 'fresh', 'expires_in': 7200}),
            Mock(status_code=200, json=lambda: {'errcode': 42001}),
            Mock(status_code=200, json=lambda: {'access_token': 'renewed', 'expires_in': 7200}),
            Mock(status_code=200, json=lambda: {'errcode': 0, 'url_link': 'https://wxaurl.cn/offline'})]
        with patch('requests.Session', return_value=transport):
            result = wechat_url_link(path=SAFE_TARGET, query='kook_intent='+'a'*32,
                                    expires_at=timezone.now()+timedelta(minutes=10))
        self.assertEqual(result, 'https://wxaurl.cn/offline')
        self.assertEqual(transport.post.call_count, 4)
        for call in transport.post.call_args_list:
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertNotIn(True, [call.kwargs['json'].get('force_refresh')])

    def test_rejected_token_reacquired_once_without_forced_invalidation(self):
        provider = self.provider()
        cache.set('wechat-stable-access-token:offline-app', 'old')
        transport = Mock()
        transport.post.return_value = Mock(status_code=200, json=lambda: {'access_token': 'new', 'expires_in': 7200})
        with patch('requests.Session', return_value=transport):
            self.assertEqual(provider.get_access_token(rejected_token='old'), 'new')
            self.assertEqual(provider.get_access_token(rejected_token='old'), 'new')
        self.assertEqual(transport.post.call_count, 1)
        self.assertIs(transport.post.call_args.kwargs['json']['force_refresh'], False)

    def test_stable_token_cached_with_early_expiry_without_forcing_other_consumers(self):
        provider = self.provider()
        transport = Mock()
        transport.post.return_value = Mock(status_code=200, json=lambda: {'access_token': 'renewable', 'expires_in': 7200})
        with patch('requests.Session', return_value=transport), patch.object(cache, 'set', wraps=cache.set) as save:
            self.assertEqual(provider.get_access_token(), 'renewable')
            self.assertEqual(provider.get_access_token(), 'renewable')
        transport.post.assert_called_once_with('https://api.weixin.qq.com/cgi-bin/stable_token',
            json={'grant_type': 'client_credential', 'appid': 'offline-app', 'secret': 'offline-secret', 'force_refresh': False},
            timeout=(3, 8), allow_redirects=False)
        self.assertFalse(transport.trust_env)
        save.assert_called_once_with('wechat-stable-access-token:offline-app', 'renewable', 6900)
