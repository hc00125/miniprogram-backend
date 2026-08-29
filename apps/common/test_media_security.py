import base64
import hashlib
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import RequestFactory, TestCase, override_settings
from rest_framework.exceptions import ValidationError as DRFValidationError

from .content_security import MEDIA_TYPE_AUDIO, SCENE_PROFILE
from .media_security import ensure_media_publishable, ensure_tracked_media_reference
from .models import MediaContentSecurityCheck
from .wechat_callbacks import content_security_callback


TOKEN = 'content-security-test-token'
APP_ID = 'wx-test-appid'
AES_KEY_BYTES = b'0123456789abcdef0123456789abcdef'
AES_ENCODING_KEY = base64.b64encode(AES_KEY_BYTES).decode('ascii').rstrip('=')


def signature(*parts):
    return hashlib.sha1(''.join(sorted(str(part) for part in parts)).encode('utf-8')).hexdigest()


def media_callback_xml(trace_id, suggest='pass', label='100', errcode='0'):
    return f'''<xml>
<ToUserName><![CDATA[gh_test]]></ToUserName>
<FromUserName><![CDATA[system]]></FromUserName>
<CreateTime>1700000000</CreateTime>
<MsgType><![CDATA[event]]></MsgType>
<Event><![CDATA[wxa_media_check]]></Event>
<appid><![CDATA[{APP_ID}]]></appid>
<trace_id><![CDATA[{trace_id}]]></trace_id>
<version>2</version>
<detail><strategy><![CDATA[content_model]]></strategy><errcode>0</errcode><suggest><![CDATA[{suggest}]]></suggest><label>{label}</label><prob>90</prob></detail>
<errcode>{errcode}</errcode>
<errmsg><![CDATA[ok]]></errmsg>
<result><suggest><![CDATA[{suggest}]]></suggest><label>{label}</label></result>
</xml>'''.encode('utf-8')


def encrypt_wechat_message(message):
    plain = b'0123456789abcdef' + struct.pack('!I', len(message)) + message + APP_ID.encode('utf-8')
    pad_len = 32 - (len(plain) % 32)
    padded = plain + bytes([pad_len]) * pad_len
    encryptor = Cipher(algorithms.AES(AES_KEY_BYTES), modes.CBC(AES_KEY_BYTES[:16])).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode('ascii')


@override_settings(
    WECHAT_CONTENT_SECURITY_ENABLED=True,
    WECHAT_APP_ID=APP_ID,
    WECHAT_MESSAGE_TOKEN=TOKEN,
    WECHAT_MESSAGE_ENCODING_AES_KEY=AES_ENCODING_KEY,
)
class MediaSecurityCallbackTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username='media-security-user')

    def create_check(self, trace_id='trace-1'):
        return MediaContentSecurityCheck.objects.create(
            user=self.user,
            trace_id=trace_id,
            media_url=f'https://api.example.com/media/{trace_id}.mp3',
            media_type=MEDIA_TYPE_AUDIO,
            scene=SCENE_PROFILE,
        )

    def test_plaintext_callback_marks_media_passed(self):
        check = self.create_check('trace-pass')
        timestamp = '1700000001'
        nonce = 'nonce-plain'
        sig = signature(TOKEN, timestamp, nonce)
        request = self.factory.post(
            f'/api/wechat/content-security-callback?timestamp={timestamp}&nonce={nonce}&signature={sig}',
            data=media_callback_xml(check.trace_id),
            content_type='text/xml',
        )

        response = content_security_callback(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'success')
        check.refresh_from_db()
        self.assertEqual(check.status, MediaContentSecurityCheck.STATUS_PASS)
        self.assertEqual(check.suggest, 'pass')
        self.assertEqual(check.label, '100')

    def test_invalid_plaintext_signature_is_rejected(self):
        check = self.create_check('trace-invalid-signature')
        request = self.factory.post(
            '/api/wechat/content-security-callback?timestamp=1&nonce=2&signature=bad',
            data=media_callback_xml(check.trace_id),
            content_type='text/xml',
        )

        response = content_security_callback(request)
        self.assertEqual(response.status_code, 403)
        check.refresh_from_db()
        self.assertEqual(check.status, MediaContentSecurityCheck.STATUS_PENDING)

    def test_aes_callback_is_decrypted_and_applied(self):
        check = self.create_check('trace-aes')
        timestamp = '1700000002'
        nonce = 'nonce-aes'
        encrypted = encrypt_wechat_message(media_callback_xml(check.trace_id))
        msg_signature = signature(TOKEN, timestamp, nonce, encrypted)
        transport = f'<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>'.encode('utf-8')
        request = self.factory.post(
            f'/api/wechat/content-security-callback?timestamp={timestamp}&nonce={nonce}&encrypt_type=aes&msg_signature={msg_signature}',
            data=transport,
            content_type='text/xml',
        )

        response = content_security_callback(request)
        self.assertEqual(response.status_code, 200)
        check.refresh_from_db()
        self.assertEqual(check.status, MediaContentSecurityCheck.STATUS_PASS)

    def test_tracked_reference_allows_pending_but_publication_does_not(self):
        check = self.create_check('trace-pending')
        self.assertEqual(
            ensure_tracked_media_reference(user=self.user, media_url=check.media_url),
            check,
        )
        with self.assertRaises(DjangoValidationError):
            ensure_media_publishable(user=self.user, media_url=check.media_url)

        check.status = MediaContentSecurityCheck.STATUS_PASS
        check.save(update_fields=['status'])
        self.assertEqual(
            ensure_media_publishable(user=self.user, media_url=check.media_url),
            check,
        )

    def test_untracked_audio_url_is_rejected(self):
        with self.assertRaises(DRFValidationError):
            ensure_tracked_media_reference(
                user=self.user,
                media_url='https://attacker.example.com/unreviewed.mp3',
            )
