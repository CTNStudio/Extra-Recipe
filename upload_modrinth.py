"""Extra Recipe Modrinth 自动上传脚本。

扫描 output/ 目录下的构建产物（[Datapack] zip 与 [All Loader] jar），
解析版本信息并通过 Modrinth API 为每个产物创建独立 version 上传。

设计要点：
  - Datapack zip 与 All Loader jar 分开发布：loaders 分别为 ["datapack"] 与
    ["fabric","forge","neoforge","quilt"]；两者 version_number 相同
    （如 4.0.4-41），靠 name 与 loaders 区分。
  - 上传前查询项目已有版本，相同 version_number 且相同 loaders 的版本
    已存在则跳过并提示。
  - changelog 自动取自 CHANGELOG.md 中 `## v{模组版本}` 段落。
  - 认证使用 Modrinth PAT（环境变量 MODRINTH_TOKEN 或 --token），token 不落盘、不打日志。
  - 全程仅依赖 Python 标准库（urllib.request / json / re / secrets），与 packed.py 风格一致。
"""

from __future__ import annotations

import argparse
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime
import json
import os
import re
from re import Match
import secrets
import sys
import time
from pathlib import Path
from typing import Any, TypeAlias
import urllib.error
import urllib.request

# 动态 JSON 数据形状的类型别名（与 packed.py 保持一致）。
JsonValue: TypeAlias = Any
JsonObject: TypeAlias = dict[str, Any]
JsonList: TypeAlias = list[Any]

PROJECT_DIR: Path = Path(__file__).parent.resolve()
DEFAULT_OUTPUT_DIR: Path = PROJECT_DIR / "output"
CHANGELOG_FILE: Path = PROJECT_DIR / "CHANGELOG.md"

API_BASE: str = "https://api.modrinth.com/v2"
DEFAULT_PROJECT_ID: str = "7AXsRqp1"
TOKEN_ENV: str = "MODRINTH_TOKEN"
USER_AGENT: str = "CTNStudio/Extra-Recipe/1.0 (https://github.com/CTNStudio/Extra-Recipe)"
REQUEST_TIMEOUT: int = 60
MAX_RETRIES: int = 3

# pack_format 前缀 -> Modrinth game_versions（取自现有 4.0.x 发布真实值。
# 新增 MC 版本时需与 packed.py 的 PACK_FORMAT_MAP 及 README 版本表同步更新）。
GAME_VERSIONS_MAP: dict[str, tuple[str, ...]] = {
    "4": ("1.13", "1.13.1", "1.13.2", "1.14", "1.14.1", "1.14.2", "1.14.3", "1.14.4"),
    "5": ("1.15", "1.15.1", "1.15.2", "1.16", "1.16.1"),
    "6": ("1.16.2", "1.16.3", "1.16.4", "1.16.5"),
    "7": ("1.17", "1.17.1"),
    "8": ("1.18", "1.18.1"),
    "9": ("1.18.2",),
    "10": ("1.19", "1.19.1", "1.19.2", "1.19.3"),
    "12": ("1.19.4",),
    "15": ("1.20", "1.20.1"),
    "18": ("1.20.2",),
    "26": ("1.20.3", "1.20.4"),
    "41": ("1.20.5", "1.20.6"),
    "48": ("1.21", "1.21.1"),
    "57": ("1.21.2", "1.21.3"),
    "61": ("1.21.4",),
    "71": ("1.21.5",),
    "80": ("1.21.6",),
    "81": ("1.21.7", "1.21.8"),
    "88.0": ("1.21.9", "1.21.10"),
    "94.1": ("1.21.11",),
    "101.1": ("26.1", "26.1.1", "26.1.2"),
    "107.1": ("26.2",),
}

# 前缀按长度降序排列，保证最长匹配优先（如 101.1 不会被误判为 10）。
PREFIXES_BY_LENGTH: tuple[str, ...] = tuple[str, ...](sorted(GAME_VERSIONS_MAP, key=len, reverse=True))

