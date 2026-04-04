import os
import sys
import json
import zipfile
import re
import argparse
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output"
DEFAULT_OUTPUT_DIR.mkdir(exist_ok=True)

PACK_FORMAT_MAP = {
    "4-1.13–1.14.4": 4,
    "5-1.15–1.16.1": 5,
    "6-1.16.2–1.16.5": 6,
    "7-1.17–1.17.1": 7,
    "8-1.18–1.18.1": 8,
    "9-1.18.2": 9,
    "10-1.19–1.19.3": 10,
    "12-1.19.4": 12,
    "15-1.20–1.20.1": 15,
    "18-1.20.2": 18,
    "26-1.20.3–1.20.4": 26,
    "41-1.20.5–1.20.6": 41,
    "48-1.21–1.21.1": 48,
    "57-1.21.2–1.21.3": 57,
    "61-1.21.4": 61,
    "71-1.21.5": 71,
    "80-1.21.6": 80,
    "81-1.21.7–1.21.8": 81,
    "88.0-1.21.9–1.21.10": 88.0,
    "94.1-1.21.11": 94.1,
    "101.1-26.1": 101.1
}

IGNORE_NAMES = {'packed.py', 'output', '.git', '__pycache__', '.idea', 'venv', 'env', '.DS_Store'}


def get_modified_content(filepath: Path, pack_type: str, version: str, mc_version: str) -> bytes:
    name = filepath.name
    try:
        if name in ('pack.mcmeta', 'fabric.mod.json', 'quilt.mod.json'):
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if name == 'pack.mcmeta':
                data.setdefault('pack', {})['pack_format'] = PACK_FORMAT_MAP.get(mc_version, 4)
                data.setdefault('pack', {})['description'] = f"Extra Recipe v{version}"
                if pack_type == 'datapack':
                    for key in ('fabric', 'quilt', 'forge', 'neoforge'):
                        data.pop(key, None)
            elif name in ('fabric.mod.json', 'quilt.mod.json'):
                data['version'] = version

            return json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')

        elif name == 'mods.toml':
            content = filepath.read_text(encoding='utf-8')
            content = re.sub(r'(\[\[mods]][\s\S]*?version\s*=\s*)".*?"', rf'\1"{version}"', content, count=1)
            content = re.sub(r'(displayName\s*=\s*)".*?"', rf'\1"Extra Recipe v{version}"', content, count=1)
            return content.encode('utf-8')

        return filepath.read_bytes()

    except Exception as e:
        print(f"⚠️  读取/修改 {filepath.name} 失败: {e}，将使用原始文件。")
        return filepath.read_bytes()


def should_include(filepath: Path, pack_type: str) -> bool:
    """判断文件是否应包含在当前包中"""
    rel = filepath.relative_to(PROJECT_DIR)

    if filepath.name in IGNORE_NAMES or filepath.parent.name in IGNORE_NAMES:
        return False
    if filepath.is_dir() and filepath.name.startswith('.') and filepath.name != 'META-INF':
        return False

    if pack_type == 'datapack':
        if filepath.name in ('fabric.mod.json', 'quilt.mod.json', 'mods.toml'):
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
    if filepath.name in ('pack.mcmeta', 'fabric.mod.json', 'quilt.mod.json', 'mods.toml', 'MANIFEST.MF'):
        return rel.as_posix()

    parts = rel.parts

    # 检查是否在版本目录下（如 6-1.16.2-1.16.5/minecraft/...）
    if len(parts) >= 2:
        first_part = parts[0]
        # 匹配版本目录格式：数字-版本号（如 6-1.16.2-1.16.5）
        if re.match(r'^\d+-', first_part):
            remaining_parts = parts[1:]

            # 两种包都是: data/extrarecipe/recipes/minecraft/...
            new_path = Path('data/extrarecipe/recipes') / '/'.join(remaining_parts)
            return new_path.as_posix()

    return rel.as_posix()


def get_version_order():
    """获取按版本号排序的版本列表"""

    def extract_prefix(version_key):
        """提取版本前缀用于排序"""
        # 从 "6-1.16.2–1.16.5" 提取前面的数字 6
        match = re.match(r'^(\d+)', version_key)
        if match:
            return int(match.group(1))
        return 0

    return sorted(PACK_FORMAT_MAP.keys(), key=extract_prefix)


