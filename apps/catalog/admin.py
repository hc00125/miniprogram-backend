from collections import Counter
from decimal import Decimal
from itertools import combinations_with_replacement

from django import forms
from django.contrib import admin, messages
from django.db import models
from django.db import transaction
from django.utils.html import format_html, format_html_join

from .models import (
    Addon,
    CompositionSku,
    Package,
    PackageFamily,
    PackageGroup,
    PackageImage,
    PackageSpec,
    PlayerOffer,
    PlayerType,
)
from .composition import calculate_static_composition


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

SAFEBOX_SPEC_TEMPLATES = [
    ('基础版-不包损耗', '基础不包损耗', 288, '预计3-4天内完成，不包损耗', 1),
    ('基础版-包损耗', '基础包损耗', 308, '预计3-4天内完成，包损耗', 2),
    ('进阶版-包损耗', '进阶版', 398, '预计2-3天内完成，包损耗', 3),
    ('尊享版-包损耗', '尊享版', 488, '预计1-2天内完成，包损耗', 4),
    ('至尊版-24小时内完成', '至尊版', 568, '保24小时内完成，超时按规则补偿', 5),
]

ESCORT_PACKAGE_TEMPLATES = [
    ('四套四弹陪', 4, 35, '四套四弹陪玩套餐，适合轻量组队开局。', 10),
    ('五套四弹陪', 5, 40, '五套四弹陪玩套餐，适合稳定车队。', 20),
    ('五套五弹陪', 5, 45, '五套五弹陪玩套餐，适合高强度局。', 30),
    ('六套五弹陪', 6, 50, '六套五弹陪玩套餐，适合满配车队。', 40),
]


def package_image_url(image_obj):
    if not image_obj:
        return ''
    return image_obj.get_url()


def uploaded_image_urls(obj, image_type=None):
    if not obj or not getattr(obj, 'pk', None):
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
    if not obj or not getattr(obj, 'pk', None):
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


def package_publish_blockers(obj):
    blockers = []
    if not obj.name:
        blockers.append('缺少商品名称')
    if not cover_urls(obj):
        blockers.append('缺少封面图')
    if not detail_urls(obj) and not obj.detail_text:
        blockers.append('缺少详情图/详情文字')

    specs = active_specs(obj)
    active_zero_price_specs = [spec.name for spec in specs if spec.price is None or spec.price <= 0]
    if active_zero_price_specs:
        blockers.append(f'存在启用规格价格为0或负数：{", ".join(active_zero_price_specs[:3])}')

    if obj.product_type == Package.PRODUCT_TYPE_GUARANTEE:
        if not specs:
            blockers.append('保底单必须至少有一个启用规格')
    elif not specs and (obj.base_price is None or obj.base_price <= 0):
        blockers.append('普通商品未启用规格时，基础价必须大于0')

    return blockers


def package_config_score(obj):
    issues = package_config_issues(obj)
    if not obj:
        return 0
    checks = 5
    return max(0, round((checks - len(issues)) / checks * 100))


