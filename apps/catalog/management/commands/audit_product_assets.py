from django.core.management.base import BaseCommand, CommandError

from apps.catalog.models import Package, PackageImage


class Command(BaseCommand):
    help = '检查上架商品的封面、详情、说明和价格配置；可选择自动下架不完整商品。'

    def add_arguments(self, parser):
        parser.add_argument('--deactivate-incomplete', action='store_true')
        parser.add_argument('--fail-on-warning', action='store_true')

    def handle(self, *args, **options):
        incomplete = []
        packages = Package.objects.filter(is_active=True).prefetch_related('images', 'specs')

        for package in packages:
            active_images = [image for image in package.images.all() if image.is_active]
            has_cover = bool(
                package.cover_url
                or package.image_url
                or package.thumb_url
                or package.picture_url
                or any(image.image_type == PackageImage.IMAGE_TYPE_COVER and image.get_url() for image in active_images)
            )
            has_detail = bool(
                package.detail_text
                or package.detail_images
                or any(image.image_type == PackageImage.IMAGE_TYPE_DETAIL and image.get_url() for image in active_images)
            )
            active_specs = [spec for spec in package.specs.all() if spec.is_active]
            has_price = bool(
                (active_specs and all(float(spec.price or 0) > 0 for spec in active_specs))
                or (not active_specs and float(package.base_price or 0) > 0)
            )
            issues = []
            if not has_cover:
                issues.append('缺少封面图')
            if not package.description:
                issues.append('缺少商品简介')
            if not has_detail:
                issues.append('缺少详情长图或详情文字')
            if not has_price:
                issues.append('存在0元或无有效价格')
            if issues:
                incomplete.append((package, issues))

        for package, issues in incomplete:
            self.stdout.write(self.style.WARNING(f'[{package.id}] {package.name}: ' + '；'.join(issues)))

        if options['deactivate_incomplete'] and incomplete:
            ids = [package.id for package, _issues in incomplete]
            Package.objects.filter(id__in=ids).update(is_active=False)
            self.stdout.write(self.style.WARNING(f'已自动下架 {len(ids)} 个配置不完整商品'))

        if not incomplete:
            self.stdout.write(self.style.SUCCESS(f'商品素材检查通过：{packages.count()} 个上架商品配置完整'))
            return

        message = f'发现 {len(incomplete)} 个上架商品配置不完整'
        if options['fail_on_warning']:
            raise CommandError(message)
        self.stdout.write(self.style.WARNING(message))