JAR_LOADERS: tuple[str, ...] = ("fabric", "forge", "neoforge", "quilt")
DATAPACK_LOADERS: tuple[str, ...] = ("datapack",)

# 模组版本号约定为纯数字点分形式（语义化版本，不含连字符）。
_VERSION_PATTERN: re.Pattern[str] = re.compile(r"^\d+(?:\.\d+)+$")
# CHANGELOG.md 中版本段落的标题行（用于截取段落结尾）。
_HEADING_PATTERN: re.Pattern[str] = re.compile(r"^## ", flags=re.MULTILINE)


class ModrinthError(Exception):
    """Modrinth API 请求失败（含 HTTP 状态码与官方错误信息）。"""

    def __init__(self, status: int, error: str, description: str) -> None:
        self.status: int = status
        self.error: str = error
        self.description: str = description
        super().__init__(f"HTTP {status}: {error} - {description}")


class Logger:
    """控制台日志（Windows GBK 终端安全，emoji 编码失败时 ASCII 兜底）。"""

    def _emit(self, message: str) -> None:
        try:
            print(message)
        except UnicodeEncodeError:
            print(message.encode(encoding="ascii", errors="replace").decode(encoding="ascii"))

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


@dataclass(frozen=True)
class Artifact:
    """一个待上传的构建产物及其 Modrinth version 元数据。"""

    path: Path  # 产物文件完整路径
    kind: str  # "datapack" | "all_loader"
    mod_ver: str  # 模组版本号，如 "4.0.4"
    prefix: str  # pack_format 前缀，如 "107.1"
    version_number: str  # datapack 与 jar 相同: f"{mod_ver}-{prefix}"
    name: str  # jar: version_number；datapack: f"[Datapack]Extra Recipe-{mod_ver}-{prefix}"
    game_versions: tuple[str, ...]  # 来自 GAME_VERSIONS_MAP[prefix]
    loaders: tuple[str, ...]  # datapack: ("datapack",)；jar: 四个 loader


# --------------------------------------------------------------------------- #
# 产物发现
# --------------------------------------------------------------------------- #
def _parse_filename(filename: str) -> tuple[str, str, str] | None:
    """解析产物文件名，返回 (kind, mod_ver, prefix)；无法解析时返回 None。"""
    for kind, marker, suffix in (
        ("datapack", "[Datapack]Extra Recipe-", ".zip"),
        ("all_loader", "[All Loader]Extra Recipe-", ".jar"),
    ):
        if not (filename.startswith(marker) and filename.endswith(suffix)):
            continue
        rest: str = filename[len(marker):-len(suffix)]
        for prefix in PREFIXES_BY_LENGTH:
            if not rest.endswith(f"-{prefix}"):
                continue
            mod_ver: str = rest[: -(len(prefix) + 1)]
            if _VERSION_PATTERN.fullmatch(mod_ver):
                return kind, mod_ver, prefix
        return None  # 属于本类产物但版本号无法解析
    return None


def discover_artifacts(output_dir: Path, log: Logger) -> list[Artifact]:
    """扫描输出目录，解析产物文件名并组装 Artifact；非法文件跳过并警告。"""
    artifacts: list[Artifact] = []
    for filepath in sorted(output_dir.glob(pattern="*")):
        if not filepath.is_file() or filepath.suffix not in (".zip", ".jar"):
            continue
        parsed: tuple[str, str, str] | None = _parse_filename(filepath.name)
        if parsed is None:
            log.skip(message=f"无法解析文件名，跳过: {filepath.name}")
            continue
        kind, mod_ver, prefix = parsed
        # datapack 与 jar 共用 version_number（如 4.0.4-41），靠 loaders/name 区分。
        version_number: str = f"{mod_ver}-{prefix}"
        if kind == "datapack":
            name: str = f"[Datapack]Extra Recipe-{mod_ver}-{prefix}"
            loaders: tuple[str, ...] = DATAPACK_LOADERS
        else:
            name = version_number
            loaders = JAR_LOADERS
        artifacts.append(
            Artifact(
                path=filepath,
                kind=kind,
                mod_ver=mod_ver,
                prefix=prefix,
                version_number=version_number,
                name=name,
                game_versions=GAME_VERSIONS_MAP[prefix],
                loaders=loaders,
            )
        )
    return artifacts