def collect_recipe_files(mc_version: str) -> list:
    """收集指定版本及所有低版本的配方文件"""
    sorted_versions = get_version_order()

    # 找到当前版本在排序中的位置
    if mc_version not in sorted_versions:
        print(f"⚠️  警告: 版本 {mc_version} 不在支持列表中")
        return []

    current_idx = sorted_versions.index(mc_version)

    # 收集当前版本及所有低版本的文件
    collected_files = []
    for version_key in sorted_versions[:current_idx + 1]:
        # 查找实际存在的版本目录（处理破折号不匹配问题）
        version_dir = None
        for item in PROJECT_DIR.iterdir():
            if item.is_dir() and re.match(r'^\d+-', item.name):
                # 提取前缀数字进行比较
                key_prefix = version_key.split('-')[0]
                dir_prefix = item.name.split('-')[0]
                if key_prefix == dir_prefix:
                    version_dir = item
                    break

        if version_dir and version_dir.exists():
            print(f"   扫描目录: {version_dir.name}")
            for root, dirs, files in os.walk(version_dir):
                dirs[:] = [d for d in dirs if d not in IGNORE_NAMES]
                for file in files:
                    filepath = Path(root) / file
                    # 计算相对路径（相对于版本目录）
                    rel_path = filepath.relative_to(version_dir)
                    collected_files.append((filepath, rel_path, version_key))
        else:
            print(f"   ⚠️  未找到版本目录: {version_key}")

    print(f"   共收集 {len(collected_files)} 个文件")
    return collected_files


def build_datapack(version: str, mc_version: str, output_dir: Path):
    """构建 Datapack"""
    # 提取 MC 版本前缀
    mc_prefix = mc_version.split('-')[0]
    filename = f"[Datapack]Extra Recipe-{version}-{mc_prefix}.zip"
    output_path = output_dir / filename

    print(f"📦 正在打包 Datapack: {filename} ...")
    print(f"   目标版本: {mc_version}")

    # 收集配方文件
    recipe_files = collect_recipe_files(mc_version)

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 添加配置文件（pack.mcmeta, pack.png, README.md）
        for config_file in ['LICENSE', 'pack.mcmeta', 'pack.png', 'README.md']:
            config_path = PROJECT_DIR / config_file
            if config_path.exists():
                if config_file == 'pack.mcmeta':
                    content = get_modified_content(config_path, 'datapack', version, mc_version)
                else:
                    content = config_path.read_bytes()
                zf.writestr(config_file, content)
                print(f"   添加配置文件: {config_file}")

        # 添加配方文件（高版本包含低版本）
        added_paths = set()
        for filepath, rel_path, source_version in recipe_files:
            # 构建归档路径: data/extrarecipe/recipes/{相对路径}
            arcname = f"data/extrarecipe/recipes/{rel_path.as_posix()}"

            # 避免重复添加（如果高版本有同名文件，使用高版本的）
            if arcname not in added_paths:
                try:
                    content = filepath.read_bytes()
                    zf.writestr(arcname, content)
                    added_paths.add(arcname)
                except Exception as e:
                    print(f"   ⚠️  添加失败 {arcname}: {e}")

        print(f"   ✅ 已添加 {len(added_paths)} 个配方文件")

    print(f"✅ Datapack 打包成功: {output_path}")


def build_all_loader(version: str, mc_version: str, output_dir: Path):
    """构建 All Loader 模组包"""
    # 提取 MC 版本前缀
    mc_prefix = mc_version.split('-')[0]
    filename = f"[All Loader]Extra Recipe-{version}-{mc_prefix}.jar"
    output_path = output_dir / filename

    print(f"📦 正在打包 All Loader: {filename} ...")
    print(f"   目标版本: {mc_version}")

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 添加所有配置文件和模组文件（不包括版本目录）
        for root, dirs, files in os.walk(PROJECT_DIR):
            dirs[:] = [d for d in dirs if d not in IGNORE_NAMES]

            # 计算相对路径
            rel_root = Path(root).relative_to(PROJECT_DIR)

            # 跳过版本目录及其子目录
            if rel_root.parts and re.match(r'^\d+-', rel_root.parts[0]):
                continue

            for file in files:
                filepath = Path(root) / file
                if not should_include(filepath, 'all_loader'):
                    continue

                arcname = get_archive_path(filepath, 'all_loader', mc_version)
                content = get_modified_content(filepath, 'all_loader', version, mc_version)
                zf.writestr(arcname, content)

        # 收集并添加配方文件（高版本包含低版本）
        recipe_files = collect_recipe_files(mc_version)

        added_paths = set()
        for filepath, rel_path, source_version in recipe_files:
            # 构建归档路径
            arcname = f"data/extrarecipe/recipes/{rel_path.as_posix()}"

            # 避免重复添加
            if arcname not in added_paths:
                try:
                    content = filepath.read_bytes()
                    zf.writestr(arcname, content)
                    added_paths.add(arcname)
                except Exception as e:
                    print(f"   ⚠️  添加失败 {arcname}: {e}")

        print(f"   ✅ 已添加 {len(added_paths)} 个配方文件")

    print(f"✅ All Loader 打包成功: {output_path}")


