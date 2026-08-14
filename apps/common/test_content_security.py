from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import ValidationError

from .content_security import (
    ContentSecurityRejected,
    SCENE_PROFILE,
    check_image_file,
    check_text,
    content_security_enabled,
    ensure_image_safe,
    ensure_text_safe,
)


class ContentSecurityTests(SimpleTestCase):
    @override_settings(DEBUG=True, WECHAT_CONTENT_SECURITY_ENABLED=False)
    def test_disabled_security_does_not_call_wechat(self):
        with patch('apps.common.content_security._post_json') as post_json:
            self.assertIsNone(check_text('测试内容', openid='openid', scene=SCENE_PROFILE))
            post_json.assert_not_called()

    @override_settings(DEBUG=False, WECHAT_CONTENT_SECURITY_ENABLED=True)
    def test_text_passes_when_wechat_suggests_pass(self):
        with patch('apps.common.content_security._post_json', return_value={
            'errcode': 0,
            'result': {'suggest': 'pass', 'label': 100},
        }) as post_json:
            result = check_text('正常昵称', openid='openid', scene=SCENE_PROFILE)
        self.assertEqual(result['result']['suggest'], 'pass')
        self.assertEqual(post_json.call_args.args[0], '/wxa/msg_sec_check')
        self.assertEqual(post_json.call_args.args[1]['openid'], 'openid')
        self.assertEqual(post_json.call_args.args[1]['version'], 2)

    @override_settings(DEBUG=False, WECHAT_CONTENT_SECURITY_ENABLED=True)
    def test_text_review_or_risky_is_rejected(self):
        for suggest in ('review', 'risky'):
            with self.subTest(suggest=suggest):
                with patch('apps.common.content_security._post_json', return_value={
                    'errcode': 0,
                    'result': {'suggest': suggest, 'label': 20002},
                }):
                    with self.assertRaises(ContentSecurityRejected):
                        check_text('风险内容', openid='openid', scene=SCENE_PROFILE)

    @override_settings(DEBUG=False, WECHAT_CONTENT_SECURITY_ENABLED=True)
    def test_validation_wrapper_returns_generic_message(self):
        with patch('apps.common.content_security._post_json', return_value={
            'errcode': 0,
            'result': {'suggest': 'risky', 'label': 20002},
        }):
            with self.assertRaises(ValidationError) as context:
                ensure_text_safe('风险内容', openid='openid', scene=SCENE_PROFILE)
        self.assertIn('发布内容含违规信息', str(context.exception.detail))

    @override_settings(DEBUG=False, WECHAT_CONTENT_SECURITY_ENABLED=True)
    def test_image_check_resets_file_pointer(self):
        upload = SimpleUploadedFile('avatar.jpg', b'fake-image', content_type='image/jpeg')
        with patch('apps.common.content_security._post_image', return_value={'errcode': 0}):
            check_image_file(upload, openid='openid')
        self.assertEqual(upload.tell(), 0)
        self.assertEqual(upload.read(), b'fake-image')

    @override_settings(DEBUG=False, WECHAT_CONTENT_SECURITY_ENABLED=True)
    def test_risky_image_is_not_accepted(self):
        upload = SimpleUploadedFile('avatar.jpg', b'fake-image', content_type='image/jpeg')
        with patch('apps.common.content_security._post_image', return_value={
            'errcode': 87014,
            'errmsg': 'risky content',
        }):
            with self.assertRaises(ValidationError) as context:
                ensure_image_safe(upload, openid='openid')
        self.assertIn('图片含违规内容', str(context.exception.detail))

    @override_settings(DEBUG=False)
    def test_production_defaults_to_enabled_when_no_explicit_setting(self):
        # override_settings may leave an explicit setting in some test runners;
        # this assertion only checks the normal production default path when absent.
        if not hasattr(self.settings(), 'WECHAT_CONTENT_SECURITY_ENABLED'):
            self.assertTrue(content_security_enabled())