def create_package_if_missing(group, name, **kwargs):
    if group.packages.filter(name=name).exists():
        return None
    defaults = {
        'product_type': Package.PRODUCT_TYPE_NORMAL,
        'base_price': 0,
        'player_count': 1,
        'description': '',
        'detail_text': '',
        'rules_text': '',
        'sold_count': 0,
        'sort_order': 0,
        'is_active': False,
        'is_custom': False,
    }
    defaults.update(kwargs)
    return Package.objects.create(group=group, name=name, **defaults)


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
    if getattr(modeladmin, 'model', None) is Package:
        enabled_count = 0
        skipped = []
        for package in queryset:
            blockers = package_publish_blockers(package)
            if blockers:
                skipped.append(f'{package.name}：{"；".join(blockers)}')
                continue
            package.is_active = True
            package.save(update_fields=['is_active'])
            enabled_count += 1
        if enabled_count:
            modeladmin.message_user(request, f'已上架 {enabled_count} 个商品。')
        if skipped:
            modeladmin.message_user(request, '以下商品未上架：' + ' | '.join(skipped[:5]), level=messages.ERROR)
        return

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
    actions = [
        mark_active, mark_inactive,
        'create_safebox_template', 'create_guarantee_template', 'create_escort_templates',
    ]

    @admin.action(description='创建 3x3 赛季安全箱模板')
    def create_safebox_template(self, request, queryset):
        created_products = 0
        created_specs = 0
        skipped = 0
        for group in queryset:
            package = create_package_if_missing(
                group,
                '3x3赛季安全箱服务',
                product_type=Package.PRODUCT_TYPE_SPECIAL,
                base_price=288,
                player_count=1,
                description='新赛季3x3赛季安全箱服务，按版本规格下单。',
                detail_text='请上传详情长图，并确认每个版本的价格、时效和补偿规则。',
                rules_text='需要号上有1500W。至尊版超时一小时补偿30，最高至免单；不包含补亏损、官方任务bug时间以及停服维护。',
                sort_order=10,
                is_active=False,
            )
            if not package:
                skipped += 1
                continue
            created_products += 1
            for name, display_name, price, description, sort_order in SAFEBOX_SPEC_TEMPLATES:
                PackageSpec.objects.create(
                    package=package,
                    name=name,
                    short_name=display_name,
                    display_name=display_name,
                    price=price,
                    description=description,
                    sort_order=sort_order,
                    is_active=True,
                )
                created_specs += 1
        self.message_user(request, f'已创建 {created_products} 个3x3安全箱商品模板、{created_specs} 个规格，跳过 {skipped} 个已存在模板。模板默认下架，请上传图片后再上架。')

    @admin.action(description='创建电视台保底模板')
    def create_guarantee_template(self, request, queryset):
        created_products = 0
        created_specs = 0
        skipped = 0
        for group in queryset:
            package = create_package_if_missing(
                group,
                '电视台保底',
                product_type=Package.PRODUCT_TYPE_GUARANTEE,
                base_price=0,
                player_count=1,
                description='暗区突围端游电视台保底服务，按保底金额选择规格。',
                detail_text='请上传详情长图，并修改每个保底档位的实际价格后启用规格。',
                rules_text='下单后客服会按所选规格确认局数、规则和开局时间。',
                sort_order=20,
                is_active=False,
            )
            if not package:
                skipped += 1
                continue
            created_products += 1
            for name, display_name, amount, sort_order in GUARANTEE_SPEC_TEMPLATES:
                PackageSpec.objects.create(
                    package=package,
                    name=name,
                    short_name=display_name,
                    display_name=display_name,
                    price=0,
                    description='请填写实际价格后再启用该规格',
                    guarantee_amount=amount,
                    sort_order=sort_order,
                    is_active=False,
                )
                created_specs += 1
        self.message_user(request, f'已创建 {created_products} 个电视台保底模板、{created_specs} 个规格，跳过 {skipped} 个已存在模板。规格默认禁用且价格为0，请改价后启用。')

    @admin.action(description='创建陪玩套餐模板')
    def create_escort_templates(self, request, queryset):
        created_products = 0
        skipped = 0
        for group in queryset:
            for name, player_count, base_price, description, sort_order in ESCORT_PACKAGE_TEMPLATES:
                package = create_package_if_missing(
                    group,
                    name,
                    product_type=Package.PRODUCT_TYPE_NORMAL,
                    base_price=base_price,
                    player_count=player_count,
                    description=description,
                    detail_text='请上传套餐详情图，补充服务范围、接单说明和注意事项。',
                    rules_text='请根据实际业务补充退款、损耗、补偿和服务规则。',
                    sort_order=sort_order,
                    is_active=False,
                )
                if package:
                    created_products += 1
                else:
                    skipped += 1
        self.message_user(request, f'已创建 {created_products} 个陪玩套餐模板，跳过 {skipped} 个已存在模板。模板默认下架，请上传图片后再上架。')


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    form = PackageAdminForm
    list_display = [
        'id', 'cover_thumb', 'config_status', 'name', 'product_type', 'group',
        'package_family', 'player_count', 'base_price', 'active_spec_count', 'image_summary',
        'sort_order', 'is_active', 'is_custom', 'sold_count', 'created_at',
    ]
    list_filter = ['group', 'package_family', 'product_type', 'is_active', 'is_custom']
    search_fields = ['name', 'description', 'rules_text', 'detail_text']
    list_editable = ['player_count', 'base_price', 'sort_order', 'is_active', 'is_custom']
    list_select_related = ['group', 'package_family']
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
            'fields': ['name', 'product_type', 'group', 'package_family', 'player_count', 'base_price', 'original_price', 'description'],
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
            'description': 'is_active 关闭后，老板端商品接口不会展示该商品。配置不完整时即使勾选上架，后台也会自动拦截并改回下架。',
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

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        package = form.instance
        if package and package.is_active:
            blockers = package_publish_blockers(package)
            if blockers:
                Package.objects.filter(pk=package.pk).update(is_active=False)
                self.message_user(
                    request,
                    f'已阻止“{package.name}”上架：' + '；'.join(blockers),
                    level=messages.ERROR,
                )

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


