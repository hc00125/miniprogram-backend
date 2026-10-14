"""Real Django HTTP bridge + jsdom; no production data or native browser claims."""
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import skipUnless
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import close_old_connections
from django.test import Client, TransactionTestCase
from rest_framework_simplejwt.tokens import AccessToken
from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.dispatch.models import Customer, DispatchReceipt, HistoryClaim
from apps.orders.models import Order
from apps.wallet.models import ClientWalletLedger
from apps.payments.models import Payment


@skipUnless(os.environ.get('DISPATCH_JSDOM'), 'optional isolated jsdom dependency not supplied')
class ConsoleDOMTests(TransactionTestCase):
    def test_console_dom_posts_real_csrf_order_and_reviews_real_jwt_claim(self):
        staff=get_user_model().objects.create_user(username='dom-staff')
        staff.user_permissions.add(Permission.objects.get(content_type__app_label='dispatch',codename='use_console'))
        boss=get_user_model().objects.create_user(username='dom-boss')
        ClientProfile.objects.create(user=boss,openid='dom-fixture-only',nickname='DOM认领者')
        Package.objects.create(name='DOM小时商品',base_price=20,player_count=1)
        authenticated=Client();authenticated.force_login(staff)
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self): self.handle_django('GET')
            def do_POST(self): self.handle_django('POST')
            def handle_django(self,method):
                close_old_connections()
                client=Client(enforce_csrf_checks=True)
                client.cookies.load(self.headers.get('Cookie',''))
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                extra={'HTTP_HOST':'testserver'}
                for source,target in [('Authorization','HTTP_AUTHORIZATION'),('X-CSRFToken','HTTP_X_CSRFTOKEN')]:
                    if self.headers.get(source): extra[target]=self.headers[source]
                response=client.generic(method,self.path,data=body,content_type=self.headers.get('Content-Type','application/json'),**extra)
                requests.append((method,self.path,response.status_code))
                self.send_response(response.status_code)
                for key,value in response.items(): self.send_header(key,value)
                for cookie in response.cookies.values(): self.send_header('Set-Cookie',cookie.OutputString())
                self.end_headers();self.wfile.write(response.content)
                close_old_connections()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        cfg={'base':'http://127.0.0.1:'+str(server.server_port),'session':authenticated.cookies['sessionid'].value,'jwt':str(AccessToken.for_user(boss))}
        try:
            result=subprocess.run(['node',str(Path(__file__).with_name('console-dom.cjs'))],input=json.dumps(cfg),text=True,capture_output=True,timeout=90,env=dict(os.environ))
            self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr+'\n'+str(requests))
            self.assertIn('PASS: DOM + real HTTP',result.stdout)
            self.assertEqual(Order.objects.count(),1)
            self.assertEqual(DispatchReceipt.objects.count(),1)
            self.assertEqual(Order.objects.get().boss_user_id,boss.pk)
            self.assertEqual(Order.objects.get().status, Order.STATUS_CANCELLED)
            self.assertEqual(Order.objects.get().cancel_reason, 'DOM老板临时取消 <script>测试</script>')
            self.assertEqual(sum(1 for method,path,status in requests if method=='POST' and path.endswith('/cancel/')),1)
            self.assertEqual(Customer.objects.get().user_id,boss.pk)
            self.assertEqual(HistoryClaim.objects.get().status,'approved')
            self.assertEqual(ClientWalletLedger.objects.count(),0)
            self.assertEqual(Payment.objects.count(),0)
            self.assertEqual(sum(1 for method,path,status in requests if method=='POST' and path=='/dispatch/api/orders/'),1)
            self.assertFalse(any(status>=400 for _,_,status in requests),requests)
            print(result.stdout.strip())
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
