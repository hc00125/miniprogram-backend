from django import forms
from django.contrib import admin
from django.db import models
from django.utils.html import format_html, format_html_join

from .models import Addon, Package, PackageGroup, PackageImage, PackageSpec, PlayerType


IMAGE_HELP_TEXT = '填写完整图片 URL，例如：https://cdn.example.com/packages/cover.jpg。也可以填写后端可访问的 /media/... 或前端静态资源路径。'
JSON_IMAGE_HELP_TEXT = '填写 JSON 数组，例如：["https://cdn.example.com/detail-1.jpg", "https://cdn.example.com/detail-2.jpg"]。'


def package_image_url(image_obj):
    if not image_obj:
        return ''
    return image_obj.get_url()


def uploaded_image_urls(obj, image_type=None):
    if not obj:
        return []
    images = getattr(obj, 'active_images', None)
    if images is None:
        images = obj.images.filter(is_active=True).order_by('sort_order', 'id')
    urls = []
    for image in images:
        if image_type and image.image_type != image_type:
            continue
        url = package_image_url(image)
        if url:
            urls.append(url)
    return urls


def first_image_url(obj):
    uploaded_cover = uploaded_image_urls(obj, PackageImage.IMAGE_TYPE_COVER)
    if uploaded_cover:
        return uploaded_cover[0]
    uploaded_any = uploaded_image_urls(obj)
    if uploaded_any:
        return uploaded_any[0]
    if not obj:
        return ''
    return obj.cover_url or obj.image_url or obj.thumb_url or obj.picture_url or ''


def render_image(url, width=120, height=80):
    if not url:
        return '暂无图片'
    return format_html(
        '<a href="{url}" target="_blank" rel="noopener">'
        '<img src="{url}" style="max-width:{width}px; max-height:{height}px; object-fit:cover; '
        'border-radius:8px; border:1px solid #ddd; background:#f8f8f8;" />'
        '</a>',
        url=url,
        width=width,
        height=height,
    )


def render_image_list(urls):
    if not urls:
        return '暂无图片'
    if not isinstance(urls, list):
        return '格式不是数组，请填写 JSON 数组'
    valid_urls = [url for url in urls if isinstance(url, str) and url.strip()]
    if not valid_urls:
        return '暂无有效图片 URL'
    return format_html(
        '<div style="display:flex; gap:8px; flex-wrap:wrap;">{}</div>',
        format_html_join(
            '',
            '<a href="{0}" target="_blank" rel="noopener">'
            '<img src="{0}" style="width:96px; height:72px; object-fit:cover; '
            'border-radius:8px; border:1px solid #ddd; background:#f8f8f8;" />'
            '</a>',
            ((url,) for url in valid_urls),
        ),
    )


class PackageImageInline(admin.TabularInline):
    model = PackageImage
    extra = 1
    fields = ['preview', 'image_type', 'image', 'external_url', 'sort_order', 'is_active']
    readonly_fields = ['preview']
    ordering = ['image_type', 'sort_order', 'id']

    @admin.display(description='预览')
    def preview(self, obj):
        return render_image(package_image_url(obj), width=96, height=72)


class PackageSpecInline(admin.TabularInline):
    model = PackageSpec
    extra = 1
    fields = [
        'name', 'short_name', 'display_name', 'price', 'original_price',
        'description', 'guarantee_amount', 'sort_order', 'is_active',
    ]
    ordering = ['sort_order', 'id']
    show_change_link = True


@admin.action(description='批量上架/启用所选项目')
def mark_active(modeladmin, request, queryset):
    updated = queryset.update(is_active=True)
    modeladmin.message_user(request, f'已启用 {updated} 个项目。')


@admin.action(description='批量下架/禁用所选项目')
def mark_inactive(modeladmin, request, queryset):
    updated = queryset.update(is_active=False)
    modeladmin.message_user(request, f'已禁用 {updated} 个项目。')