def list_versions():
    """列出所有支持的 Minecraft 版本"""
    print("\n📋 支持的 Minecraft 版本:")
    print("-" * 50)

    sorted_versions = get_version_order()

    for version_key in sorted_versions:
        # 提取前缀数字
        prefix = version_key.split('-')[0]
        print(f"  {prefix}: {version_key}")
    print("-" * 50)


def interactive_mode():
    """交互式配置模式"""
    print("=" * 60)
    print("  Extra Recipe 打包工具 - 交互式配置")
    print("=" * 60)
    print()

    # 1. 输入版本号
    while True:
        version = input("请输入模组版本号 (例如: 4.0.0): ").strip()
        if version:
            break
        print("❌ 版本号不能为空，请重新输入\n")

    # 2. 选择 Minecraft 版本
    print("\n请选择 Minecraft 版本:")
    list_versions()
    print("  all: 一次性打包所有版本")

    mc_version = "15-1.20–1.20.1"  # 设置默认值
    batch_mode = False
    while True:
        mc_choice = input("\n请输入版本前缀、all 或直接输入完整版本标识 (默认: 15-1.20–1.20.1): ").strip()

        if not mc_choice:
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
                print(f"❌ 未找到前缀为 {prefix_num} 的版本，请重新输入")
                continue
        except ValueError:
            # 直接输入完整版本标识
            if mc_choice in PACK_FORMAT_MAP:
                mc_version = mc_choice
                break
            else:
                print("❌ 无效的版本标识，请重新输入")
                continue

    # 3. 选择打包类型
    print("\n请选择打包类型:")
    print("  1. 仅打包 Datapack (.zip)")
    print("  2. 仅打包 All Loader (.jar)")
    print("  3. 两者都打包 (默认)")

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
            print("❌ 无效的选项，请输入 1、2 或 3")
            continue

    # 4. 选择输出目录
    print(f"\n当前输出目录: {DEFAULT_OUTPUT_DIR}")
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
                    print(f"❌ 无法创建目录: {e}，请重新输入")
            else:
                output_dir = DEFAULT_OUTPUT_DIR
                break
    else:
        output_dir = DEFAULT_OUTPUT_DIR
        output_dir.mkdir(exist_ok=True)

    # 5. 确认配置
    print("\n" + "=" * 60)
    print("  配置确认")
    print("=" * 60)
    print(f"  模组版本:   {version}")
    if batch_mode:
        print(f"  MC 版本:    全部版本 ({len(PACK_FORMAT_MAP)} 个)")
    else:
        print(f"  MC 版本:    {mc_version}")
    print(f"  打包类型:   {', '.join(pack_types) if pack_types != ['both'] else 'Datapack + All Loader'}")
    print(f"  输出目录:   {output_dir}")
    print("=" * 60)

    confirm = input("\n确认开始打包？(y/n, 默认: y): ").strip().lower()
    if confirm == 'n':
        print("❌ 已取消打包")
        return

    # 6. 开始打包
    print()

    if batch_mode:
        # 批量打包所有版本
        sorted_versions = get_version_order()
        print(f"🚀 开始批量打包 Extra Recipe v{version}")
        print(f"📁 输出目录: {output_dir}")
        print(f"📋 共 {len(sorted_versions)} 个版本需要打包")
        print("-" * 50)

        success_count = 0
        fail_count = 0

        for idx, mc_ver in enumerate(sorted_versions, 1):
            print(f"\n[{idx}/{len(sorted_versions)}] 正在处理: {mc_ver}")
            try:
                if 'both' in pack_types or 'datapack' in pack_types:
                    build_datapack(version, mc_ver, output_dir)

                if 'both' in pack_types or 'all_loader' in pack_types:
                    build_all_loader(version, mc_ver, output_dir)

                success_count += 1
            except Exception as e:
                print(f"   ❌ 打包失败: {e}")
                fail_count += 1

        print("\n" + "=" * 50)
        print(f"🎉 批量打包完成！")
        print(f"   成功: {success_count} 个版本")
        print(f"   失败: {fail_count} 个版本")
        print(f"📁 产物已保存至: {output_dir}")
    else:
        # 单个版本打包
        print(f"🚀 开始打包 Extra Recipe v{version} (目标 MC: {mc_version})")
        print(f"📁 输出目录: {output_dir}")
        print("-" * 50)

        if 'both' in pack_types or 'datapack' in pack_types:
            build_datapack(version, mc_version, output_dir)

        if 'both' in pack_types or 'all_loader' in pack_types:
            build_all_loader(version, mc_version, output_dir)

        print("-" * 50)
        print("🎉 全部打包完成！产物已保存至 output/ 目录。")


