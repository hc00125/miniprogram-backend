import json
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from apps.kook_integration.client import KookClient
from apps.kook_integration.secrets import KookError

class Command(BaseCommand):
    help = 'Explicit read-only identity/target inspection; does not authorize sending.'
    def add_arguments(self,parser):
        parser.add_argument('--read-only',action='store_true',required=True)
    def handle(self,*args,**options):
        guild=getattr(settings,'KOOK_CANDIDATE_GUILD_ID','')
        channel=getattr(settings,'KOOK_CANDIDATE_CHANNEL_ID','')
        if not guild or not channel:
            raise CommandError('CANDIDATE_TARGET_MISSING')
        try:
            result=KookClient().inspect_target(guild,channel)
        except KookError as exc:
            raise CommandError(str(exc.detail))
        self.stdout.write(json.dumps(result,sort_keys=True))
        if not result['identity_ok'] or not result['channel_ok']:
            raise CommandError('TARGET_MISMATCH')
        self.stdout.write('Effective channel/mention permissions still require controlled verification; configuration unchanged.')