@admin.register(PackageGroup)
class PackageGroupAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'sort_order', 'is_active', 'created_at']
    list_editable = ['sort_order', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name']
    ordering = ['sort_order', 'id']
    list_per_page = 30
    actions = [mark_active, mark_inactive]


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'cover_thumb', 'name', 'product_type', 'group', 'player_count',
        'base_price', 'spec_count', 'sort_order', 'is_active', 'is_custom',
        'sold_count', 'created_at',
    ]
    list_filter = ['group', 'product_type', 'is_active', 'is_custom']
    search_fields = ['name', 'description', 'rules_text', 'detail_text']
    list_editable = ['player_count', 'base_price', 'sort_order', 'is_active', 'is_custom']
    list_select_related = ['group']
    ordering = ['sort_order', 'id']
    list_per_page = 30
    inlines = [PackageImageInline, PackageSpecInline]
    readonly_fields = ['cover_preview', 'gallery_images_preview', 'detail_images_preview']
    actions = [mark_active, mark_inactive, 'duplicate_packages']
    formfield_overrides = {
        models.JSONField: {
            'widget': forms.Textarea(attrs={
                'rows': 5,
                'style': 'font-family: SFMono-Regular, Consolas, monospace; width: 90%;',
                'placeholder': '["https://cdn.example.com/image-1.jpg", "https://cdn.example.com/image-2.jpg"]',
            })
        },
        models.TextField: {
            'widget': forms.Textarea(attrs={'rows': 5, 'style': 'width: 90%;'})
        },
    }
    fieldsets = [
        ('基本信息', {
            'fields': ['name', 'product_type', 'group', 'player_count', 'base_price', 'original_price', 'description'],
            'description': '商品名称、分类、基础价格会直接影响前端展示和下单价格。保底单建议 product_type 选择“保底单”。',
        }),
        ('图片与详情（兼容旧 URL 字段）', {
            'fields': [
                'cover_preview', 'cover_url', 'image_url', 'thumb_url', 'picture_url',
                'gallery_images_preview', 'gallery_images',
                'detail_images_preview', 'detail_images',
                'detail_text', 'rules_text',
            ],
            'description': '新商品建议优先使用下方“商品图片”上传区。旧 URL 字段继续保留兼容：主图优先级 cover_url > image_url > thumb_url > picture_url；图文详情长图旧字段为 detail_images。',
        }),
        ('统计与排序', {
            'fields': ['sold_count', 'sort_order'],
            'description': 'sort_order 越小越靠前；sold_count 仅用于前端展示。',
        }),
        ('状态', {
            'fields': ['is_active', 'is_custom'],
            'description': 'is_active 关闭后，老板端商品接口不会展示该商品。',
        }),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            spec_count_value=models.Count('specs'),
        ).prefetch_related(
            models.Prefetch(
                'images',
                queryset=PackageImage.objects.filter(is_active=True).order_by('image_type', 'sort_order', 'id'),
                to_attr='active_images',
            )
        )

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        help_texts = {
            'cover_url': f'旧字段。商品列表图/商品主图。{IMAGE_HELP_TEXT}',
            'image_url': f'旧字段。备用商品图。{IMAGE_HELP_TEXT}',
            'thumb_url': f'旧字段。缩略图，当前前端会在 cover_url/image_url 为空时兜底使用。{IMAGE_HELP_TEXT}',
            'picture_url': f'旧字段。展示图，当前前端会在其他图片字段为空时兜底使用。{IMAGE_HELP_TEXT}',
            'gallery_images': f'旧字段。商品轮播图列表。新商品建议用下方“商品图片”上传轮播图。{JSON_IMAGE_HELP_TEXT}',
            'detail_images': f'旧字段。商品详情长图列表。新商品建议用下方“商品图片”上传详情长图。{JSON_IMAGE_HELP_TEXT}',
            'detail_text': '商品详情文字，没有详情长图时用于补充说明。',
            'rules_text': '规则、玩法、补偿说明、注意事项等。',
            'sort_order': '数字越小越靠前。',
            'is_active': '关闭后，老板端不会展示该商品。',
        }
        if formfield and db_field.name in help_texts:
            formfield.help_text = help_texts[db_field.name]
        return formfield

    @admin.display(description='封面')
    def cover_thumb(self, obj):
        return render_image(first_image_url(obj), width=72, height=54)

    @admin.display(description='主图预览')
    def cover_preview(self, obj):
        return render_image(first_image_url(obj), width=220, height=140)

    @admin.display(description='轮播图预览')
    def gallery_images_preview(self, obj):
        urls = uploaded_image_urls(obj, PackageImage.IMAGE_TYPE_GALLERY)
        if not urls and obj:
            urls = obj.gallery_images
        return render_image_list(urls)

    @admin.display(description='详情图预览')
    def detail_images_preview(self, obj):
        urls = uploaded_image_urls(obj, PackageImage.IMAGE_TYPE_DETAIL)
        if not urls and obj:
            urls = obj.detail_images
        return render_image_list(urls)

    @admin.display(description='规格数')
    def spec_count(self, obj):
        return getattr(obj, 'spec_count_value', obj.specs.count())

    @admin.action(description='复制所选商品（含规格和图片）')
    def duplicate_packages(self, request, queryset):
        created_count = 0
        for package in queryset.prefetch_related('specs', 'images'):
            copied = Package.objects.create(
                name=f'{package.name}（复制）',
                product_type=package.product_type,
                group=package.group,
                base_price=package.base_price,
                original_price=package.original_price,
                player_count=package.player_count,
                description=package.description,
                cover_url=package.cover_url,
                image_url=package.image_url,
                thumb_url=package.thumb_url,
                picture_url=package.picture_url,
                gallery_images=package.gallery_images,
                detail_images=package.detail_images,
                detail_text=package.detail_text,
                rules_text=package.rules_text,
                sold_count=0,
                sort_order=package.sort_order + 1,
                is_active=False,
                is_custom=package.is_custom,
            )
            for image in package.images.all():
                PackageImage.objects.create(
                    package=copied,
                    image_type=image.image_type,
                    image=image.image,
                    external_url=image.external_url,
                    sort_order=image.sort_order,
                    is_active=image.is_active,
                )
            for spec in package.specs.all():
                PackageSpec.objects.create(
                    package=copied,
                    name=spec.name,
                    short_name=spec.short_name,
                    display_name=spec.display_name,
                    price=spec.price,
                    original_price=spec.original_price,
                    description=spec.description,
                    guarantee_amount=spec.guarantee_amount,
                    sort_order=spec.sort_order,
                    is_active=spec.is_active,
                )
            created_count += 1
        self.message_user(request, f'已复制 {created_count} 个商品。复制出的商品默认下架，请检查后再上架。')


