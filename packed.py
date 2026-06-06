import os
import sys
import json
import zipfile
import re
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional

PROJECT_DIR = Path(__file__).parent.resolve()
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output"
DEFAULT_OUTPUT_DIR.mkdir(exist_ok=True)

# 日志文件路径
LOG_FILE = DEFAULT_OUTPUT_DIR / ".latest.log"

PACK_FORMAT_MAP = {
    "4-1.13-1.14.4": 4,
    "5-1.15-1.16.1": 5,
    "6-1.16.2-1.16.5": 6,
    "7-1.17-1.17.1": 7,
    "8-1.18-1.18.1": 8,
    "9-1.18.2": 9,
    "10-1.19-1.19.3": 10,
    "12-1.19.4": 12,
    "15-1.20-1.20.1": 15,
    "18-1.20.2": 18,
    "26-1.20.3-1.20.4": 26,
    "41-1.20.5-1.20.6": 41,
    "48-1.21-1.21.1": 48,
    "57-1.21.2-1.21.3": 57,
    "61-1.21.4": 61,
    "71-1.21.5": 71,
    "80-1.21.6": 80,
    "81-1.21.7-1.21.8": 81,
    "88.0-1.21.9-1.21.10": 88.0,
    "94.1-1.21.11": 94.1,
    "101.1-26.1": 101.1
}

IGNORE_NAMES = {
    'packed.py',
    'output',
    '.git',
    '__pycache__',
    '.idea',
    'venv',
    'env',
    '.DS_Store',
    'blacklist.json',
    '.venv'
}


