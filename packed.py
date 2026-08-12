"""Extra Recipe 打包工具。

将仓库中的配方 JSON（按 Minecraft 版本分目录存放）组装为：
  - Datapack (.zip)：纯数据包，适用于各版本客户端/服务端
  - All Loader (.jar)：同时兼容 Fabric / Quilt / Forge / NeoForge 的整合包

设计要点：
  - 配方以"最低支持版本"的格式存放，打包时按目标版本的 pack_format 向上迁移 schema。
  - 目标版本产物 = 其自身目录 + 所有更低版本目录中的配方并集（通过 blacklist.json 控制继承排除）。
  - 全程仅依赖 Python 标准库（json / zipfile / argparse / re / pathlib）。
"""

from __future__ import annotations

import argparse
from argparse import ArgumentParser
import json
import re
from re import Match
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeAlias

# 动态 JSON 数据形状的类型别名。动态解析的 JSON 本质上类型不定，
# 使用 Any 是唯一合理的表示；基于它的子类型也无类型约束意义。
JsonValue: TypeAlias = Any
JsonObject: TypeAlias = dict[str, Any]
JsonList: TypeAlias = list[Any]

PROJECT_DIR: Path = Path(__file__).parent.resolve()
DEFAULT_OUTPUT_DIR: Path = PROJECT_DIR / "output"
LOG_FILE: Path = DEFAULT_OUTPUT_DIR / ".latest.log"
DEFAULT_OUTPUT_DIR.mkdir(exist_ok=True)

# 版本目录名 -> pack_format。键的前缀（可能含小数，如 88.0）即排序依据。
PACK_FORMAT_MAP: dict[str, int | float] = {
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
    "101.1-26.1": 101.1,
    "107.1-26.2": 107.1,
}

# 构建时忽略的文件/目录名
IGNORE_NAMES: frozenset[str] = frozenset[str]({
    "packed.py", "output", ".git", "__pycache__",
    ".idea", "venv", "env", ".DS_Store", "blacklist.json", ".venv",
})

# 保持在压缩包根目录的配置文件
ROOT_CONFIG_FILES: frozenset[str] = frozenset[str]({
    "pack.mcmeta", "fabric.mod.json", "quilt.mod.json",
    "mods.toml", "neoforge.mods.toml", "MANIFEST.MF",
})

# Datapack 根目录直接打入的元数据文件
DATAPACK_ROOT_FILES: tuple[str, ...] = ("LICENSE", "pack.mcmeta", "pack.png", "README.md", "CHANGELOG.md")

# 空黑名单模板（避免多处重复字面量）
EMPTY_BLACKLIST: dict[str, list[str]] = {
    "skip_namespaces": [],
    "skip_recipes": [],
    "allow_namespaces": [],
    "allow_recipes": [],
}

# 常见标签命名空间（用于判断是否应加 # 前缀）
TAG_NAMESPACES: frozenset[str] = frozenset[str]({"c", "forge", "fabric"})

# 配方类型常量
TYPE_SHAPED: str = "minecraft:crafting_shaped"
TYPE_SHAPELESS: str = "minecraft:crafting_shapeless"
TYPE_SMITHING: str = "minecraft:smithing"
TYPE_SMITHING_TRANSFORM: str = "minecraft:smithing_transform"
COOKING_TYPES: tuple[str, ...] = (
    "minecraft:campfire_cooking",
    "minecraft:blasting",
    "minecraft:smoking",
    "minecraft:smelting",
)


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
class Logger:
    """同时输出到控制台（GBK 终端安全）与日志文件。"""

    def __init__(self, log_file: Path) -> None:
        self._log_file: Path = log_file
        _ = self._log_file.write_text(data="", encoding="utf-8")
        self._start_time: datetime = datetime.now()

    def _emit(self, message: str) -> None:
        try:
            print(message)
        except UnicodeEncodeError:
            # Windows GBK 终端下 emoji/特殊字符可能编码失败，用 ASCII 替换兜底
            print(message.encode(encoding="ascii", errors="replace").decode(encoding="ascii"))
        timestamp: str = datetime.now().strftime(format="%H:%M:%S")
        with open(file=self._log_file, mode="a", encoding="utf-8") as f:
            _ = f.write(f"[{timestamp}] {message}\n")

    def info(self, message: str) -> None:
        self._emit(message)

    def success(self, message: str) -> None:
        self._emit(message=f"✅ {message}")

    def warning(self, message: str) -> None:
        self._emit(message=f"⚠️  {message}")

    def error(self, message: str) -> None:
        self._emit(message=f"❌ {message}")

    def skip(self, message: str) -> None:
        self._emit(message=f"⏭️  {message}")

    def get_elapsed_time(self) -> str:
        elapsed: timedelta = datetime.now() - self._start_time
        total_seconds: int = int(elapsed.total_seconds())
        return f"{total_seconds // 60} 分 {total_seconds % 60} 秒"