def cli_mode(args):
    """命令行模式"""
    if args.list:
        list_versions()
        return

    if not args.version:
        print("❌ 错误: 缺少必需的版本号参数\n")
        parser.print_help()
        sys.exit(1)

    output_dir = args.output_dir
    output_dir.mkdir(exist_ok=True)

    version = args.version
    mc_version = args.mc_version

    # 检查是否为批量模式
    if mc_version.lower() == 'all':
        sorted_versions = get_version_order()
        print(f"🚀 开始批量打包 Extra Recipe v{version}")
        print(f"📁 输出目录: {output_dir}")
        print(f"📋 共 {len(sorted_versions)} 个版本需要打包")
        print("-" * 50)

        success_count = 0
        fail_count = 0

        for idx, mc_ver in enumerate(sorted_versions, 1):
            print(f"\n[{idx}/{len(sorted_versions)}] 正在处理: {mc_ver}")
            try:
                if args.only == 'datapack' or args.only is None:
                    build_datapack(version, mc_ver, output_dir)

                if args.only == 'all_loader' or args.only is None:
                    build_all_loader(version, mc_ver, output_dir)

                success_count += 1
            except Exception as e:
                print(f"   ❌ 打包失败: {e}")
                fail_count += 1

        print("\n" + "=" * 50)
        print(f"🎉 批量打包完成！")
        print(f"   成功: {success_count} 个版本")
        print(f"   失败: {fail_count} 个版本")
        print(f"📁 产物已保存至: {output_dir}")
    else:
        # 单个版本打包
        print(f"🚀 开始打包 Extra Recipe v{version} (目标 MC: {mc_version})")
        print(f"📁 输出目录: {output_dir}")
        print("-" * 50)

        if args.only == 'datapack' or args.only is None:
            build_datapack(version, mc_version, output_dir)

        if args.only == 'all_loader' or args.only is None:
            build_all_loader(version, mc_version, output_dir)

        print("-" * 50)
        print("🎉 全部打包完成！产物已保存至 output/ 目录。")


def main():
    # 检查是否有命令行参数
    if len(sys.argv) > 1:
        # 使用命令行模式
        global parser
        parser = argparse.ArgumentParser(
            description='Extra Recipe 打包工具 - 为 Minecraft 模组生成 Datapack 和 All Loader 包',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
示例用法:
  python packed.py 4.0.0 6-1.16.2-1.16.5    打包指定版本
  python packed.py 4.0.0 all                  打包所有版本
  python packed.py 4.0.0                      使用默认 MC 版本 (1.20.1)
  python packed.py --list                     列出所有支持的 MC 版本
  python packed.py 4.0.0 --only datapack      仅打包 Datapack
  python packed.py 4.0.0 --only all_loader    仅打包 All Loader
            """
        )

        parser.add_argument('version', nargs='?', help='模组版本号 (例如: 4.0.0)')
        parser.add_argument('mc_version', nargs='?', default='1.20.1',
                            help='Minecraft 版本标识 (例如: 6-1.16.2-1.16.5, 默认: 1.20.1, 使用 all 打包所有版本)')
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