# --------------------------------------------------------------------------- #
# changelog 提取
# --------------------------------------------------------------------------- #
def extract_changelog(mod_ver: str) -> str:
    """从 CHANGELOG.md 提取 `## v{mod_ver}` 段落；未找到返回空字符串。"""
    if not CHANGELOG_FILE.exists():
        return ""
    text: str = CHANGELOG_FILE.read_text(encoding="utf-8")
    match: Match[str] | None = re.search(
        pattern=rf"^## v{re.escape(pattern=mod_ver)}\s*$", string=text, flags=re.MULTILINE
    )
    if match is None:
        return ""
    next_heading: Match[str] | None = _HEADING_PATTERN.search(text, match.end())
    end: int = next_heading.start() if next_heading is not None else len(text)
    return text[match.end():end].strip()


_changelog_cache: dict[str, str] = {}


def changelog_for(mod_ver: str) -> str:
    """按模组版本号缓存并返回 CHANGELOG 段落。"""
    if mod_ver not in _changelog_cache:
        _changelog_cache[mod_ver] = extract_changelog(mod_ver=mod_ver)
    return _changelog_cache[mod_ver]


# --------------------------------------------------------------------------- #
# 元数据组装
# --------------------------------------------------------------------------- #
def build_version_meta(
    artifact: Artifact, project_id: str, changelog: str, version_type: str
) -> JsonObject:
    """组装 Modrinth create version 接口的 data JSON。"""
    return {
        "project_id": project_id,
        "name": artifact.name,
        "version_number": artifact.version_number,
        "game_versions": list[str](artifact.game_versions),
        "loaders": list[str](artifact.loaders),
        "version_type": version_type,
        "changelog": changelog,
        # 以下字段为服务端 serde 反序列化必填项（缺失时按顺序报
        # "missing field `...`"），即便 OpenAPI 文档未全部标为 required。
        "dependencies": [],   # 本项目无运行时依赖，恒为空数组
        "featured": False,    # 不置顶推荐
        "status": "listed",   # 立即可见
        "file_parts": ["file"],
        "primary_file": "file",
    }


# --------------------------------------------------------------------------- #
# API 客户端
# --------------------------------------------------------------------------- #
def _build_multipart(data: JsonObject, file_path: Path, boundary: str) -> bytes:
    """手工构造 multipart/form-data 请求体（data 字段 JSON + 文件字段）。"""
    data_bytes: bytes = json.dumps(obj=data, ensure_ascii=False).encode(encoding="utf-8")
    file_bytes: bytes = file_path.read_bytes()
    content_type: str = "application/zip" if file_path.suffix == ".zip" else "application/java-archive"

    chunks: list[bytes] = [
        f"--{boundary}\r\n".encode(encoding="utf-8"),
        b'Content-Disposition: form-data; name="data"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        data_bytes,
        b"\r\n",
        f"--{boundary}\r\n".encode(encoding="utf-8"),
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'.encode(encoding="utf-8"),
        f"Content-Type: {content_type}\r\n\r\n".encode(encoding="utf-8"),
        file_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode(encoding="utf-8"),
    ]
    return b"".join(chunks)


def _parse_retry_after(value: str | None) -> float:
    """解析 Retry-After 头（秒数或 HTTP 日期），解析失败默认 5 秒。"""
    if value is None:
        return 5.0
    try:
        return max(float(value), 1.0)
    except ValueError:
        return 5.0