_logger: Logger | None = None


def get_logger() -> Logger:
    """获取全局日志实例；未初始化时抛出异常。"""
    if _logger is None:
        raise RuntimeError("Logger 未初始化！请先调用 init_logger()")
    return _logger


def init_logger() -> Logger:
    """初始化全局日志实例。"""
    global _logger
    _logger = Logger(log_file=LOG_FILE)
    return _logger


# --------------------------------------------------------------------------- #
# 版本与黑名单
# --------------------------------------------------------------------------- #
def _extract_prefix(version_key: str) -> float:
    """从版本目录名 (如 "88.0-1.21.9-1.21.10") 提取前缀数字用于排序，支持小数前缀。"""
    match: Match[str] | None = re.match(r"^(\d+(?:\.\d+)?)", version_key)
    return float(match.group(1)) if match else 0.0


def get_version_order() -> list[str]:
    """按版本前缀升序返回所有支持版本（前缀支持小数，如 88.0、101.1）。"""
    return sorted(PACK_FORMAT_MAP, key=_extract_prefix)


def _is_version_dir(name: str) -> bool:
    """判断目录名是否为版本源目录（如 4-1.13-1.14.4 或 88.0-1.21.9-1.21.10）。"""
    return "-" in name and bool(re.match(r"^\d+(?:\.\d+)*", name))


def load_blacklist(version_dir: Path) -> dict[str, list[str]]:
    """加载版本目录中的 blacklist.json；缺失或异常时返回空黑名单。"""
    log: Logger = get_logger()
    blacklist_path: Path = version_dir / "blacklist.json"
    if not blacklist_path.exists():
        return dict[str, list[str]](EMPTY_BLACKLIST)

    try:
        with blacklist_path.open(mode="r", encoding="utf-8") as f:
            data: dict[str, list[str]] = json.load(fp=f)
        blacklist: dict[str, list[str]] = {
            key: data.get(key, [])
            for key in ("skip_namespaces", "skip_recipes", "allow_namespaces", "allow_recipes")
        }
        total: int = sum(len(v) for v in blacklist.values())
        if total:
            log.info(
                message="   📋 已加载黑名单: 跳过("
                + f"{len(blacklist['skip_namespaces'])}命名空间, "
                + f"{len(blacklist['skip_recipes'])}配方), "
                + f"允许({len(blacklist['allow_namespaces'])}命名空间, "
                + f"{len(blacklist['allow_recipes'])}配方)"
            )
        return blacklist
    except Exception as e:  # noqa: BLE001 - 黑名单加载失败不应中断打包
        log.warning(message=f"   加载黑名单失败: {e}，将不使用黑名单")
        return dict[str, list[str]](EMPTY_BLACKLIST)


def is_recipe_blacklisted(
    rel_path: Path,
    target_version: str,
    all_blacklists: dict[str, dict[str, list[str]]],
) -> tuple[bool, str]:
    """根据从最低版本到目标版本的累计规则，判断配方是否应被跳过。

    allow_* 规则会覆盖同级别的 skip_* 规则。
    """
    parts: list[str] = str(rel_path.with_suffix(suffix="")).replace("\\", "/").split(sep="/")
    if len(parts) >= 2:
        namespace, recipe_name = parts[0], "/".join(parts[1:])
    elif len(parts) == 1:
        namespace, recipe_name = "minecraft", parts[0]
    else:
        return False, ""

    full_recipe_id: str = f"{namespace}:{recipe_name}"
    sorted_versions: list[str] = get_version_order()
    if target_version not in sorted_versions:
        return False, ""

    current_idx: int = sorted_versions.index(target_version)
    is_skipped = False
    skip_reason = ""

    for version_key in sorted_versions[: current_idx + 1]:
        blacklist: dict[str, list[str]] | None = all_blacklists.get(version_key)
        if blacklist is None:
            continue
        if namespace in blacklist["skip_namespaces"]:
            is_skipped, skip_reason = True, f"命名空间 '{namespace}' 在 {version_key} 被加入跳过列表"
        elif full_recipe_id in blacklist["skip_recipes"]:
            is_skipped, skip_reason = True, f"配方 '{full_recipe_id}' 在 {version_key} 被加入跳过列表"
        if namespace in blacklist["allow_namespaces"]:
            is_skipped, skip_reason = False, ""
        elif full_recipe_id in blacklist["allow_recipes"]:
            is_skipped, skip_reason = False, ""

    return is_skipped, skip_reason