@admin.register(PackageImage)
class PackageImageAdmin(admin.ModelAdmin):
    list_display = ['id', 'preview', 'package', 'image_type', 'sort_order', 'is_active', 'created_at']
    list_filter = ['image_type', 'is_active', 'package']
    search_fields = ['package__name', 'external_url']
    list_editable = ['image_type', 'sort_order', 'is_active']
    list_select_related = ['package']
    ordering = ['package__sort_order', 'package_id', 'image_type', 'sort_order', 'id']
    list_per_page = 50
    actions = [mark_active, mark_inactive]

    @admin.display(description='预览')
    def preview(self, obj):
        return render_image(package_image_url(obj), width=96, height=72)


@admin.register(PackageSpec)
class PackageSpecAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'package', 'name', 'short_name', 'display_name', 'price',
        'original_price', 'guarantee_amount', 'sort_order', 'is_active', 'created_at',
    ]
    list_filter = ['package', 'is_active']
    search_fields = ['name', 'display_name', 'short_name', 'package__name']
    list_editable = ['price', 'original_price', 'guarantee_amount', 'sort_order', 'is_active']
    list_select_related = ['package']
    ordering = ['package__sort_order', 'sort_order', 'id']
    list_per_page = 50
    actions = [mark_active, mark_inactive]


@admin.register(Addon)
class AddonAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'price_per_player', 'priority', 'is_active', 'created_at']
    list_filter = ['is_active']
    search_fields = ['name']
    list_editable = ['price_per_player', 'priority', 'is_active']
    ordering = ['priority', 'id']
    list_per_page = 30
    actions = [mark_active, mark_inactive]


@admin.register(PlayerType)
class PlayerTypeAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'priority', 'can_view_addon_priority', 'price_extra', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name']
    list_editable = ['priority', 'can_view_addon_priority', 'price_extra', 'is_active']
    ordering = ['priority', 'id']
    list_per_page = 30
    actions = [mark_active, mark_inactive]
