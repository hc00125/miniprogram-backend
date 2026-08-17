"""虚拟道具 ID 通用命名规则引擎。

规则格式：{类别代码}_{规格标识}[_{等级}]
- 类别代码从商品名推导（nvpei / tech_nvpei / guarantee / fun / special / cup / solo|duo|trio_44 ...）
- 规格标识从规格名/价格推导（弹数 / 价格 / 拼音）
- 等级后缀 _ent/_tech/_gold/_star（仅等级档商品）

设计目标：通用 + 一眼可读。已绑定商品的既有 ID 不被覆盖。
"""

import re
import unicodedata

# ---------- 类别代码 ----------

EQUIPMENT_PATTERNS = {
    '四套四弹': '44',
    '五套四弹': '54',
    '五套五弹': '55',
    '六套五弹': '65',
}

PLAYER_COUNT_PATTERNS = {
    '单人': 'solo',
    '双人': 'duo',
    '三人': 'trio',
}

TIER_SUFFIX = {
    '娱乐': 'ent',
    '技术': 'tech',
    '金牌': 'gold',
    '明星': 'star',
}

# 趣味单中文名 -> 拼音/英文标识（一眼可读）
FUN_NAME_MAP = {
    '摸金圣手': 'mojin',
    '黄金收集者': 'gold',
    '枪械收集者': 'gun',
    '大金天平': 'balance',
    '超级保险': 'super',
}

# 锦标赛品类 -> 拼音
CUP_TYPE_MAP = {
    '劝架': 'quanjia',
    '猛攻': 'menggong',
    '上分': 'shangfen',
}


def _cn_char_count(text):
    """统计中文字符数。"""
    return sum(1 for ch in str(text) if '\u4e00' <= ch <= '\u9fff')


def _strip_tier(name):
    """从规格名中剥离等级词，返回 (剩余名, 等级后缀或'')。"""
    for tier, suffix in TIER_SUFFIX.items():
        if tier in str(name):
            return str(name).replace(tier, ''), suffix
    return str(name), ''


def package_category_code(package_name):
    """从商品名推导类别代码。返回 (代码, 剩余名)。"""
    name = str(package_name or '')

    if '技术女陪' in name:
        return 'tech_nvpei', name.replace('技术女陪', '')
    if '女陪' in name:
        return 'nvpei', name.replace('女陪', '')
    if '护航' in name or '保底' in name:
        return 'guarantee', name
    if '趣味' in name:
        return 'fun', name
    if '特色' in name:
        return 'special', name
    if '锦标赛' in name or '杯赛' in name:
        return 'cup', name
    # 专属陪玩服务：用陪玩名拼音（chen2 -> chen2）
    if '专属陪玩' in name or '专属' in name:
        base = name.replace('专属陪玩服务', '').replace('专属', '').strip()
        return f'player_{base}', name

    # 装备单：solo_44 / duo_54 / trio_65；单数无人数前缀时默认 solo
    count_code = None
    for key, code in PLAYER_COUNT_PATTERNS.items():
        if key in name:
            count_code = code
            break
    for key, code in EQUIPMENT_PATTERNS.items():
        if key in name:
            if count_code:
                return f'{count_code}_{code}', name
            return f'solo_{code}', name
    return '', name


def _spec_identifier(spec_name, package_name, price):
    """推导规格标识（弹数 / 价格 / 拼音）。"""
    name = str(spec_name or '')
    pkg = str(package_name or '')

    # 1. 装备弹数（含"四套四弹"等，或规格名里直接出现）
    for key, code in EQUIPMENT_PATTERNS.items():
        if key in name or key in pkg:
            return code

    # 2. 锦标赛品类拼音
    for key, code in CUP_TYPE_MAP.items():
        if key in name:
            return code

    # 3. 趣味单中文名映射
    for key, code in FUN_NAME_MAP.items():
        if key in name:
            return code

    # 4. 数字开头/纯数字（88R / 298R / 58保888）
    digits = re.findall(r'\d+', name)
    if digits:
        return digits[0]

    # 5. 已有 ASCII 短标识（保险）
    ascii_only = re.sub(r'[^\x20-\x7e]', '', name).strip()
    if ascii_only and len(ascii_only) <= 12:
        return ascii_only.lower().replace(' ', '_')

    # 兜底：用商品价格
    if price:
        return str(int(price))

    return ''


def generate_virtual_item_id(package, spec):
    """根据商品 + 规格生成虚拟道具 ID。

    返回 (item_id, 名称, 价格元)。无法推导时 item_id 为空。
    """
    pkg_name = package.name or ''
    spec_name = spec.name or ''
    price = spec.price or package.base_price or 0

    category_code, _ = package_category_code(pkg_name)

    # 装备单：solo/duo/trio + 弹数 + 等级
    if category_code and re.match(r'^(solo|duo|trio)_\d+$', category_code):
        remainder, tier = _strip_tier(spec_name)
        item_id = category_code
        if tier:
            item_id = f'{item_id}_{tier}'
        return item_id, _display_name(spec_name, price), price

    if not category_code:
        # 兜底：商品名拼音/ASCII + 价格
        fallback = _ascii_slug(pkg_name)
        if not fallback:
            return '', _display_name(spec_name, price), price
        return f'{fallback}_{int(price)}', _display_name(spec_name, price), price

    # 非装备单
    remainder, tier = _strip_tier(spec_name)
    identifier = _spec_identifier(remainder, pkg_name, price)
    if not identifier:
        identifier = _spec_identifier(spec_name, pkg_name, price)

    item_id = f'{category_code}'
    if identifier and identifier != category_code:
        item_id = f'{category_code}_{identifier}'

    # 锦标赛按人数区分子项（cup_quanjia_solo / duo / trio）
    if category_code == 'cup':
        count_code = None
        for key, code in PLAYER_COUNT_PATTERNS.items():
            if key in remainder or key in spec_name:
                count_code = code
                break
        if count_code:
            item_id = f'{item_id}_{count_code}'

    if tier and tier not in item_id:
        item_id = f'{item_id}_{tier}'
    return item_id, _display_name(spec_name, price), price


def _ascii_slug(text):
    """提取商品名的 ASCII/字母数字段作为兜底标识（chen2 -> chen2，水枪单 -> 空）。"""
    slug = re.sub(r'[^\x20-\x7e]', '', str(text)).strip()
    slug = re.sub(r'[^a-zA-Z0-9_]+', '_', slug).strip('_')
    return slug.lower()


def _display_name(spec_name, price):
    """道具展示名：规格名截断到 10 个中文字符以内。"""
    name = str(spec_name or '')
    if _cn_char_count(name) > 10:
        cut = 0
        for i, ch in enumerate(name):
            cut = i + 1
            if _cn_char_count(name[:cut]) >= 10:
                break
        name = name[:cut]
    return name or f'{int(price)}元'
