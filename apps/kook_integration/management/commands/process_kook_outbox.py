import fcntl
import os
import time
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from apps.kook_integration.outbox import process_once
from apps.kook_integration.secrets import require_enabled, secret, KookError

class Command(BaseCommand):
    help = 'Single-host KOOK durable inbound/outbound worker; no implicit enablement.'

    def add_arguments(self,parser):
        mode=parser.add_mutually_exclusive_group(required=True)
        mode.add_argument('--once',action='store_true')
        mode.add_argument('--loop',action='store_true')
        parser.add_argument('--poll-seconds',type=float,default=2)
        parser.add_argument('--batch-size',type=int,default=50)

    def handle(self,*args,**options):
        try:
            require_enabled()
            secret('binding_pepper')
            if getattr(settings,'KOOK_SEND_ENABLED',False):
                secret('bot_token')
        except KookError as exc:
            raise CommandError(str(exc.detail))
        if not 1<=options['batch_size']<=500 or not 0.1<=options['poll_seconds']<=60:
            raise CommandError('Invalid worker bounds')
        path=getattr(settings,'KOOK_WORKER_LOCK_PATH','/run/lock/huc125-kook-worker.lock')
        try:
            descriptor=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        except OSError:
            raise CommandError('WORKER_LOCK_UNAVAILABLE')
        with os.fdopen(descriptor,'w') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise CommandError('KOOK_WORKER_ALREADY_RUNNING')
            while True:
                result=process_once(batch_size=options['batch_size'])
                self.stdout.write('inbound={inbound} outbound={outbound}'.format(**result))
                if options['once']:
                    break
                time.sleep(options['poll_seconds'])
