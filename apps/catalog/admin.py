from django import forms
from django.contrib import admin
from django.db import models
from django.utils.html import format_html, format_html_join

from .models import Addon, Package, PackageGroup, PackageImage, PackageSpec, PlayerType


IMAGE_HELP_TEXT = '填写完整图片 URL，例如：https://cdn.example.com/packages/cover.jpg。也可以填写后端可访问的 /media/... 或前端静态资源路径。'
JSON_IMAGE_HELP_TEXT = '填写 JSON 数组，例如：["https://cdn.example.com/detail-1.jpg", "https://cdn.example.com/detail-2.jpg"]。'

GUARANTEE_SPEC_TEMPLATES = [
    ('电视台保底 888w', '888w档', '888w', 1),
    ('电视台保底 1088w', '1088w档', '1088w', 2),
    ('电视台保底 1288w', '1288w档', '1288w', 3),
    ('电视台保底 1488w', '1488w档', '1488w', 4),
    ('电视台保底 1688w', '1688w档', '1688w', 5),
    ('电视台保底 2688w', '2688w档', '2688w', 6),
    ('电视台保底 3988w', '3988w档', '3988w', 7),
    ('电视台保底 5888w', '5888w档', '5888w', 8),
    ('电视台保底 10001w', '10001w档', '10001w', 9),
]


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


def active_specs(obj):
    if not obj:
        return []
    specs = getattr(obj, 'active_specs', None)
    if specs is None:
        specs = obj.specs.filter(is_active=True).order_by('sort_order', 'id')
    return list(specs)


def legacy_image_urls(value):
    if not value or not isinstance(value, list):
        return []
    return [url for url in value if isinstance(url, str) and url.strip()]


def cover_urls(obj):
    urls = uploaded_image_urls(obj, PackageImage.IMAGE_TYPE_COVER)
    if urls:
        return urls
    if not obj:
        return []
    return [url for url in [obj.cover_url, obj.image_url, obj.thumb_url, obj.picture_url] if url]


def gallery_urls(obj):
    uploaded = uploaded_image_urls(obj, PackageImage.IMAGE_TYPE_GALLERY)
    legacy = legacy_image_urls(obj.gallery_images if obj else [])
    return uploaded + legacy


def detail_urls(obj):
    uploaded = uploaded_image_urls(obj, PackageImage.IMAGE_TYPE_DETAIL)
    legacy = legacy_image_urls(obj.detail_images if obj else [])
    return uploaded + legacy


def first_image_url(obj):
    uploaded_cover = cover_urls(obj)
    if uploaded_cover:
        return uploaded_cover[0]
    uploaded_any = uploaded_image_urls(obj)
    if uploaded_any:
        return uploaded_any[0]
    return ''


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


def package_config_issues(obj):
    if not obj:
        return ['保存后可查看配置完整度']
    issues = []
    if not obj.name:
        issues.append('缺少商品名称')
    if obj.base_price is None or obj.base_price < 0:
        issues.append('基础价异常')
    if not cover_urls(obj):
        issues.append('缺少封面图')
    if not detail_urls(obj) and not obj.detail_text:
        issues.append('缺少详情图/详情文字')
    if obj.product_type == Package.PRODUCT_TYPE_GUARANTEE and not active_specs(obj):
        issues.append('保底单缺少启用规格')
    return issues


def package_config_score(obj):
    issues = package_config_issues(obj)
    if not obj:
        return 0
    checks = 5
    return max(0, round((checks - len(issues)) / checks * 100))