@admin.register(PackageFamily)
class PackageFamilyAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'name', 'code', 'game_service', 'package_summary',
        'sort_order', 'is_active', 'updated_at',
    ]
    list_filter = ['is_active', 'game_service']
    search_fields = ['name', 'code', 'description']
    list_editable = ['sort_order', 'is_active']
    list_select_related = ['game_service']
    actions = ['generate_composition_skus']
    readonly_fields = ['created_at', 'updated_at', 'package_summary_detail']
    fieldsets = [
        ('装备商品族', {
            'fields': ['name', 'code', 'game_service', 'description'],
            'description': '同一装备配置的 1/2/3 人套餐必须归入同一个商品族。',
        }),
        ('套餐矩阵', {
            'fields': ['package_summary_detail'],
            'description': '请到“商品”中给每个 1/2/3 人套餐选择本商品族。',
        }),
        ('状态', {'fields': ['sort_order', 'is_active']}),
        ('记录信息', {'fields': ['created_at', 'updated_at']}),
    ]

    @admin.display(description='套餐矩阵')
    def package_summary(self, obj):
        matrix = obj.package_matrix(active_only=False)
        return ' / '.join(
            f'{count}人：{matrix[count].name}' if count in matrix else f'{count}人：缺失'
            for count in (1, 2, 3)
        )

    @admin.display(description='套餐矩阵详情')
    def package_summary_detail(self, obj):
        if not obj or not obj.pk:
            return '保存后可查看套餐矩阵。'
        matrix = obj.package_matrix(active_only=False)
        rows = []
        for count in (1, 2, 3):
            package = matrix.get(count)
            if not package:
                rows.append(f'<li>{count} 人套餐：<strong style="color:#dc2626;">缺失</strong></li>')
            else:
                state = '已上架' if package.is_active else '已下架'
                rows.append(f'<li>{count} 人套餐：{package.name}（{state}）</li>')
        return format_html('<ul>{}</ul>', format_html_join('', '{}', ((row,) for row in rows)))

    @admin.action(description='批量生成/更新非启用组合 SKU（不自动绑定支付）')
    def generate_composition_skus(self, request, queryset):
        """Build every valid static composition but never publish it automatically."""
        player_types = list(PlayerType.objects.filter(is_active=True).order_by('priority', 'id'))
        if not player_types:
            self.message_user(request, '没有启用的陪玩类型，无法生成组合 SKU。', level=messages.ERROR)
            return

        created = updated = unchanged = skipped_active = skipped_bound = 0
        missing = Counter()
        for family in queryset.select_related('game_service'):
            for base_type in player_types:
                eligible_types = [
                    player_type for player_type in player_types
                    if int(player_type.priority or 0) >= int(base_type.priority or 0)
                ]
                for required_players in (1, 2, 3):
                    for designated_count in range(required_players + 1):
                        for selection in combinations_with_replacement(eligible_types, designated_count):
                            counts = Counter(player_type.id for player_type in selection)
                            signature = [
                                {'player_type_id': player_type_id, 'count': count}
                                for player_type_id, count in sorted(counts.items())
                            ]
                            try:
                                quote = calculate_static_composition(
                                    package_family=family,
                                    base_player_type=base_type,
                                    required_players=required_players,
                                    designated_type_signature=signature,
                                    active_only=False,
                                )
                            except Exception as exc:  # 配置缺失时继续扫描其余组合，并汇总原因。
                                missing[str(exc)] += 1
                                continue

                            sku = CompositionSku.objects.filter(
                                composition_key=quote['composition_key'],
                            ).select_related('virtual_package_spec').first()
                            if sku and sku.is_active:
                                skipped_active += 1
                                continue

                            price = quote['total_price_per_hour']
                            if sku and sku.virtual_package_spec_id:
                                virtual_price = Decimal(str(sku.virtual_package_spec.price or 0)).quantize(Decimal('0.01'))
                                if virtual_price != price:
                                    skipped_bound += 1
                                    continue

                            if not sku:
                                try:
                                    with transaction.atomic():
                                        CompositionSku.objects.create(
                                            package_family=family,
                                            base_player_type=base_type,
                                            required_players=required_players,
                                            designated_type_signature=signature,
                                            total_price_per_hour=price,
                                            is_active=False,
                                        )
                                    created += 1
                                except Exception as exc:
                                    missing[str(exc)] += 1
                                continue

                            changed = (
                                sku.total_price_per_hour != price
                                or sku.designated_type_signature != signature
                                or sku.required_players != required_players
                                or sku.base_player_type_id != base_type.id
                                or sku.package_family_id != family.id
                            )
                            if not changed:
                                unchanged += 1
                                continue
                            try:
                                with transaction.atomic():
                                    sku.package_family = family
                                    sku.base_player_type = base_type
                                    sku.required_players = required_players
                                    sku.designated_type_signature = signature
                                    sku.total_price_per_hour = price
                                    sku.is_active = False
                                    sku.save()
                                updated += 1
                            except Exception as exc:
                                missing[str(exc)] += 1

        summary = (
            f'组合 SKU 批量生成完成：新建 {created}，更新 {updated}，'
            f'无需更新 {unchanged}，跳过已启用 {skipped_active}，'
            f'跳过已绑定但价格不匹配 {skipped_bound}。'
        )
        if missing:
            examples = '；'.join(
                f'{reason}（{count} 个）'
                for reason, count in missing.most_common(3)
            )
            summary += f' 缺失/异常配置 {sum(missing.values())} 个：{examples}'
            self.message_user(request, summary, level=messages.WARNING)
        else:
            self.message_user(request, summary, level=messages.SUCCESS)