def collect_recipe_files(mc_version: str) -> list[tuple[Path, Path, str]]:
    """收集目标版本及其所有更低版本目录中的配方文件（已应用黑名单过滤）。"""
    log: Logger = get_logger()
    sorted_versions: list[str] = get_version_order()
    if mc_version not in sorted_versions:
        log.warning(message=f"版本 {mc_version} 不在支持列表中")
        return []

    current_idx: int = sorted_versions.index(mc_version)
    versions_to_collect: list[str] = sorted_versions[: current_idx + 1]
    log.info(message=f"   📊 当前版本: {mc_version} (索引 {current_idx})")
    log.info(message=f"   📊 将收集以下 {len(versions_to_collect)} 个版本: {', '.join(versions_to_collect)}")

    all_blacklists: dict[str, dict[str, list[str]]] = {
        v: load_blacklist(version_dir=PROJECT_DIR / v) for v in versions_to_collect
    }

    collected: list[tuple[Path, Path, str]] = []
    skipped_count = 0
    for version_key in versions_to_collect:
        version_dir: Path = PROJECT_DIR / version_key
        if not version_dir.is_dir():
            log.warning(message=f"   未找到版本目录: {version_key}")
            continue
        log.info(message=f"   扫描目录: {version_dir.name}")
        for filepath in sorted(version_dir.rglob(pattern="*")):
            if not filepath.is_file() or filepath.name == "blacklist.json":
                continue
            rel_path: Path = filepath.relative_to(other=version_dir)
            should_skip, reason = is_recipe_blacklisted(rel_path, mc_version, all_blacklists)
            if should_skip:
                log.skip(message=f"   跳过: {rel_path.as_posix()} - {reason}")
                skipped_count += 1
                continue
            collected.append((filepath, rel_path, version_key))

    log.info(message=f"   共收集 {len(collected)} 个文件，跳过 {skipped_count} 个黑名单文件")
    return collected


# --------------------------------------------------------------------------- #
# 配方格式转换
# --------------------------------------------------------------------------- #
def _flatten_ingredients(ingredients: JsonList) -> JsonList:
    """展平可能嵌套的 ingredients 数组（[[{...}]] -> [{...}]）。"""
    flattened: JsonList = []
    for item in ingredients:
        if isinstance(item, list):
            flattened.extend(item)
        else:
            flattened.append(item)
    return flattened


def _convert_key_to_string(key_data: JsonObject) -> JsonObject:
    """key 值：对象格式 {"item": ...} / {"tag": ...} -> 字符串格式。"""
    return {
        char: (f"#{v['tag']}" if "tag" in v else v["item"]) if isinstance(v, dict) else v
        for char, v in key_data.items()
    }


def _convert_key_to_object(key_data: JsonObject) -> JsonObject:
    """key 值：字符串格式 -> 对象格式。"""
    converted: JsonObject = {}
    for char, v in key_data.items():
        if isinstance(v, str):
            converted[char] = {"tag": v[1:]} if v.startswith("#") else {"item": v}
        else:
            converted[char] = v
    return converted


def _convert_ingredients_to_string(ingredients: JsonList) -> JsonList:
    """ingredients 元素：对象格式 -> 字符串格式。"""
    converted: JsonList = []
    for item in ingredients:
        if isinstance(item, dict):
            converted.append(f"#{item['tag']}" if "tag" in item else item["item"])
        else:
            converted.append(item)
    return converted


def _convert_ingredients_to_object(ingredients: JsonList) -> JsonList:
    """ingredients 元素：字符串格式 -> 对象格式（含嵌套列表展平）。"""
    converted: JsonList = []
    for item in ingredients:
        if isinstance(item, str):
            converted.append({"tag": item[1:]} if item.startswith("#") else {"item": item})
        elif isinstance(item, dict):
            converted.append(item)
        elif isinstance(item, list):
            converted.extend(_convert_ingredients_to_object(ingredients=item))
    return converted


def _ensure_tag_prefix(value: str) -> str:
    """为疑似标签的字符串加 # 前缀（路径含 / 或属于常见标签命名空间）。"""
    if value.startswith("#") or ":" not in value:
        return value
    namespace, path = value.split(sep=":", maxsplit=1)
    if "/" in path or namespace in TAG_NAMESPACES:
        return f"#{value}"
    return value


