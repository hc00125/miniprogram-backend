from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError, PermissionDenied
from apps.players.models import Player
from apps.gifts.models import Gift
from apps.gifts.services.configuration import initialize_configs


class Command(BaseCommand):
    help = '显式选择陪玩与礼物，仅初始化缺失25%关联；不会覆盖已有配置，不产生交易。'

    def add_arguments(self, parser):
        parser.add_argument('--actor-id', type=int, required=True)
        parser.add_argument('--player-ids', type=int, nargs='+', required=True)
        parser.add_argument('--gift-ids', type=int, nargs='+', required=True)
        parser.add_argument('--reason', required=True)

    def handle(self, *args, **options):
        try:
            actor = get_user_model().objects.get(pk=options['actor_id'])
            players = list(Player.objects.filter(pk__in=options['player_ids']))
            gifts = list(Gift.objects.filter(pk__in=options['gift_ids']))
            if len(players) != len(set(options['player_ids'])) or len(gifts) != len(set(options['gift_ids'])):
                raise CommandError('指定的陪玩或礼物不存在')
            if len(players) * len(gifts) > 10000:
                raise CommandError('单批最多10000个关联')
            created = initialize_configs(actor=actor, players=players, gifts=gifts, reason=options['reason'])
        except (get_user_model().DoesNotExist, ValidationError, PermissionDenied) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write('created=%s; existing associations unchanged' % created)