@admin.register(PlayerOffer)
class PlayerOfferAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'player', 'player_type', 'package_family', 'is_active',
        'currently_available', 'sort_order', 'updated_at',
    ]
    list_filter = ['is_active', 'package_family', 'player__player_type']
    search_fields = ['player__name', 'package_family__name', 'package_family__code']
    list_editable = ['is_active', 'sort_order']
    list_select_related = ['player', 'player__player_type', 'package_family']
    ordering = ['package_family__sort_order', 'sort_order', 'id']

    @admin.display(description='陪玩类型')
    def player_type(self, obj):
        return obj.player.player_type

    @admin.display(description='当前可指定', boolean=True)
    def currently_available(self, obj):
        return obj in PlayerOffer.objects.available().filter(pk=obj.pk)


@admin.register(CompositionSku)
class CompositionSkuAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'composition_key', 'package_family', 'base_player_type',
        'required_players', 'signature_summary', 'total_price_per_hour',
        'virtual_package_spec', 'virtual_payment_ready', 'is_active', 'updated_at',
    ]
    list_filter = ['is_active', 'package_family', 'base_player_type']
    search_fields = ['composition_key', 'package_family__name', 'package_family__code']
    list_editable = ['is_active']
    list_select_related = [
        'package_family', 'base_player_type', 'virtual_package_spec',
        'virtual_package_spec__package',
    ]
    readonly_fields = ['composition_key', 'virtual_payment_status']
    fieldsets = [
        ('组合规则', {
            'fields': [
                'package_family', 'base_player_type', 'required_players',
                'designated_type_signature', 'total_price_per_hour', 'composition_key',
            ],
            'description': '价格由装备商品族中的单人指定规格与剩余公开名额规格静态相加得出。',
        }),
        ('虚拟支付绑定', {
            'fields': ['virtual_package_spec', 'virtual_payment_status'],
            'description': '内部规格价格必须等于组合价格；再到“虚拟支付商品绑定”配置微信道具。',
        }),
        ('状态', {'fields': ['is_active']}),
    ]

    @admin.display(description='指定类型')
    def signature_summary(self, obj):
        signature = obj.designated_type_signature or []
        return '，'.join(
            f'类型#{item.get("player_type_id")} × {item.get("count")}'
            for item in signature if isinstance(item, dict)
        ) or '无指定名额'

    @admin.display(description='微信虚拟支付')
    def virtual_payment_ready(self, obj):
        binding = obj.active_virtual_binding()
        if binding:
            return format_html('<span style="color:#14823b; font-weight:700;">已绑定 {}</span>', binding.product_id)
        return format_html('<span style="color:#dc2626; font-weight:700;">未绑定或价格不匹配</span>')

    @admin.display(description='微信虚拟支付状态')
    def virtual_payment_status(self, obj):
        if not obj or not obj.pk:
            return '保存后可检查虚拟支付绑定。'
        binding = obj.active_virtual_binding()
        if binding:
            return f'已绑定微信道具 {binding.product_id}，价格 {binding.goods_price_fen} 分。'
        return '未配置价格匹配的启用微信虚拟支付道具；该组合不能进入支付。'