class PackageAdminForm(forms.ModelForm):
    class Meta:
        model = Package
        fields = '__all__'

    def clean_base_price(self):
        value = self.cleaned_data.get('base_price')
        if value is not None and value < 0:
            raise forms.ValidationError('基础价不能小于 0。')
        return value

    def clean_original_price(self):
        value = self.cleaned_data.get('original_price')
        if value is not None and value < 0:
            raise forms.ValidationError('划线价不能小于 0。')
        return value

    def clean_sort_order(self):
        value = self.cleaned_data.get('sort_order')
        return value if value is not None else 0


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
    form = PackageAdminForm
    list_display = [
        'id', 'cover_thumb', 'config_status', 'name', 'product_type', 'group',
        'player_count', 'base_price', 'active_spec_count', 'image_summary',
        'sort_order', 'is_active', 'is_custom', 'sold_count', 'created_at',
    ]
    list_filter = ['group', 'product_type', 'is_active', 'is_custom']
    search_fields = ['name', 'description', 'rules_text', 'detail_text']
    list_editable = ['player_count', 'base_price', 'sort_order', 'is_active', 'is_custom']
    list_select_related = ['group']
    ordering = ['sort_order', 'id']
    list_per_page = 30
    inlines = [PackageImageInline, PackageSpecInline]
    readonly_fields = [
        'frontend_preview', 'cover_preview', 'gallery_images_preview',
        'detail_images_preview', 'config_status_detail',
    ]
    actions = [
        mark_active, mark_inactive, 'duplicate_packages',
        'generate_guarantee_specs', 'mark_as_guarantee_product',
    ]
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
        ('前端展示预览', {
            'fields': ['frontend_preview', 'config_status_detail'],
            'description': '保存商品后，这里会显示前端读取到的主图、价格、图片数量、规格数量和配置问题。',
        }),
        ('常用配置', {
            'fields': ['name', 'product_type', 'group', 'player_count', 'base_price', 'original_price', 'description'],
            'description': '日常上架主要填这里。保底单建议 product_type 选择“保底单”，然后用列表页操作“一键生成电视台保底九档规格”。',
        }),
        ('旧图片 URL 字段（兼容旧数据，通常不用填）', {
            'classes': ['collapse'],
            'fields': [
                'cover_preview', 'cover_url', 'image_url', 'thumb_url', 'picture_url',
                'gallery_images_preview', 'gallery_images',
                'detail_images_preview', 'detail_images',
            ],
            'description': '新商品建议优先使用下方“商品图片”上传区。这里是兼容旧数据用的 URL/JSON 字段。',
        }),
        ('详情文字与规则', {
            'fields': ['detail_text', 'rules_text'],
            'description': '没有详情长图时，detail_text 会作为文字说明；rules_text 用于玩法、补偿、注意事项等。',
        }),
        ('统计与排序', {
            'fields': ['sold_count', 'sort_order'],
            'description': 'sort_order 越小越靠前；sold_count 仅用于前端展示。',
        }),
        ('状态', {
            'fields': ['is_active', 'is_custom'],
            'description': 'is_active 关闭后，老板端商品接口不会展示该商品。建议先配好图片和规格，再上架。',
        }),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            spec_count_value=models.Count('specs', distinct=True),
            active_spec_count_value=models.Count('specs', filter=models.Q(specs__is_active=True), distinct=True),
            image_count_value=models.Count('images', distinct=True),
            active_image_count_value=models.Count('images', filter=models.Q(images__is_active=True), distinct=True),
        ).prefetch_related(
            models.Prefetch(
                'images',
                queryset=PackageImage.objects.filter(is_active=True).order_by('image_type', 'sort_order', 'id'),
                to_attr='active_images',
            ),
            models.Prefetch(
                'specs',
                queryset=PackageSpec.objects.filter(is_active=True).order_by('sort_order', 'id'),
                to_attr='active_specs',
            ),
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

    @admin.display(description='配置状态')
    def config_status(self, obj):
        issues = package_config_issues(obj)
        score = package_config_score(obj)
        if not issues:
            return format_html('<span style="color:#14823b; font-weight:700;">完整 {}%</span>', score)
        color = '#d97706' if score >= 60 else '#dc2626'
        return format_html('<span style="color:{}; font-weight:700;">{}% · {}项待补</span>', color, score, len(issues))

    @admin.display(description='前端配置状态')
    def config_status_detail(self, obj):
        issues = package_config_issues(obj)
        if not issues:
            return format_html('<div style="color:#14823b; font-weight:700;">配置完整，可以上架。</div>')
        return format_html(
            '<div style="padding:10px 12px; border-radius:8px; background:#fff7ed; color:#9a3412;">'
            '<strong>建议补充：</strong><ul style="margin:8px 0 0 18px;">{}</ul></div>',
            format_html_join('', '<li>{}</li>', ((issue,) for issue in issues)),
        )

    @admin.display(description='前端展示预览')
    def frontend_preview(self, obj):
        if not obj:
            return '保存商品后显示预览。'
        cover = first_image_url(obj)
        specs = active_specs(obj)
        price = min([spec.price for spec in specs], default=obj.base_price)
        return format_html(
            '<div style="display:flex; gap:14px; align-items:flex-start; padding:12px; border:1px solid #e5e7eb; border-radius:10px; background:#fafafa;">'
            '<div>{cover}</div>'
            '<div style="line-height:1.8;">'
            '<div><strong style="font-size:16px;">{name}</strong> <span style="color:#666;">{type}</span></div>'
            '<div>前端价格：<strong style="color:#dc2626;">¥{price}</strong></div>'
            '<div>启用规格：{spec_count} 个；封面图：{cover_count} 张；轮播图：{gallery_count} 张；详情图：{detail_count} 张</div>'
            '<div style="color:#666;">分类：{group}；排序：{sort_order}；状态：{status}</div>'
            '</div></div>',
            cover=render_image(cover, width=120, height=90),
            name=obj.name,
            type=obj.get_product_type_display(),
            price=price,
            spec_count=len(specs),
            cover_count=len(cover_urls(obj)),
            gallery_count=len(gallery_urls(obj)),
            detail_count=len(detail_urls(obj)),
            group=obj.group or '未分类',
            sort_order=obj.sort_order,
            status='已上架' if obj.is_active else '未上架',
        )

    @admin.display(description='主图预览')
    def cover_preview(self, obj):
        return render_image(first_image_url(obj), width=220, height=140)

    @admin.display(description='轮播图预览')
    def gallery_images_preview(self, obj):
        return render_image_list(gallery_urls(obj))

    @admin.display(description='详情图预览')
    def detail_images_preview(self, obj):
        return render_image_list(detail_urls(obj))

    @admin.display(description='启用规格')
    def active_spec_count(self, obj):
        count = getattr(obj, 'active_spec_count_value', len(active_specs(obj)))
        return count

    @admin.display(description='规格数')
    def spec_count(self, obj):
        return getattr(obj, 'spec_count_value', obj.specs.count())

    @admin.display(description='图片')
    def image_summary(self, obj):
        return format_html(
            '封面{} / 轮播{} / 详情{}',
            len(cover_urls(obj)), len(gallery_urls(obj)), len(detail_urls(obj)),
        )

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

    @admin.action(description='一键生成电视台保底九档规格（默认禁用，价格为0）')
    def generate_guarantee_specs(self, request, queryset):
        created_count = 0
        skipped_count = 0
        for package in queryset:
            existing_amounts = set(package.specs.exclude(guarantee_amount__isnull=True).values_list('guarantee_amount', flat=True))
            for name, display_name, amount, sort_order in GUARANTEE_SPEC_TEMPLATES:
                if amount in existing_amounts:
                    skipped_count += 1
                    continue
                PackageSpec.objects.create(
                    package=package,
                    name=name,
                    short_name=display_name,
                    display_name=display_name,
                    price=0,
                    original_price=None,
                    description='请填写实际价格后再启用该规格',
                    guarantee_amount=amount,
                    sort_order=sort_order,
                    is_active=False,
                )
                created_count += 1
        self.message_user(request, f'已生成 {created_count} 个保底规格，跳过 {skipped_count} 个已存在规格。新规格默认禁用、价格为0，请改价后启用。')

    @admin.action(description='标记为保底单并下架检查')
    def mark_as_guarantee_product(self, request, queryset):
        updated = queryset.update(product_type=Package.PRODUCT_TYPE_GUARANTEE, is_active=False)
        self.message_user(request, f'已将 {updated} 个商品标记为保底单并设为下架，请检查规格和图片后再上架。')


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
