from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.catalog.models import Package, PackageSpec
from apps.players.models import PlayerServiceListing


def normalized(value):
    return ''.join(str(value or '').strip().lower().split())


def money_key(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


class Command(BaseCommand):
    help = (
        '把旧的陪玩师专属商品精确匹配到已有公共共享规格并生成上架记录。'
        '默认仅预览；使用 --apply 才写入，使用 --offline-legacy 可同时下架已完整迁移的旧商品。'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='实际创建或更新上架记录')
        parser.add_argument('--offline-legacy', action='store_true', help='迁移成功后下架对应旧专属商品；必须与 --apply 同时使用')
        parser.add_argument('--approve', action='store_true', help='迁移记录直接标记为已审核上架；否则保持待审核')
        parser.add_argument('--player-id', type=int, help='只迁移指定陪玩师，便于分批验证')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        offline_legacy = options['offline_legacy']
        approve = options['approve']
        player_id = options.get('player_id')
        if offline_legacy and not apply_changes:
            self.stderr.write(self.style.ERROR('--offline-legacy 必须与 --apply 一起使用'))
            return

        shared_specs = (
            PackageSpec.objects
            .filter(
                is_active=True,
                package__is_active=True,
                package__selling_mode=Package.SELLING_MODE_PUBLIC,
                package__owner_player__isnull=True,
                package__player_count=1,
            )
            .select_related('package')
            .order_by('package_id', 'sort_order', 'id')
        )
        index = defaultdict(list)
        for spec in shared_specs:
            key = (
                normalized(spec.package.name),
                normalized(spec.name),
                money_key(spec.price),
            )
            index[key].append(spec)

        legacy_packages = (
            Package.objects
            .filter(
                selling_mode=Package.SELLING_MODE_PLAYER_DESIGNATED,
                owner_player__isnull=False,
            )
            .select_related('owner_player')
            .prefetch_related('specs')
            .order_by('owner_player_id', 'id')
        )
        if player_id:
            legacy_packages = legacy_packages.filter(owner_player_id=player_id)

        matched = 0
        created = 0
        unmatched = 0
        ambiguous = 0
        offlined = 0

        for legacy_package in legacy_packages:
            package_all_matched = True
            active_specs = [spec for spec in legacy_package.specs.all() if spec.is_active]
            if not active_specs:
                self.stdout.write(f'[跳过] 旧商品#{legacy_package.id} {legacy_package.name} 没有启用规格')
                unmatched += 1
                continue

            for legacy_spec in active_specs:
                key = (
                    normalized(legacy_package.name),
                    normalized(legacy_spec.name),
                    money_key(legacy_spec.price),
                )
                candidates = index.get(key, [])
                if not candidates:
                    package_all_matched = False
                    unmatched += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f'[未匹配] 玩家#{legacy_package.owner_player_id} {legacy_package.owner_player.name} / '
                            f'{legacy_package.name} / {legacy_spec.name} / ¥{legacy_spec.price}'
                        )
                    )
                    continue
                if len(candidates) > 1:
                    package_all_matched = False
                    ambiguous += 1
                    ids = ','.join(str(item.id) for item in candidates)
                    self.stdout.write(
                        self.style.WARNING(
                            f'[多重匹配] 旧规格#{legacy_spec.id} 对应共享规格 {ids}，未自动迁移'
                        )
                    )
                    continue

                shared_spec = candidates[0]
                matched += 1
                self.stdout.write(
                    f'[匹配] {legacy_package.owner_player.name}: '
                    f'旧规格#{legacy_spec.id} -> 共享规格#{shared_spec.id}'
                )
                if not apply_changes:
                    continue

                with transaction.atomic():
                    listing, was_created = PlayerServiceListing.objects.get_or_create(
                        player=legacy_package.owner_player,
                        spec=shared_spec,
                        defaults={
                            'status': (
                                PlayerServiceListing.STATUS_APPROVED
                                if approve else PlayerServiceListing.STATUS_PENDING
                            ),
                            'is_available': True,
                            'custom_description': legacy_package.description or '',
                            'sort_order': legacy_package.sort_order,
                        },
                    )
                    if was_created:
                        created += 1
                    elif approve and listing.status != PlayerServiceListing.STATUS_APPROVED:
                        listing.status = PlayerServiceListing.STATUS_APPROVED
                        listing.is_available = True
                        listing.rejection_reason = ''
                        listing.save(update_fields=['status', 'is_available', 'rejection_reason', 'updated_at'])

            if apply_changes and offline_legacy and package_all_matched:
                legacy_package.is_active = False
                legacy_package.save(update_fields=['is_active'])
                offlined += 1

        mode = '执行完成' if apply_changes else '预览完成（未写入）'
        self.stdout.write(self.style.SUCCESS(
            f'{mode}：匹配 {matched}，新建 {created}，未匹配 {unmatched}，多重匹配 {ambiguous}，下架旧商品 {offlined}'
        ))
        if not apply_changes:
            self.stdout.write('确认结果后可执行：python manage.py migrate_player_service_listings --apply --approve')