def _convert_tag_prefixes(items: JsonList) -> JsonList:
    """为 ingredients/key 中的标签字符串统一加 # 前缀（1.21.2+ 要求）。"""
    return [_ensure_tag_prefix(value=item) if isinstance(item, str) else item for item in items]


def _convert_smithing(data: JsonObject, pack_format: int | float) -> JsonObject:
    """1.19.4 (pack_format >= 12) 起 smithing -> smithing_transform。"""
    if data.get("type") == TYPE_SMITHING and pack_format >= 12:
        data["type"] = TYPE_SMITHING_TRANSFORM
    return data


def _convert_recipe_for_1_21(data: JsonObject, pack_format: int | float) -> JsonObject:
    """将旧版本配方 schema 转换为目标 pack_format 对应的 1.21+ 格式。"""
    recipe_type: str = data.get("type", "")

    if recipe_type in (TYPE_SHAPED, TYPE_SHAPELESS):
        result = data.get("result")
        if isinstance(result, dict) and "item" in result:
            result["id"] = result.pop("item")

        if recipe_type == TYPE_SHAPED and "key" in data:
            data["key"] = (
                _convert_tag_prefixes(items=list[Any](_convert_key_to_string(key_data=data["key"])))
                if pack_format >= 57
                else _convert_key_to_object(key_data=data["key"])
            )

        if recipe_type == TYPE_SHAPELESS and "ingredients" in data:
            ingredients: JsonList = _flatten_ingredients(ingredients=data["ingredients"])
            data["ingredients"] = (
                _convert_tag_prefixes(items=_convert_ingredients_to_string(ingredients))
                if pack_format >= 57
                else _convert_ingredients_to_object(ingredients)
            )

    elif recipe_type in COOKING_TYPES:
        result = data.get("result")
        if isinstance(result, str):
            data["result"] = {"id": result}
        elif isinstance(result, dict) and "item" in result:
            result["id"] = result.pop("item")

        ingredient: Any | None = data.get("ingredient")
        if isinstance(ingredient, dict):
            if pack_format >= 57:
                data["ingredient"] = (
                    f"#{ingredient['tag']}" if "tag" in ingredient else ingredient["item"]
                )
            elif "item" in ingredient:
                data["ingredient"] = {"item": ingredient["item"]}
            elif "tag" in ingredient:
                data["ingredient"] = {"tag": ingredient["tag"]}

    elif recipe_type == "minecraft:stonecutting" and pack_format >= 41:
        if isinstance(data.get("result"), str):
            count = data.pop("count", 1)
            data["result"] = {"id": data["result"], "count": count}

    return data