class Logger:
    """日志记录器，同时输出到控制台和文件"""

    def __init__(self, log_file: Path):
        self.log_file = log_file
        # 清空或创建日志文件
        self.log_file.write_text("", encoding='utf-8')
        self.start_time = datetime.now()

    def log(self, message: str):
        """记录日志消息"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_line = f"[{timestamp}] {message}"

        # 输出到控制台
        print(message)

        # 写入文件
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(log_line + "\n")

    def info(self, message: str):
        """记录信息级别日志"""
        self.log(message)

    def success(self, message: str):
        """记录成功级别日志"""
        self.log(f"✅ {message}")

    def warning(self, message: str):
        """记录警告级别日志"""
        self.log(f"⚠️  {message}")

    def error(self, message: str):
        """记录错误级别日志"""
        self.log(f"❌ {message}")

    def skip(self, message: str):
        """记录跳过日志"""
        self.log(f"⏭️  {message}")

    def get_elapsed_time(self) -> str:
        """获取已用时间"""
        elapsed = datetime.now() - self.start_time
        total_seconds = int(elapsed.total_seconds())
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}分{seconds}秒"


# 全局日志实例
logger: Optional[Logger] = None


def get_logger() -> Logger:
    """获取日志记录器实例，如果未初始化则抛出异常"""
    if logger is None:
        raise RuntimeError("Logger 未初始化！请先调用 init_logger()")
    return logger


def init_logger():
    """初始化日志记录器"""
    global logger
    logger = Logger(LOG_FILE)
    return logger


def get_version_order():
    """获取按版本号排序的版本列表"""

    def extract_prefix(version_key):
        """提取版本前缀用于排序"""
        # 从 "6-1.16.2-1.16.5" 提取前面的数字 6
        match = re.match(r'^(\d+)', version_key)
        if match:
            return int(match.group(1))
        return 0

    return sorted(PACK_FORMAT_MAP.keys(), key=extract_prefix)


def load_blacklist(version_dir: Path) -> dict:
    """加载版本目录中的 blacklist.json 文件

    返回格式:
    {
        "skip_namespaces": [],      # 从此版本开始跳过的命名空间
        "skip_recipes": [],         # 从此版本开始跳过的具体配方
        "allow_namespaces": [],     # 从此版本开始允许（取消跳过）的命名空间
        "allow_recipes": []         # 从此版本开始允许（取消跳过）的具体配方
    }
    """
    log = get_logger()
    blacklist_path = version_dir / "blacklist.json"

    if not blacklist_path.exists():
        return {
            "skip_namespaces": [],
            "skip_recipes": [],
            "allow_namespaces": [],
            "allow_recipes": []
        }

    try:
        with open(blacklist_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # 确保格式正确
        blacklist = {
            "skip_namespaces": data.get("skip_namespaces", []),
            "skip_recipes": data.get("skip_recipes", []),
            "allow_namespaces": data.get("allow_namespaces", []),
            "allow_recipes": data.get("allow_recipes", [])
        }

        skip_ns_count = len(blacklist['skip_namespaces'])
        skip_r_count = len(blacklist['skip_recipes'])
        allow_ns_count = len(blacklist['allow_namespaces'])
        allow_r_count = len(blacklist['allow_recipes'])

        if skip_ns_count + skip_r_count + allow_ns_count + allow_r_count > 0:
            log.info(f"   📋 已加载黑名单: 跳过({skip_ns_count}命名空间, {skip_r_count}配方), "
                        f"允许({allow_ns_count}命名空间, {allow_r_count}配方)")

        return blacklist

    except Exception as e:
        log.warning(f"   加载黑名单失败: {e}，将不使用黑名单")
        return {
            "skip_namespaces": [],
            "skip_recipes": [],
            "allow_namespaces": [],
            "allow_recipes": []
        }


def is_recipe_blacklisted(rel_path: Path, source_version: str, target_version: str, all_blacklists: dict) -> tuple:
    """检查配方是否应该被跳过

    Args:
        rel_path: 相对于版本目录的路径 (如 minecraft/anvil.json)
        source_version: 配方来源的版本标识
        target_version: 当前打包的目标版本标识
        all_blacklists: 所有版本的黑名单字典 {version_key: blacklist}

    Returns:
        (bool, str): (是否应该跳过, 跳过原因)
    """
    # 获取配方的相对路径字符串（不含 .json 扩展名）
    path_str = str(rel_path.with_suffix('')).replace('\\', '/')

    # 提取命名空间和配方名称
    parts = path_str.split('/')
    if len(parts) >= 2:
        namespace = parts[0]
        recipe_name = '/'.join(parts[1:])  # 支持子目录
        full_recipe_id = f"{namespace}:{recipe_name}"
    elif len(parts) == 1:
        # 直接在版本根目录下的文件
        namespace = "minecraft"  # 默认命名空间
        recipe_name = parts[0]
        full_recipe_id = f"{namespace}:{recipe_name}"
    else:
        return False, ""

    # 获取排序后的版本列表
    sorted_versions = get_version_order()
    if source_version not in sorted_versions or target_version not in sorted_versions:
        return False, ""

    current_idx = sorted_versions.index(target_version)

    # 追踪该配方的状态
    is_skipped = False
    skip_reason = ""

    # 从最低版本到目标版本，依次应用规则
    for version_key in sorted_versions[:current_idx + 1]:
        if version_key not in all_blacklists:
            continue

        blacklist = all_blacklists[version_key]

        # 检查是否应该跳过（skip 规则）
        if namespace in blacklist["skip_namespaces"]:
            is_skipped = True
            skip_reason = f"命名空间 '{namespace}' 在 {version_key} 被加入跳过列表"
        elif full_recipe_id in blacklist["skip_recipes"]:
            is_skipped = True
            skip_reason = f"配方 '{full_recipe_id}' 在 {version_key} 被加入跳过列表"

        # 检查是否应该允许（allow 规则会覆盖 skip）
        if namespace in blacklist["allow_namespaces"]:
            is_skipped = False
            skip_reason = ""
        elif full_recipe_id in blacklist["allow_recipes"]:
            is_skipped = False
            skip_reason = ""

    return is_skipped, skip_reason


def collect_recipe_files(mc_version: str) -> list:
    """收集指定版本及所有低版本的配方文件"""
    log = get_logger()
    sorted_versions = get_version_order()

    # 找到当前版本在排序中的位置
    if mc_version not in sorted_versions:
        log.warning(f"版本 {mc_version} 不在支持列表中")
        return []

    current_idx = sorted_versions.index(mc_version)

    # 调试:显示当前处理的版本和将要收集的范围
    versions_to_collect = sorted_versions[:current_idx + 1]
    log.info(f"   📊 当前版本: {mc_version} (索引 {current_idx})")
    log.info(f"   📊 将收集以下 {len(versions_to_collect)} 个版本: {', '.join(versions_to_collect)}")

    # 首先加载所有相关版本的黑名单
    all_blacklists = {}
    for version_key in sorted_versions[:current_idx + 1]:
        # 直接查找与 version_key 完全匹配的目录
        version_dir = PROJECT_DIR / version_key
        if version_dir.is_dir():
            all_blacklists[version_key] = load_blacklist(version_dir)
        else:
            all_blacklists[version_key] = {
                "skip_namespaces": [],
                "skip_recipes": [],
                "allow_namespaces": [],
                "allow_recipes": []
            }

    # 收集当前版本及所有低版本的文件
    collected_files = []
    skipped_count = 0

    for version_key in sorted_versions[:current_idx + 1]:
        # 直接查找与 version_key 完全匹配的目录
        version_dir = PROJECT_DIR / version_key

        if version_dir.is_dir():
            log.info(f"   扫描目录: {version_dir.name}")
            for root, dirs, files in os.walk(version_dir):
                dirs[:] = [d for d in dirs if d not in IGNORE_NAMES]
                for file in files:
                    filepath = Path(root) / file

                    # 跳过 blacklist.json 文件本身
                    if file == "blacklist.json":
                        continue

                    # 计算相对路径(相对于版本目录)
                    rel_path = filepath.relative_to(version_dir)

                    # 检查是否在黑名单中(传入目标版本 mc_version)
                    should_skip, skip_reason = is_recipe_blacklisted(rel_path, version_key, mc_version, all_blacklists)
                    if should_skip:
                        log.skip(f"   跳过: {rel_path.as_posix()} - {skip_reason}")
                        skipped_count += 1
                        continue

                    collected_files.append((filepath, rel_path, version_key))
        else:
            log.warning(f"   未找到版本目录: {version_key}")

    log.info(f"   共收集 {len(collected_files)} 个文件，跳过 {skipped_count} 个黑名单文件")
    return collected_files


def convert_recipe_for_1_21(data: dict, mc_version: str) -> dict:
    """将旧版本配方格式转换为 1.21+ 的新格式"""
    recipe_type = data.get('type', '')
    pack_format = PACK_FORMAT_MAP.get(mc_version, 4)

    # crafting_shaped 和 crafting_shapeless
    if recipe_type in ('minecraft:crafting_shaped', 'minecraft:crafting_shapeless'):
        # 1. result.item -> result.id (适用于所有 1.21+ 版本)
        if 'result' in data and isinstance(data['result'], dict):
            if 'item' in data['result']:
                data['result']['id'] = data['result'].pop('item')

        # 2. key 格式转换（仅 crafting_shaped）
        if recipe_type == 'minecraft:crafting_shaped' and 'key' in data:
            if pack_format >= 57:
                # 1.21.2+: key 值应该是字符串，如果是对象则转换
                data['key'] = convert_key_to_string_format(data['key'])
                # 同时处理标签格式（添加 # 前缀）
                data['key'] = convert_key_tags(data['key'])
            else:
                # 1.21.2 之前: key 值应该是对象格式 {"item": "..."}
                data['key'] = convert_key_to_object_format(data['key'])

        # 3. ingredients 格式转换（仅 crafting_shapeless）
        if recipe_type == 'minecraft:crafting_shapeless' and 'ingredients' in data:
            # 首先清理可能的嵌套数组
            data['ingredients'] = flatten_ingredients(data['ingredients'])

            if pack_format >= 57:
                # 1.21.2+: ingredients 元素应该是字符串
                data['ingredients'] = convert_ingredients_to_string_format(data['ingredients'])
                # 处理标签格式
                data['ingredients'] = convert_ingredients_tags(data['ingredients'])
            else:
                # 1.21.2 之前: ingredients 元素应该是对象格式
                data['ingredients'] = convert_ingredients_to_object_format(data['ingredients'])

    # campfire_cooking, blasting, smoking, smelting:
    # 1. result 字符串 -> result 对象 {id}
    # 2. ingredient 对象 -> ingredient 字符串 (1.21.2+)
    elif recipe_type in ('minecraft:campfire_cooking', 'minecraft:blasting', 'minecraft:smoking', 'minecraft:smelting'):
        # 处理 result 格式
        if 'result' in data:
            if isinstance(data['result'], str):
                data['result'] = {"id": data['result']}
            elif isinstance(data['result'], dict) and 'item' in data['result']:
                data['result']['id'] = data['result'].pop('item')

        # 处理 ingredient 格式 (1.21.2+ 使用字符串)
        if 'ingredient' in data:
            if pack_format >= 57:  # ✅ 正确：1.21.2+
                # 1.21.2+: ingredient 应该是字符串
                if isinstance(data['ingredient'], dict):
                    if 'item' in data['ingredient']:
                        data['ingredient'] = data['ingredient']['item']
                    elif 'tag' in data['ingredient']:
                        data['ingredient'] = f"#{data['ingredient']['tag']}"
            else:
                # 1.21.2 之前: ingredient 应该是对象
                if isinstance(data['ingredient'], str):
                    if data['ingredient'].startswith('#'):
                        data['ingredient'] = {"tag": data['ingredient'][1:]}
                    else:
                        data['ingredient'] = {"item": data['ingredient']}

    # stonecutting: 1.20.5+ (pack_format >= 41) result 从字符串变为对象
    elif recipe_type == 'minecraft:stonecutting':
        if pack_format >= 41:
            # 1.20.5+ 新格式：result 为对象 {id, count}
            if 'result' in data and isinstance(data['result'], str):
                # 提取外层的 count（如果存在）
                count = data.pop('count', 1)

                # 转换为新格式
                data['result'] = {
                    "id": data['result'],
                    "count": count
                }

    return data


def flatten_ingredients(ingredients: list) -> list:
    """展平可能嵌套的 ingredients 数组

    处理 [[{...}]] -> [{...}] 的情况
    """
    flattened = []
    for item in ingredients:
        if isinstance(item, list):
            # 如果是嵌套数组，展平它
            for sub_item in item:
                flattened.append(sub_item)
        else:
            # 否则直接添加
            flattened.append(item)
    return flattened


def convert_key_to_string_format(key_data: dict) -> dict:
    """将 key 从对象格式转换为字符串格式

    旧格式: {"#": {"item": "minecraft:copper_ingot"}}
    新格式: {"#": "minecraft:copper_ingot"}
    """
    converted = {}
    for char, value in key_data.items():
        if isinstance(value, dict) and 'item' in value:
            # 对象格式 -> 字符串格式
            converted[char] = value['item']
        elif isinstance(value, dict) and 'tag' in value:
            # 标签对象格式 -> 带 # 的字符串格式
            converted[char] = f"#{value['tag']}"
        else:
            # 已经是字符串或其他格式，保持不变
            converted[char] = value
    return converted


def convert_ingredients_to_string_format(ingredients: list) -> list:
    """将 ingredients 从对象格式转换为字符串格式

    旧格式: [{"item": "minecraft:acacia_planks"}]
    新格式: ["minecraft:acacia_planks"]
    """
    converted = []
    for item in ingredients:
        if isinstance(item, dict):
            if 'item' in item:
                # 物品对象格式 -> 字符串格式
                converted.append(item['item'])
            elif 'tag' in item:
                # 标签对象格式 -> 带 # 的字符串格式
                converted.append(f"#{item['tag']}")
            else:
                # 其他情况，保持原样
                converted.append(item)
        else:
            # 已经是字符串或其他格式，保持不变
            converted.append(item)
    return converted


def convert_ingredients_to_object_format(ingredients: list) -> list:
    """将 ingredients 从字符串格式转换为对象格式

    新格式: ["minecraft:acacia_planks"]
    旧格式: [{"item": "minecraft:acacia_planks"}]
    """
    converted = []
    for item in ingredients:
        if isinstance(item, str):
            if item.startswith('#'):
                # 标签字符串 -> 标签对象格式
                converted.append({"tag": item[1:]})  # 移除 # 前缀
            else:
                # 物品字符串 -> 物品对象格式
                converted.append({"item": item})
        elif isinstance(item, dict):
            # 已经是对象格式，保持不变
            converted.append(item)
        elif isinstance(item, list):
            # 如果已经是数组（可能是嵌套的），展平它
            for sub_item in item:
                if isinstance(sub_item, dict):
                    converted.append(sub_item)
                elif isinstance(sub_item, str):
                    if sub_item.startswith('#'):
                        converted.append({"tag": sub_item[1:]})
                    else:
                        converted.append({"item": sub_item})
        else:
            # 其他情况，保持原样
            converted.append(item)
    return converted

def convert_ingredients_tags(ingredients: list) -> list:
    """转换 ingredients 中的标签格式

    1.21.2+ 要求标签使用 # 前缀，如 #c:gems/diamond
    """
    converted = []
    for item in ingredients:
        if isinstance(item, str):
            if not item.startswith('#') and ':' in item:
                namespace, path = item.split(':', 1)
                # 判断是否是标签：如果路径中包含 / 或者命名空间是常见的标签命名空间
                if '/' in path or namespace in ['c', 'forge', 'fabric']:
                    item = f"#{item}"
        converted.append(item)
    return converted


def convert_key_to_object_format(key_data: dict) -> dict:
    """将 key 从字符串格式转换为对象格式

    新格式: {"#": "minecraft:copper_ingot"}
    旧格式: {"#": {"item": "minecraft:copper_ingot"}}
    """
    converted = {}
    for char, value in key_data.items():
        if isinstance(value, str):
            if value.startswith('#'):
                # 标签字符串 -> 标签对象格式
                converted[char] = {"tag": value[1:]}  # 移除 # 前缀
            else:
                # 物品字符串 -> 物品对象格式
                converted[char] = {"item": value}
        else:
            # 已经是对象或其他格式，保持不变
            converted[char] = value
    return converted


def convert_key_tags(key_data: dict) -> dict:
    """转换 key 中的标签格式

    1.21.2+ 要求标签使用 # 前缀，如 #c:gems/diamond
    旧格式可能是 c:gems/diamond 或 minecraft:diamond
    """
    converted = {}
    for char, value in key_data.items():
        if isinstance(value, str):
            # 如果值不包含 # 且不包含 .（说明不是具体的物品ID），则添加 # 前缀
            if not value.startswith('#') and ':' in value:
                namespace, path = value.split(':', 1)
                # 判断是否是标签：如果路径中包含 / 或者命名空间是常见的标签命名空间
                if '/' in path or namespace in ['c', 'forge', 'fabric']:
                    value = f"#{value}"
        converted[char] = value
    return converted

def convert_smithing_recipe(data: dict, mc_version: str) -> dict:
    """转换锻造台配方格式

    1.19.4 (pack_format >= 12) 以后，smithing 类型需要改为 smithing_transform
    """
    recipe_type = data.get('type', '')

    # 检查是否是旧的 smithing 类型
    if recipe_type == 'minecraft:smithing':
        pack_format = PACK_FORMAT_MAP.get(mc_version, 4)

        # 1.19.4 及以后版本使用 smithing_transform
        if pack_format >= 12:
            data['type'] = 'minecraft:smithing_transform'

    return data


def convert_pottery_shard_to_sherd(data):
    """将 1.20 之前的 pottery_shard 转换为 1.20+ 的 pottery_sherd"""

    # 递归处理 JSON 数据中的所有字符串值
    def replace_in_value(obj):
        if isinstance(obj, dict):
            return {k: replace_in_value(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [replace_in_value(item) for item in obj]
        elif isinstance(obj, str):
            # 替换所有 pottery_shard 为 pottery_sherd
            return obj.replace('pottery_shard', 'pottery_sherd')
        else:
            return obj

    return replace_in_value(data)


def get_modified_content(filepath: Path, pack_type: str, version: str, mc_version: str) -> bytes:
    log = get_logger()
    name = filepath.name
    try:
        if name in ('pack.mcmeta', 'fabric.mod.json'):
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if name == 'pack.mcmeta':
                pack_format = PACK_FORMAT_MAP.get(mc_version, 4)

                # 1.21.9+ (pack_format >= 88.0) 使用 min_format 和 max_format
                if pack_format >= 88.0:
                    # 移除旧的 pack_format
                    if 'pack' in data and 'pack_format' in data['pack']:
                        del data['pack']['pack_format']

                    # 设置 min_format 和 max_format 为相同值（精确匹配）
                    data.setdefault('pack', {})['min_format'] = pack_format
                    data.setdefault('pack', {})['max_format'] = pack_format
                else:
                    # 旧版本继续使用 pack_format
                    data.setdefault('pack', {})['pack_format'] = pack_format

                data.setdefault('pack', {})['description'] = f"Extra Recipe v{version}"
                if pack_type == 'datapack':
                    for key in ('fabric', 'quilt', 'forge', 'neoforge'):
                        data.pop(key, None)
            elif name == 'fabric.mod.json':
                data['version'] = version

            return json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')

        elif name == 'quilt.mod.json':
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # quilt.mod.json 的 version 在 quilt_loader.version
            if 'quilt_loader' in data:
                data['quilt_loader']['version'] = version

            return json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')



        elif name == 'neoforge.mods.toml':

            content = filepath.read_text(encoding='utf-8-sig')

            # 更新 version 字段

            content = re.sub(r'version\s*=\s*"([^"]*)"', f'version="{version}"', content, count=1)

            # 根据 Minecraft 版本选择 loaderVersion

            pack_format = PACK_FORMAT_MAP.get(mc_version, 4)

            if pack_format >= 94.1:

                # 1.21.11+ (pack_format >= 94.1): NeoForge 使用 loaderVersion [4,)

                content = re.sub(r'loaderVersion\s*=\s*"\[.*?\)"', r'loaderVersion="[4,)"', content, count=1)

            elif pack_format >= 41:

                # 1.20.5-1.21.10 (pack_format 41-88): NeoForge 使用 loaderVersion [2,)

                content = re.sub(r'loaderVersion\s*=\s*"\[.*?\)"', r'loaderVersion="[2,)"', content, count=1)

            else:

                # 更早版本（理论上不应该出现，因为 neoforge 只在 1.20.5+ 存在）

                content = re.sub(r'loaderVersion\s*=\s*"\[.*?\)"', r'loaderVersion="[2,)"', content, count=1)

            return content.encode('utf-8')


        elif name == 'mods.toml':

            content = filepath.read_text(encoding='utf-8')

            # 更新 version 字段 - 匹配 extrarecipe 模组的 version

            content = re.sub(r"(modId\s*=\s*'extrarecipe',\s*version\s*=\s*')([^']+)'", rf"\g<1>{version}'", content,

                             count=1)

            # 根据 Minecraft 版本选择 modLoader 和 loaderVersion

            pack_format = PACK_FORMAT_MAP.get(mc_version, 4)

            if pack_format == 4:
                # 版本4 (1.13-1.14.4): javafml, loaderVersion 最高 28
                content = re.sub(r'modLoader\s*=\s*".*?"', r'modLoader="javafml"', content, count=1)
                content = re.sub(r"loaderVersion\s*=\s*'\[.*?\)'", r"loaderVersion = '[28,)'", content, count=1)
            elif pack_format == 5:
                # 版本5 (1.15-1.16.1): javafml, loaderVersion 最高 31
                content = re.sub(r'modLoader\s*=\s*".*?"', r'modLoader="javafml"', content, count=1)
                content = re.sub(r"loaderVersion\s*=\s*'\[.*?\)'", r"loaderVersion = '[31,)'", content, count=1)
            elif pack_format < 8:
                # 版本6-7 (1.16.2-1.17.1): javafml, loaderVersion 支持 34
                content = re.sub(r'modLoader\s*=\s*".*?"', r'modLoader="javafml"', content, count=1)
                content = re.sub(r"loaderVersion\s*=\s*'\[.*?\)'", r"loaderVersion = '[34,)'", content, count=1)
            elif pack_format < 41:
                # 版本8-26 (1.18-1.20.4): lowcodefml, loaderVersion 支持 34
                content = re.sub(r'modLoader\s*=\s*".*?"', r'modLoader="lowcodefml"', content, count=1)
                content = re.sub(r"loaderVersion\s*=\s*'\[.*?\)'", r"loaderVersion = '[34,)'", content, count=1)
            else:
                # 版本41+ (1.20.5+): NeoForge 使用 javafml, loaderVersion [2,)
                content = re.sub(r'modLoader\s*=\s*".*?"', r'modLoader="javafml"', content, count=1)
                content = re.sub(r"loaderVersion\s*=\s*'\[.*?\)'", r"loaderVersion = '[2,)'", content, count=1)

            return content.encode('utf-8')
        return filepath.read_bytes()

    except Exception as e:
        log.warning(f"读取/修改 {filepath.name} 失败: {e}，将使用原始文件。")
        return filepath.read_bytes()


def should_include(filepath: Path, pack_type: str) -> bool:
    """判断文件是否应包含在当前包中"""
    rel = filepath.relative_to(PROJECT_DIR)

    if filepath.name in IGNORE_NAMES or filepath.parent.name in IGNORE_NAMES:
        return False
    if filepath.is_dir() and filepath.name.startswith('.') and filepath.name != 'META-INF':
        return False

    if pack_type == 'datapack':
        if filepath.name in ('fabric.mod.json', 'quilt.mod.json', 'mods.toml', 'neoforge.mods.toml'):
            return False
        if 'META-INF' in rel.parts:
            return False
        return True
    else:
        return True

def get_archive_path(filepath: Path, pack_type: str, mc_version: str) -> str:
    """计算文件在压缩包中的路径"""
    rel = filepath.relative_to(PROJECT_DIR)

    # 配置文件保持在根目录
    if filepath.name in ('pack.mcmeta', 'fabric.mod.json', 'quilt.mod.json', 'mods.toml', 'neoforge.mods.toml', 'MANIFEST.MF'):
        return rel.as_posix()

    parts = rel.parts

    # 检查是否在版本目录下（如 4-1.13-1.14.4/minecraft/...）
    if len(parts) >= 2:
        first_part = parts[0]
        # 匹配版本目录格式：数字-版本号（如 4-1.13-1.14.4）
        if re.match(r'^\d+-', first_part):
            remaining_parts = parts[1:]

            # 获取命名空间（通常是 minecraft）
            namespace = remaining_parts[0] if len(remaining_parts) > 0 else ''

            # 1.21+ (pack_format >= 48) 使用 recipe，之前版本使用 recipes
            pack_format = PACK_FORMAT_MAP.get(mc_version, 4)
            recipe_folder = 'recipe' if pack_format >= 48 else 'recipes'

            # 检查是否是 smithing 配方，1.19.4+ 需要改为 smithing_transform
            recipe_type_folder = remaining_parts[1] if len(remaining_parts) > 1 else ''
            if recipe_type_folder == 'smithing' and pack_format >= 12:
                recipe_type_folder = 'smithing_transform'
                # 重新构建路径
                new_remaining = [namespace, recipe_type_folder] + list(remaining_parts[2:])
            else:
                new_remaining = list(remaining_parts)

            new_path = Path(f'data/extrarecipe/{recipe_folder}') / '/'.join(new_remaining)
            return new_path.as_posix()

    return rel.as_posix()


def convert_recipe_file(filepath: Path, mc_version: str) -> bytes:
    """如果需要，转换配方文件格式"""
    log = get_logger()
    try:
        # 只处理 JSON 文件
        if not filepath.suffix == '.json':
            return filepath.read_bytes()

        # 读取并解析 JSON
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # 检查是否是配方文件
        if 'type' not in data:
            return filepath.read_bytes()

        # 转换锻造台配方格式（适用于所有版本）
        converted_data = convert_smithing_recipe(data, mc_version)

        # 检查是否需要转换格式（stonecutting: pack_format >= 41, 其他: pack_format >= 48）
        pack_format = PACK_FORMAT_MAP.get(mc_version, 4)
        if pack_format >= 41:
            converted_data = convert_recipe_for_1_21(converted_data, mc_version)

        return json.dumps(converted_data, indent=2, ensure_ascii=False).encode('utf-8')

    except Exception as e:
        log.warning(f"   转换配方 {filepath.name} 失败: {e}，使用原始文件")
        return filepath.read_bytes()

def convert_pottery_file(filepath: Path, mc_version: str) -> bytes:
    """如果需要，转换陶片文件格式（1.20+ 从 shard 改为 sherd）"""
    log = get_logger()
    try:
        # 检查目标版本是否为 1.20+ (pack_format >= 15)
        pack_format = PACK_FORMAT_MAP.get(mc_version, 4)
        if pack_format < 15:
            return filepath.read_bytes()

        # 只处理 JSON 文件
        if not filepath.suffix == '.json':
            return filepath.read_bytes()

        # 只处理包含 pottery_shard 的文件
        if 'pottery_shard' not in filepath.name:
            return filepath.read_bytes()

        # 读取并解析 JSON
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # 转换 pottery_shard 为 pottery_sherd
        converted_data = convert_pottery_shard_to_sherd(data)

        return json.dumps(converted_data, indent=2, ensure_ascii=False).encode('utf-8')

    except Exception as e:
        log.warning(f"   转换陶片文件 {filepath.name} 失败: {e}，使用原始文件")
        return filepath.read_bytes()


def build_datapack(version: str, mc_version: str, output_dir: Path):
    """构建 Datapack"""
    log = get_logger()
    # 提取 MC 版本前缀
    mc_prefix = mc_version.split('-')[0]

    # 在版本号后添加 MC 版本前缀
    version_with_prefix = f"{version}-{mc_prefix}"

    filename = f"[Datapack]Extra Recipe-{version_with_prefix}.zip"
    output_path = output_dir / filename

    log.info(f"📦 正在打包 Datapack: {filename} ...")
    log.info(f"   目标版本: {mc_version}")

    # 收集配方文件
    recipe_files = collect_recipe_files(mc_version)

    # 确定配方文件夹名称（1.21+ 使用 recipe，之前使用 recipes）
    pack_format = PACK_FORMAT_MAP.get(mc_version, 4)
    recipe_folder = 'recipe' if pack_format >= 48 else 'recipes'

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 添加配置文件（pack.mcmeta, pack.png, README.md, CHANGELOG.md）
        for config_file in ['LICENSE', 'pack.mcmeta', 'pack.png', 'README.md', 'CHANGELOG.md']:
            config_path = PROJECT_DIR / config_file
            if config_path.exists():
                if config_file == 'pack.mcmeta':
                    content = get_modified_content(config_path, 'datapack', version_with_prefix, mc_version)
                else:
                    content = config_path.read_bytes()
                zf.writestr(config_file, content)
                log.info(f"   添加配置文件: {config_file}")

        # 添加配方文件（高版本包含低版本）
        added_paths = set()
        for filepath, rel_path, source_version in recipe_files:
            # 双重保险：再次确认不是 blacklist.json
            if filepath.name == "blacklist.json":
                log.warning(f"   跳过黑名单文件: {filepath}")
                continue

            # 构建归档路径
            arcname = f"data/extrarecipe/{recipe_folder}/{rel_path.as_posix()}"

            # 1.19.4+ 将 smithing 文件夹改为 smithing_transform
            if pack_format >= 12:
                arcname = arcname.replace('/smithing/', '/smithing_transform/')

            # 避免重复添加
            if arcname not in added_paths:
                try:
                    # 根据目标版本转换配方格式
                    content = convert_recipe_file(filepath, mc_version)

                    # 如果是陶片文件且目标是 1.20+，还需要转换 shard -> sherd
                    is_pottery_shard = 'pottery_shard' in filepath.name
                    if is_pottery_shard:
                        content = convert_pottery_file(filepath, mc_version)
                        # 修改文件名：将 pottery_shard 改为 pottery_sherd
                        pack_format = PACK_FORMAT_MAP.get(mc_version, 4)
                        if pack_format >= 15:
                            arcname = arcname.replace('pottery_shard', 'pottery_sherd')

                    zf.writestr(arcname, content)
                    added_paths.add(arcname)
                except Exception as e:
                    log.warning(f"   添加失败 {arcname}: {e}")

        log.info(f"   ✅ 已添加 {len(added_paths)} 个配方文件")

    log.success(f"All Loader 打包成功: {output_path}")


def build_all_loader(version: str, mc_version: str, output_dir: Path):
    """构建 All Loader 模组包"""
    log = get_logger()
    # 提取 MC 版本前缀
    mc_prefix = mc_version.split('-')[0]

    # 在版本号后添加 MC 版本前缀
    version_with_prefix = f"{version}-{mc_prefix}"

    filename = f"[All Loader]Extra Recipe-{version_with_prefix}.jar"
    output_path = output_dir / filename

    log.info(f"📦 正在打包 All Loader: {filename} ...")
    log.info(f"   目标版本: {mc_version}")

    # 确定配方文件夹名称（1.21+ 使用 recipe，之前使用 recipes）
    pack_format = PACK_FORMAT_MAP.get(mc_version, 4)
    recipe_folder = 'recipe' if pack_format >= 48 else 'recipes'

    # 判断是否需要包含 class 文件（1.17及以前需要，1.18及以后不需要）
    include_class_files = pack_format < 8

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 添加所有配置文件和模组文件（不包括版本目录）
        for root, dirs, files in os.walk(PROJECT_DIR):
            # 计算相对路径
            rel_root = Path(root).relative_to(PROJECT_DIR)

            # 在过滤 dirs 之前，先移除所有版本目录
            # 这样可以阻止 os.walk 进入这些目录
            dirs[:] = [
                d for d in dirs
                if d not in IGNORE_NAMES and not re.match(r'^\d+(\.\d+)*-', d)
            ]

            for file in files:
                filepath = Path(root) / file

                # 如果不包含 class 文件，跳过 .class 文件
                if not include_class_files and filepath.suffix == '.class':
                    continue

                if not should_include(filepath, 'all_loader'):
                    continue

                arcname = get_archive_path(filepath, 'all_loader', mc_version)
                content = get_modified_content(filepath, 'all_loader', version_with_prefix, mc_version)
                zf.writestr(arcname, content)

        # 收集并添加配方文件（高版本包含低版本）
        recipe_files = collect_recipe_files(mc_version)

        added_paths = set()
        for filepath, rel_path, source_version in recipe_files:
            # 双重保险：再次确认不是 blacklist.json
            if filepath.name == "blacklist.json":
                log.warning(f"   跳过黑名单文件: {filepath}")
                continue

            # 构建归档路径
            arcname = f"data/extrarecipe/{recipe_folder}/{rel_path.as_posix()}"

            # 1.19.4+ 将 smithing 文件夹改为 smithing_transform
            if pack_format >= 12:
                arcname = arcname.replace('/smithing/', '/smithing_transform/')

            # 避免重复添加
            if arcname not in added_paths:
                try:
                    # 根据目标版本转换配方格式
                    content = convert_recipe_file(filepath, mc_version)

                    # 如果是陶片文件且目标是 1.20+，还需要转换 shard -> sherd
                    is_pottery_shard = 'pottery_shard' in filepath.name
                    if is_pottery_shard:
                        content = convert_pottery_file(filepath, mc_version)
                        # 修改文件名：将 pottery_shard 改为 pottery_sherd
                        pack_format = PACK_FORMAT_MAP.get(mc_version, 4)
                        if pack_format >= 15:
                            arcname = arcname.replace('pottery_shard', 'pottery_sherd')

                    zf.writestr(arcname, content)
                    added_paths.add(arcname)
                except Exception as e:
                    log.warning(f"   添加失败 {arcname}: {e}")

        log.info(f"   ✅ 已添加 {len(added_paths)} 个配方文件")

    log.success(f"All Loader 打包成功: {output_path}")

def list_versions():
    """列出所有支持的 Minecraft 版本"""
    log = get_logger()
    log.info("\n📋 支持的 Minecraft 版本:")
    log.info("-" * 50)

    sorted_versions = get_version_order()

    for version_key in sorted_versions:
        # 提取前缀数字
        prefix = version_key.split('-')[0]
        log.info(f"  {prefix}: {version_key}")
    log.info("-" * 50)


def interactive_mode():
    """交互式配置模式"""
    log = get_logger()
    log.info("=" * 60)
    log.info("  Extra Recipe 打包工具 - 交互式配置")
    log.info("=" * 60)
    log.info("")

    # 1. 输入版本号
    while True:
        version = input("请输入模组版本号 (例如: 4.0.0): ").strip()
        if version:
            break
        log.error("版本号不能为空，请重新输入\n")

    # 2. 选择 Minecraft 版本
    log.info("\n请选择 Minecraft 版本:")
    list_versions()
    log.info("  all: 一次性打包所有版本")

    mc_version = "all"  # 设置默认值
    batch_mode = False
    while True:
        mc_choice = input(f"\n请输入版本前缀、all 或直接输入完整版本标识 (默认: {mc_version}): ").strip()

        if not mc_choice:
            batch_mode = True
            break

        # 检查是否为批量模式
        if mc_choice.lower() == 'all':
            batch_mode = True
            break

        # 尝试作为前缀解析
        try:
            prefix_num = int(mc_choice)
            sorted_versions = get_version_order()

            # 查找匹配前缀的版本
            found = False
            for v in sorted_versions:
                if v.startswith(f"{prefix_num}-"):
                    mc_version = v
                    found = True
                    break

            if found:
                break
            else:
                log.error(f"未找到前缀为 {prefix_num} 的版本，请重新输入")
                continue
        except ValueError:
            # 直接输入完整版本标识
            if mc_choice in PACK_FORMAT_MAP:
                mc_version = mc_choice
                break
            else:
                log.error("无效的版本标识，请重新输入")
                continue

    # 3. 选择打包类型
    log.info("\n请选择打包类型:")
    log.info("  1. 仅打包 Datapack (.zip)")
    log.info("  2. 仅打包 All Loader (.jar)")
    log.info("  3. 两者都打包 (默认)")

    pack_types = ['both']  # 设置默认值
    while True:
        pack_choice = input("\n请输入选项编号 (1/2/3, 默认: 3): ").strip()

        if not pack_choice or pack_choice == '3':
            pack_types = ['both']
            break
        elif pack_choice == '1':
            pack_types = ['datapack']
            break
        elif pack_choice == '2':
            pack_types = ['all_loader']
            break
        else:
            log.error("无效的选项，请输入 1、2 或 3")
            continue

    # 4. 选择输出目录
    log.info(f"\n当前输出目录: {DEFAULT_OUTPUT_DIR}")
    custom_dir = input("是否自定义输出目录？(y/n, 默认: n): ").strip().lower()

    if custom_dir == 'y':
        while True:
            output_dir_input = input("请输入输出目录路径: ").strip()
            if output_dir_input:
                output_dir = Path(output_dir_input)
                try:
                    output_dir.mkdir(parents=True, exist_ok=True)
                    break
                except Exception as e:
                    log.error(f"无法创建目录: {e}，请重新输入")
            else:
                output_dir = DEFAULT_OUTPUT_DIR
                break
    else:
        output_dir = DEFAULT_OUTPUT_DIR
        output_dir.mkdir(exist_ok=True)

    # 5. 确认配置
    log.info("\n" + "=" * 60)
    log.info("  配置确认")
    log.info("=" * 60)
    log.info(f"  模组版本:   {version}")
    if batch_mode:
        log.info(f"  MC 版本:    全部版本 ({len(PACK_FORMAT_MAP)} 个)")
    else:
        log.info(f"  MC 版本:    {mc_version}")
    log.info(f"  打包类型:   {', '.join(pack_types) if pack_types != ['both'] else 'Datapack + All Loader'}")
    log.info(f"  输出目录:   {output_dir}")
    log.info("=" * 60)

    confirm = input("\n确认开始打包？(y/n, 默认: y): ").strip().lower()
    if confirm == 'n':
        log.error("已取消打包")
        return

    # 6. 开始打包
    log.info("")

    if batch_mode:
        # 批量打包所有版本
        sorted_versions = get_version_order()
        log.info(f"🚀 开始批量打包 Extra Recipe v{version}")
        log.info(f"📁 输出目录: {output_dir}")
        log.info(f"📋 共 {len(sorted_versions)} 个版本需要打包")
        log.info("-" * 50)

        success_count = 0
        fail_count = 0

        for idx, mc_ver in enumerate(sorted_versions, 1):
            log.info(f"\n[{idx}/{len(sorted_versions)}] 正在处理: {mc_ver}")
            try:
                if 'both' in pack_types or 'datapack' in pack_types:
                    build_datapack(version, mc_ver, output_dir)

                if 'both' in pack_types or 'all_loader' in pack_types:
                    build_all_loader(version, mc_ver, output_dir)

                success_count += 1
            except Exception as e:
                log.error(f"   打包失败: {e}")
                fail_count += 1

        log.info("\n" + "=" * 50)
        log.info(f"🎉 批量打包完成！")
        log.info(f"   成功: {success_count} 个版本")
        log.info(f"   失败: {fail_count} 个版本")
        log.info(f"   耗时: {log.get_elapsed_time()}")
        log.info(f"📁 产物已保存至: {output_dir}")
        log.info(f"📄 日志已保存至: {LOG_FILE}")
    else:
        # 单个版本打包
        log.info(f"🚀 开始打包 Extra Recipe v{version} (目标 MC: {mc_version})")
        log.info(f"📁 输出目录: {output_dir}")
        log.info("-" * 50)

        if 'both' in pack_types or 'datapack' in pack_types:
            build_datapack(version, mc_version, output_dir)

        if 'both' in pack_types or 'all_loader' in pack_types:
            build_all_loader(version, mc_version, output_dir)

        log.info("-" * 50)
        log.info("🎉 全部打包完成！产物已保存至 output/ 目录。")
        log.info(f"📄 日志已保存至: {LOG_FILE}")


def cli_mode(args):
    """命令行模式"""
    log = get_logger()
    if args.list:
        list_versions()
        return

    if not args.version:
        log.error("错误: 缺少必需的版本号参数\n")
        parser.print_help()
        sys.exit(1)

    output_dir = args.output_dir
    output_dir.mkdir(exist_ok=True)

    version = args.version
    mc_version = args.mc_version

    # 检查是否为批量模式
    if mc_version.lower() == 'all':
        sorted_versions = get_version_order()
        log.info(f"🚀 开始批量打包 Extra Recipe v{version}")
        log.info(f"📁 输出目录: {output_dir}")
        log.info(f"📋 共 {len(sorted_versions)} 个版本需要打包")
        log.info("-" * 50)

        success_count = 0
        fail_count = 0

        for idx, mc_ver in enumerate(sorted_versions, 1):
            log.info(f"\n[{idx}/{len(sorted_versions)}] 正在处理: {mc_ver}")
            try:
                if args.only == 'datapack' or args.only is None:
                    build_datapack(version, mc_ver, output_dir)

                if args.only == 'all_loader' or args.only is None:
                    build_all_loader(version, mc_ver, output_dir)

                success_count += 1
            except Exception as e:
                log.error(f"   打包失败: {e}")
                fail_count += 1

        log.info("\n" + "=" * 50)
        log.info(f"🎉 批量打包完成！")
        log.info(f"   成功: {success_count} 个版本")
        log.info(f"   失败: {fail_count} 个版本")
        log.info(f"   耗时: {log.get_elapsed_time()}")
        log.info(f"📁 产物已保存至: {output_dir}")
        log.info(f"📄 日志已保存至: {LOG_FILE}")
    else:
        # 验证版本标识是否有效
        if mc_version not in PACK_FORMAT_MAP:
            log.error(f"错误: 无效的版本标识 '{mc_version}'")
            log.info("请使用 --list 查看支持的版本，或使用 'all' 打包所有版本")
            sys.exit(1)

        # 单个版本打包
        log.info(f"🚀 开始打包 Extra Recipe v{version} (目标 MC: {mc_version})")
        log.info(f"📁 输出目录: {output_dir}")
        log.info("-" * 50)

        if args.only == 'datapack' or args.only is None:
            build_datapack(version, mc_version, output_dir)

        if args.only == 'all_loader' or args.only is None:
            build_all_loader(version, mc_version, output_dir)

        log.info("-" * 50)
        log.info("🎉 全部打包完成！产物已保存至 output/ 目录。")
        log.info(f"📄 日志已保存至: {LOG_FILE}")


def main():
    # 初始化日志记录器
    init_logger()

    log = get_logger()
    log.info("=" * 60)
    log.info("  Extra Recipe 打包工具启动")
    log.info(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    # 检查是否有命令行参数
    if len(sys.argv) > 1:
        # 使用命令行模式
        global parser
        parser = argparse.ArgumentParser(
            description='Extra Recipe 打包工具 - 为 Minecraft 模组生成 Datapack 和 All Loader 包',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
示例用法:
  python packed.py 4.0.0 4-1.13-1.14.4    打包指定版本
  python packed.py 4.0.0 all                  打包所有版本
  python packed.py 4.0.0                      使用默认 MC 版本 (1.20.1)
  python packed.py --list                     列出所有支持的 MC 版本
  python packed.py 4.0.0 --only datapack      仅打包 Datapack
  python packed.py 4.0.0 --only all_loader    仅打包 All Loader
            """
        )
        parser.add_argument('version', nargs='?', help='模组版本号 (例如: 4.0.0)')
        parser.add_argument('mc_version', nargs='?', default='all',
                            help='Minecraft 版本标识 (例如: 4-1.13-1.14.4, 默认: all, 使用 all 打包所有版本)')
        parser.add_argument('--list', '-l', action='store_true', help='列出所有支持的 Minecraft 版本')
        parser.add_argument('--only', '-o', choices=['datapack', 'all_loader'],
                            help='仅打包指定类型 (datapack 或 all_loader)')
        parser.add_argument('--output-dir', '-d', type=Path, default=DEFAULT_OUTPUT_DIR,
                            help=f'输出目录 (默认: {DEFAULT_OUTPUT_DIR})')

        args = parser.parse_args()
        cli_mode(args)
    else:
        # 无参数时进入交互模式
        interactive_mode()


if __name__ == "__main__":
    main()