def _parse_error_body(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """解析 HTTPError 响应体中的 error / description 字段；失败返回通用信息。"""
    try:
        body_text: str = exc.read().decode(encoding="utf-8", errors="replace")
        data: JsonValue = json.loads(s=body_text)
    except Exception:  # noqa: BLE001 - 仅影响错误提示的可读性
        return f"HTTP {exc.code}", str(exc.reason)
    if not isinstance(data, dict):
        return f"HTTP {exc.code}", str(exc.reason)
    error_name: Any = data.get("error")
    description: Any = data.get("description")
    return (
        str(error_name) if error_name is not None else f"HTTP {exc.code}",
        str(description) if description is not None else str(exc.reason),
    )


class ModrinthClient:
    """Modrinth v2 API 客户端（纯标准库，手工构造 multipart 上传）。"""

    def __init__(self, token: str | None, log: Logger) -> None:
        self._token: str | None = token
        self._log: Logger = log

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        """发送请求并返回 (status, body)；429 按 Retry-After 头重试。"""
        request_headers: dict[str, str] = {"User-Agent": USER_AGENT}
        if headers:
            request_headers.update(headers)
        retries: int = 0
        while True:
            req: urllib.request.Request = urllib.request.Request(
                url=url, data=body, headers=request_headers, method=method
            )
            try:
                with urllib.request.urlopen(url=req, timeout=REQUEST_TIMEOUT) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and retries < MAX_RETRIES:
                    retries += 1
                    wait_seconds: float = _parse_retry_after(value=exc.headers.get("Retry-After"))
                    self._log.warning(
                        message=f"触发限流 (HTTP 429)，{wait_seconds:.0f} 秒后重试 ({retries}/{MAX_RETRIES})"
                    )
                    time.sleep(wait_seconds)
                    continue
                error_name, description = _parse_error_body(exc)
                raise ModrinthError(
                    status=exc.code, error=error_name, description=description
                ) from exc
            except urllib.error.URLError as exc:
                raise ModrinthError(status=0, error="NetworkError", description=str(exc.reason)) from exc

    def get_existing_version_numbers(self, project_id: str) -> dict[str, list[frozenset[str]]]:
        """查询项目已有版本（只读，无需认证）。

        返回 version_number -> 该号已使用过的 loaders 集合列表。同一
        version_number 可能有多个 version（如 datapack 与 jar 共用），
        因此按 (version_number, loaders) 组合判断是否已上传。
        """
        _, body = self._request(method="GET", url=f"{API_BASE}/project/{project_id}/version")
        data: JsonValue = json.loads(s=body.decode(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Modrinth 版本列表接口返回了非数组数据")
        existing: dict[str, list[frozenset[str]]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            number: Any = item.get("version_number")
            loaders_value: Any = item.get("loaders")
            if not isinstance(number, str) or not isinstance(loaders_value, list):
                continue
            loaders_set: frozenset[str] = frozenset[str](str(l) for l in loaders_value)
            existing.setdefault(number, []).append(loaders_set)
        return existing

    def create_version(self, data: JsonObject, file_path: Path) -> JsonObject:
        """上传产物并创建 Modrinth version，返回服务端响应 JSON。"""
        boundary: str = secrets.token_hex(nbytes=16)
        body: bytes = _build_multipart(data=data, file_path=file_path, boundary=boundary)
        headers: dict[str, str] = {
            "Authorization": self._token or "",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        _, resp_body = self._request(
            method="POST", url=f"{API_BASE}/version", headers=headers, body=body
        )
        response: JsonValue = json.loads(s=resp_body.decode(encoding="utf-8"))
        if not isinstance(response, dict):
            raise ValueError("Modrinth 创建版本接口返回了非对象数据")
        return response


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _prefix_sort_key(prefix: str) -> float:
    """按 pack_format 前缀数值排序（支持小数前缀如 88.0）。"""
    try:
        return float(prefix)
    except ValueError:
        return 0.0


def print_error_hint(log: Logger, exc: ModrinthError) -> None:
    """根据状态码输出可操作的上传失败提示。"""
    if exc.status == 401:
        log.error(message="   → 认证失败: 请检查 token 是否有效，且 scope 包含 VERSION_CREATE")
    elif exc.status == 403:
        log.error(message="   → 权限不足: 请确认该账号对项目有上传权限")
    elif exc.status == 400:
        log.error(message="   → 请求校验失败: 请检查 game_versions / loaders 是否合法")
    elif exc.status == 404:
        log.error(message="   → 资源不存在: 请检查 --project-id 是否正确")
    elif exc.status == 0:
        log.error(message="   → 网络错误: 请检查网络连接后重试")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace, token: str | None, log: Logger) -> int:
    """执行上传主流程，返回进程退出码（0 成功 / 1 失败）。"""
    output_dir: Path = args.output_dir
    if not output_dir.is_dir():
        log.error(message=f"输出目录不存在: {output_dir}")
        return 1

    log.info(message=f"📁 扫描目录: {output_dir}")
    artifacts: list[Artifact] = discover_artifacts(output_dir=output_dir, log=log)
    if args.type:
        filtered: list[Artifact] = [a for a in artifacts if a.kind == args.type]
        log.info(message=f"按类型过滤 '{args.type}': {len(filtered)}/{len(artifacts)} 个产物")
        artifacts = filtered
    if not artifacts:
        log.error(message="未发现可上传的产物，请先运行 packed.py 打包")
        return 1

    # 同一 (类型, 模组版本, 前缀) 只保留一个产物（防御性去重，正常打包不会重复）。
    unique: dict[tuple[str, str, str], Artifact] = {}
    for artifact in artifacts:
        key: tuple[str, str, str] = (artifact.kind, artifact.mod_ver, artifact.prefix)
        if key in unique:
            log.skip(message=f"重复产物 {artifact.path.name}，仅保留 {unique[key].path.name}")
            continue
        unique[key] = artifact
    artifacts = sorted(unique.values(), key=lambda a: _prefix_sort_key(a.prefix))

    client: ModrinthClient = ModrinthClient(token=token, log=log)
    log.info(message="")
    log.info(message="📡 查询 Modrinth 已有版本 ...")
    try:
        existing: dict[str, list[frozenset[str]]] = client.get_existing_version_numbers(
            project_id=args.project_id
        )
        total_versions: int = sum(len(loaders_list) for loaders_list in existing.values())
        log.info(message=f"   已获取 {total_versions} 个已有版本")
    except ModrinthError as exc:
        log.error(message=f"查询已有版本失败: {exc}")
        if exc.status == 404:
            log.error(message=f"   项目 '{args.project_id}' 不存在，请检查 --project-id")
        return 1
    except Exception as exc:  # noqa: BLE001 - 查询失败不应中断整体
        log.error(message=f"查询已有版本失败: {exc}")
        return 1

    success_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    total: int = len(artifacts)

    for idx, artifact in enumerate[Artifact](artifacts, start=1):
        log.info(message="")
        log.info(message=f"[{idx}/{total}] {artifact.path.name}")
        log.info(message=f"   version_number: {artifact.version_number}")
        log.info(message=f"   name: {artifact.name}")
        log.info(message=f"   game_versions: {', '.join(artifact.game_versions)}")
        log.info(message=f"   loaders: {', '.join(artifact.loaders)}")

        loaders_set: frozenset[str] = frozenset[str](artifact.loaders)
        if any(loaders_set == ls for ls in existing.get(artifact.version_number, [])):
            log.skip(
                message=f"version_number '{artifact.version_number}' 的相同 loaders 已存在于 Modrinth，跳过"
            )
            skipped_count += 1
            continue

        if args.dry_run:
            log.info(message="   [dry-run] 该版本将上传（未发送请求）")
            success_count += 1
            continue

        changelog: str = changelog_for(artifact.mod_ver)
        data: JsonObject = build_version_meta(
            artifact=artifact,
            project_id=args.project_id,
            changelog=changelog,
            version_type=args.version_type,
        )
        try:
            response: JsonObject = client.create_version(data=data, file_path=artifact.path)
            version_id: str = str(response.get("id", ""))
            log.success(
                message=f"上传成功: {artifact.version_number}"
                + (f" (id: {version_id})" if version_id else "")
            )
            success_count += 1
        except ModrinthError as exc:
            log.error(message=f"上传失败: {artifact.version_number}")
            log.error(message=f"   {exc}")
            print_error_hint(log=log, exc=exc)
            failed_count += 1

    log.info(message="")
    log.info(message="=" * 60)
    if args.dry_run:
        log.info(message="📋 dry-run 预览完成（未发送任何上传请求）")
    else:
        log.info(message="🎉 上传任务完成！")
    log.info(message=f"   成功: {success_count}")
    log.info(message=f"   跳过: {skipped_count}")
    log.info(message=f"   失败: {failed_count}")
    log.info(message="=" * 60)
    return 1 if failed_count > 0 else 0


def build_parser() -> ArgumentParser:
    """构建命令行参数解析器。"""
    parser: ArgumentParser = argparse.ArgumentParser(
        description="Extra Recipe Modrinth 自动上传脚本 - 将 output/ 下的构建产物发布到 Modrinth",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例用法:\n"
            "  python upload_modrinth.py --dry-run          预览将上传/跳过的内容\n"
            "  python upload_modrinth.py                   上传全部产物\n"
            "  python upload_modrinth.py --type datapack   仅上传 Datapack zip\n"
            "  python upload_modrinth.py --token <PAT>     显式传入 token\n"
        ),
    )
    _ = parser.add_argument(
        "--output-dir", "-d", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"产物目录 (默认: {DEFAULT_OUTPUT_DIR})",
    )
    _ = parser.add_argument(
        "--project-id", type=str, default=DEFAULT_PROJECT_ID,
        help=f"Modrinth 项目 ID 或 slug (默认: {DEFAULT_PROJECT_ID})",
    )
    _ = parser.add_argument(
        "--type", "-t", choices=["datapack", "all_loader"],
        help="仅上传指定类型的产物",
    )
    _ = parser.add_argument(
        "--version-type", choices=["release", "beta", "alpha"], default="release",
        help="版本类型 (默认: release)",
    )
    _ = parser.add_argument(
        "--dry-run", action="store_true",
        help="预览模式：查询已有版本但不上传",
    )
    _ = parser.add_argument(
        "--token", type=str, default=None,
        help=f"Modrinth PAT（默认读取环境变量 {TOKEN_ENV}，建议使用环境变量）",
    )
    return parser


def main() -> None:
    log: Logger = Logger()
    parser: ArgumentParser = build_parser()
    args: argparse.Namespace = parser.parse_args()

    token: str | None = args.token if args.token else os.environ.get(TOKEN_ENV)
    if not args.dry_run and token is None:
        log.error(message=f"未提供 Modrinth token！请设置环境变量 {TOKEN_ENV} 或使用 --token 传入")
        sys.exit(1)

    log.info(message="=" * 60)
    log.info(message="Extra Recipe Modrinth 上传工具")
    log.info(message=f"  时间: {datetime.now().strftime(format='%Y-%m-%d %H:%M:%S')}")
    log.info(message=f"  模式: {'dry-run 预览' if args.dry_run else '实际上传'}")
    log.info(message=f"  项目: {args.project_id}")
    log.info(message="=" * 60)

    sys.exit(run(args=args, token=token, log=log))


if __name__ == "__main__":
    main()