def _convert_pottery_shard_to_sherd(obj: JsonValue) -> JsonValue:  # type: ignore[explicit-any]
    """递归将 JSON 中所有 pottery_shard 字符串替换为 pottery_sherd。"""
    if isinstance(obj, dict):
        return {k: _convert_pottery_shard_to_sherd(obj=v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_pottery_shard_to_sherd(obj=v) for v in obj]
    if isinstance(obj, str):
        return obj.replace("pottery_shard", "pottery_sherd")
    return obj


def convert_recipe_file(filepath: Path, mc_version: str) -> bytes:
    """读取配方 JSON 并按目标版本转换格式；非配方/解析失败时回退原始字节。"""
    log: Logger = get_logger()
    try:
        if filepath.suffix != ".json":
            return filepath.read_bytes()
        data: JsonObject = json.loads(s=filepath.read_text(encoding="utf-8"))
        if "type" not in data:
            return filepath.read_bytes()

        pack_format: int | float = PACK_FORMAT_MAP.get(mc_version, 4)
        data = _convert_smithing(data, pack_format)
        if pack_format >= 41:
            data = _convert_recipe_for_1_21(data, pack_format)
        return json.dumps(obj=data, indent=2, ensure_ascii=False).encode(encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - 单文件转换失败不应中断整体打包
        log.warning(message=f"   转换配方 {filepath.name} 失败: {e}，使用原始文件")
        return filepath.read_bytes()


def convert_pottery_file(filepath: Path, mc_version: str) -> bytes:
    """1.20+ 目标版本下，将 pottery_shard 文件内容转为 pottery_sherd。"""
    log: Logger = get_logger()
    try:
        if PACK_FORMAT_MAP.get(mc_version, 4) < 15 or "pottery_shard" not in filepath.name:
            return filepath.read_bytes()
        if filepath.suffix != ".json":
            return filepath.read_bytes()
        data = json.loads(s=filepath.read_text(encoding="utf-8"))
        return json.dumps(
            obj=_convert_pottery_shard_to_sherd(obj=data), indent=2, ensure_ascii=False
        ).encode(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning(message=f"   转换陶片文件 {filepath.name} 失败: {e}，使用原始文件")
        return filepath.read_bytes()


# --------------------------------------------------------------------------- #
# 路径与元数据
# --------------------------------------------------------------------------- #
def get_recipe_archive_path(rel_path: Path, mc_version: str) -> str:
    """计算配方在压缩包内的归档路径（处理 recipe/recipes 切换与 smithing 重命名）。"""
    pack_format: int | float = PACK_FORMAT_MAP.get(mc_version, 4)
    parts: tuple[str, ...] = rel_path.parts
    recipe_folder: Literal['recipe', 'recipes'] = "recipe" if pack_format >= 48 else "recipes"
    namespace: str = parts[0] if parts else ""

    if len(parts) >= 2 and parts[1] == "smithing" and pack_format >= 12:
        remaining: tuple[str, ...] = (namespace, "smithing_transform", *parts[2:])
    else:
        remaining = parts

    return str(Path("data/extrarecipe") / recipe_folder / "/".join(remaining))


def get_archive_path(filepath: Path, _pack_type: str, mc_version: str) -> str:
    """计算任意文件在压缩包内的归档路径。"""
    rel: Path = filepath.relative_to(other=PROJECT_DIR)
    if filepath.name in ROOT_CONFIG_FILES:
        return rel.as_posix()
    if len(rel.parts) >= 2 and _is_version_dir(name=rel.parts[0]):
        return get_recipe_archive_path(rel, mc_version)
    return rel.as_posix()


def _patch_loader_metadata(name: str, raw: str, version: str, mc_version: str) -> str:
    """根据目标版本重写 loader 元数据中的 version / modLoader / loaderVersion。"""
    pack_format: int | float = PACK_FORMAT_MAP.get(mc_version, 4)

    if name == "neoforge.mods.toml":
        raw = re.sub(r'version\s*=\s*"([^"]*)"', f'version="{version}"', raw, count=1)
        loader_ver = "[4,)" if pack_format >= 94.1 else "[2,)"
        return re.sub(r'loaderVersion\s*=\s*"\[.*?\)"', f'loaderVersion="{loader_ver}"', raw, count=1)

    if name == "mods.toml":
        raw = re.sub(
            r"(modId\s*=\s*'extrarecipe',\s*version\s*=\s*')([^']+)'",
            rf"\g<1>{version}'", raw, count=1,
        )
        if pack_format == 4:
            mod_loader, loader_ver = "javafml", "[28,)"
        elif pack_format == 5:
            mod_loader, loader_ver = "javafml", "[31,)"
        elif pack_format < 8:
            mod_loader, loader_ver = "javafml", "[34,)"
        elif pack_format < 41:
            mod_loader, loader_ver = "lowcodefml", "[34,)"
        else:
            mod_loader, loader_ver = "javafml", "[2,)"
        raw = re.sub(r'modLoader\s*=\s*".*?"', f'modLoader="{mod_loader}"', raw, count=1)
        return re.sub(r"loaderVersion\s*=\s*'\[.*?\)'", f"loaderVersion = '{loader_ver}'", raw, count=1)

    return raw


def get_modified_content(filepath: Path, pack_type: str, version: str, mc_version: str) -> bytes:
    """读取配置文件并按目标版本改写版本号 / pack_format / loader 信息。"""
    log: Logger = get_logger()
    name: str = filepath.name
    try:
        if name in ("pack.mcmeta", "fabric.mod.json"):
            data: JsonObject = json.loads(filepath.read_text(encoding="utf-8"))
            if name == "pack.mcmeta":
                pack_format: int | float = PACK_FORMAT_MAP.get(mc_version, 4)
                pack = data.setdefault("pack", {})
                if isinstance(pack, dict):
                    if pack_format >= 88.0:
                        pack.pop("pack_format", None)
                        pack["min_format"] = pack_format
                        pack["max_format"] = pack_format
                    else:
                        pack["pack_format"] = pack_format
                    pack["description"] = f"Extra Recipe v{version}"
                    if pack_type == "datapack":
                        for key in ("fabric", "quilt", "forge", "neoforge"):
                            pack.pop(key, None)
            else:
                data["version"] = version
            return json.dumps(obj=data, indent=2, ensure_ascii=False).encode(encoding="utf-8")

        if name == "quilt.mod.json":
            data = json.loads(s=filepath.read_text(encoding="utf-8"))
            if "quilt_loader" in data:
                data["quilt_loader"]["version"] = version
            return json.dumps(obj=data, indent=2, ensure_ascii=False).encode(encoding="utf-8")

        if name in ("neoforge.mods.toml", "mods.toml"):
            encoding: Literal['utf-8-sig', 'utf-8'] = "utf-8-sig" if name == "neoforge.mods.toml" else "utf-8"
            raw: str = filepath.read_text(encoding=encoding)
            return _patch_loader_metadata(name, raw, version, mc_version).encode(encoding="utf-8")

        return filepath.read_bytes()
    except Exception as e:  # noqa: BLE001
        log.warning(message=f"读取/修改 {name} 失败: {e}，将使用原始文件。")
        return filepath.read_bytes()


def should_include(filepath: Path, pack_type: str) -> bool:
    """判断文件是否应纳入指定类型的包。"""
    if filepath.name in IGNORE_NAMES or filepath.parent.name in IGNORE_NAMES:
        return False
    if filepath.is_dir() and filepath.name.startswith(".") and filepath.name != "META-INF":
        return False
    if pack_type == "datapack":
        if filepath.name in ("fabric.mod.json", "quilt.mod.json", "mods.toml", "neoforge.mods.toml"):
            return False
        if "META-INF" in filepath.relative_to(other=PROJECT_DIR).parts:
            return False
    return True


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #
def add_recipe_files(zf: zipfile.ZipFile, mc_version: str) -> None:
    """将目标版本及其更低版本的所有配方写入 zip（去重 + 格式转换 + 陶片重命名）。"""
    log: Logger = get_logger()
    added_paths: set[str] = set[str]()
    for filepath, rel_path, _ in collect_recipe_files(mc_version):
        if filepath.name == "blacklist.json":
            log.warning(message=f"   跳过黑名单文件: {filepath}")
            continue
        arcname: str = get_recipe_archive_path(rel_path, mc_version)
        if arcname in added_paths:
            continue
        try:
            content: bytes = convert_recipe_file(filepath, mc_version)
            if "pottery_shard" in filepath.name:
                content = convert_pottery_file(filepath, mc_version)
                if PACK_FORMAT_MAP.get(mc_version, 4) >= 15:
                    arcname = arcname.replace("pottery_shard", "pottery_sherd")
            zf.writestr(zinfo_or_arcname=arcname, data=content)
            added_paths.add(arcname)
        except Exception as e:  # noqa: BLE001
            log.warning(message=f"   添加失败 {arcname}: {e}")
    log.info(message=f"   ✅ 已添加 {len(added_paths)} 个配方文件")


def build_datapack(version: str, mc_version: str, output_dir: Path) -> None:
    """构建 Datapack (.zip)。"""
    log: Logger = get_logger()
    version_with_prefix: str = f"{version}-{mc_version.split(sep='-')[0]}"
    output_path: Path = output_dir / f"[Datapack]Extra Recipe-{version_with_prefix}.zip"

    log.info(message=f"📦 正在打包 Datapack: {output_path.name} ...")
    log.info(message=f"   目标版本: {mc_version}")

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for config_file in DATAPACK_ROOT_FILES:
            config_path: Path = PROJECT_DIR / config_file
            if not config_path.exists():
                continue
            if config_file == "pack.mcmeta":
                content: bytes = get_modified_content(config_path, "datapack", version_with_prefix, mc_version)
            else:
                content = config_path.read_bytes()
            zf.writestr(config_file, data=content)
            log.info(message=f"   添加配置文件: {config_file}")
        add_recipe_files(zf, mc_version)

    log.success(message=f"Datapack 打包成功: {output_path}")


def build_all_loader(version: str, mc_version: str, output_dir: Path) -> None:
    """构建 All Loader 模组包 (.jar)。"""
    log: Logger = get_logger()
    version_with_prefix: str = f"{version}-{mc_version.split(sep='-')[0]}"
    output_path: Path = output_dir / f"[All Loader]Extra Recipe-{version_with_prefix}.jar"

    log.info(message=f"📦 正在打包 All Loader: {output_path.name} ...")
    log.info(message=f"   目标版本: {mc_version}")

    include_class_files: bool = PACK_FORMAT_MAP.get(mc_version, 4) < 8

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for filepath in sorted(PROJECT_DIR.rglob(pattern="*")):
            if not filepath.is_file():
                continue
            if _is_version_dir(filepath.parent.name) or filepath.parent.name in IGNORE_NAMES:
                continue
            if not include_class_files and filepath.suffix == ".class":
                continue
            if not should_include(filepath, pack_type="all_loader"):
                continue
            arcname: str = get_archive_path(filepath, "all_loader", mc_version)
            content: bytes = get_modified_content(filepath, "all_loader", version_with_prefix, mc_version)
            zf.writestr(arcname, data=content)
        add_recipe_files(zf, mc_version)

    log.success(message=f"All Loader 打包成功: {output_path}")


def list_versions() -> None:
    """列出所有支持的 Minecraft 版本。"""
    log: Logger = get_logger()
    log.info(message="\n📋 支持的 Minecraft 版本:")
    log.info(message="-" * 50)
    for version_key in get_version_order():
        log.info(message=f"  {version_key.split(sep='-')[0]}: {version_key}")
    log.info(message="-" * 50)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _build_one(version: str, mc_version: str, output_dir: Path, only: str | None) -> None:
    if only in (None, "datapack"):
        build_datapack(version, mc_version, output_dir)
    if only in (None, "all_loader"):
        build_all_loader(version, mc_version, output_dir)


def cli_mode(args: argparse.Namespace, parser: ArgumentParser) -> None:
    """命令行模式。"""
    log: Logger = get_logger()
    if args.list:
        list_versions()
        return
    if not args.version:
        log.error(message="错误: 缺少必需的版本号参数\n")
        parser.print_help()
        sys.exit(1)

    output_dir = args.output_dir
    output_dir.mkdir(exist_ok=True)
    version = args.version

    if args.mc_version.lower() == "all":
        sorted_versions: list[str] = get_version_order()
        log.info(message=f"🚀 开始批量打包 Extra Recipe v{version}")
        log.info(message=f"📁 输出目录: {output_dir}")
        log.info(message=f"📋 共 {len(sorted_versions)} 个版本需要打包")
        log.info(message="-" * 50)
        success_count = fail_count = 0
        for idx, mc_ver in enumerate[str](sorted_versions, 1):
            log.info(message=f"\n[{idx}/{len(sorted_versions)}] 正在处理: {mc_ver}")
            try:
                _build_one(version, mc_ver, output_dir, args.only)
                success_count += 1
            except Exception as e:  # noqa: BLE001
                log.error(message=f"   打包失败: {e}")
                fail_count += 1
        log.info(message="\n" + "=" * 50)
        log.info(message="🎉 批量打包完成！")
        log.info(message=f"   成功: {success_count} 个版本")
        log.info(message=f"   失败: {fail_count} 个版本")
        log.info(message=f"   耗时: {log.get_elapsed_time()}")
        log.info(message=f"📁 产物已保存至: {output_dir}")
        log.info(message=f"📄 日志已保存至: {LOG_FILE}")
    else:
        if args.mc_version not in PACK_FORMAT_MAP:
            log.error(message=f"错误: 无效的版本标识 '{args.mc_version}'")
            log.info(message="请使用 --list 查看支持的版本，或使用 'all' 打包所有版本")
            sys.exit(1)
        log.info(message=f"🚀 开始打包 Extra Recipe v{version} (目标 MC: {args.mc_version})")
        log.info(message=f"📁 输出目录: {output_dir}")
        log.info(message="-" * 50)
        _build_one(version, args.mc_version, output_dir, args.only)
        log.info(message="-" * 50)
        log.info(message="🎉 全部打包完成！产物已保存至 output/ 目录。")
        log.info(message=f"📄 日志已保存至: {LOG_FILE}")


def interactive_mode() -> None:
    """交互式配置模式。"""
    log: Logger = get_logger()
    log.info(message="=" * 60)
    log.info(message="  Extra Recipe 打包工具 - 交互式配置")
    log.info(message="=" * 60)

    while not (version := input("请输入模组版本号 (例如: 4.0.0): ").strip()):
        log.error(message="版本号不能为空，请重新输入\n")

    list_versions()
    log.info(message="  all: 一次性打包所有版本")

    mc_version = "all"
    batch_mode = False
    while True:
        choice: str = input(f"\n请输入版本前缀、all 或直接输入完整版本标识 (默认: {mc_version}): ").strip()
        if not choice:
            batch_mode = True
            break
        if choice.lower() == "all":
            batch_mode = True
            break
        try:
            prefix_num: int = int(choice)
            match: str | None = next((v for v in get_version_order() if v.startswith(f"{prefix_num}-")), None)
            if match:
                mc_version: str = match
                break
            log.error(message=f"未找到前缀为 {prefix_num} 的版本，请重新输入")
        except ValueError:
            if choice in PACK_FORMAT_MAP:
                mc_version = choice
                break
            log.error(message="无效的版本标识，请重新输入")

    chosen: str = "3"
    while True:
        pack_choice: str = input("\n请选择打包类型:\n  1. 仅 Datapack\n  2. 仅 All Loader\n  3. 两者 (默认): ").strip()
        if not pack_choice or pack_choice in ("1", "2", "3"):
            chosen = pack_choice or "3"
            break
        log.error(message="无效的选项，请输入 1、2 或 3")

    pack_types: list[str] = (
        ["datapack"] if chosen == "1"
        else ["all_loader"] if chosen == "2"
        else ["both"]
    )

    custom_dir: Path | None = None
    if input("\n是否自定义输出目录？(y/n, 默认: n): ").strip().lower() == "y":
        while True:
            path_input: str = input("请输入输出目录路径: ").strip()
            if not path_input:
                break
            try:
                candidate: Path = Path(path_input)
                candidate.mkdir(parents=True, exist_ok=True)
                custom_dir = candidate
                break
            except Exception as e:  # noqa: BLE001
                log.error(message=f"无法创建目录: {e}，请重新输入")

    output_dir: Path = custom_dir if custom_dir is not None else DEFAULT_OUTPUT_DIR

    log.info(message="\n" + "=" * 60)
    log.info(message="  配置确认")
    log.info(message="=" * 60)
    log.info(message=f"  模组版本:   {version}")
    log.info(message=f"  MC 版本:    {'全部版本 (' + str(len(PACK_FORMAT_MAP)) + ' 个)' if batch_mode else mc_version}")
    log.info(message=f"  打包类型:   {'Datapack + All Loader' if pack_types == ['both'] else ', '.join(pack_types)}")
    log.info(message=f"  输出目录:   {output_dir}")
    log.info(message="=" * 60)

    if input("\n确认开始打包？(y/n, 默认: y): ").strip().lower() == "n":
        log.error(message="已取消打包")
        return

    only: str | None = None if "both" in pack_types else pack_types[0]
    if batch_mode:
        log.info(message="")
        for idx, mc_ver in enumerate[str](get_version_order(), 1):
            log.info(message=f"\n[{idx}/{len(PACK_FORMAT_MAP)}] 正在处理: {mc_ver}")
            _build_one(version, mc_ver, output_dir, only)
    else:
        log.info(message="")
        _build_one(version, mc_version, output_dir, only)
    log.info(message="-" * 50)
    log.info(message="🎉 全部打包完成！产物已保存至 output/ 目录。")
    log.info(message=f"📄 日志已保存至: {LOG_FILE}")


def main() -> None:
    _ = init_logger()
    log: Logger = get_logger()
    log.info(message="=" * 60)
    log.info(message="  Extra Recipe 打包工具启动")
    log.info(message=f"  时间: {datetime.now().strftime(format='%Y-%m-%d %H:%M:%S')}")
    log.info(message="=" * 60)

    if len(sys.argv) > 1:
        parser: ArgumentParser = argparse.ArgumentParser(
            description="Extra Recipe 打包工具 - 为 Minecraft 模组生成 Datapack 和 All Loader 包",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=(
                "示例用法:\n"
                "  python packed.py 4.0.0 4-1.13-1.14.4    打包指定版本\n"
                "  python packed.py 4.0.0 all                  打包所有版本\n"
                "  python packed.py 4.0.0                      使用默认 MC 版本 (1.20.1)\n"
                "  python packed.py --list                     列出所有支持的 MC 版本\n"
                "  python packed.py 4.0.0 --only datapack      仅打包 Datapack\n"
                "  python packed.py 4.0.0 --only all_loader    仅打包 All Loader\n"
            ),
        )
        _ = parser.add_argument("version", nargs="?", help="模组版本号 (例如: 4.0.0)")
        _ = parser.add_argument(
            "mc_version", nargs="?", default="all",
            help="Minecraft 版本标识 (例如: 4-1.13-1.14.4, 默认: all)",
        )
        _ = parser.add_argument("--list", "-l", action="store_true", help="列出所有支持的 Minecraft 版本")
        _ = parser.add_argument(
            "--only", "-o", choices=["datapack", "all_loader"],
            help="仅打包指定类型 (datapack 或 all_loader)",
        )
        _ = parser.add_argument(
            "--output-dir", "-d", type=Path, default=DEFAULT_OUTPUT_DIR,
            help=f"输出目录 (默认: {DEFAULT_OUTPUT_DIR})",
        )
        cli_mode(args=parser.parse_args(), parser=parser)
    else:
        interactive_mode()


if __name__ == "__main__":
    main()
