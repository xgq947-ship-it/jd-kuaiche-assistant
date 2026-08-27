"""自动更新检查。

与 reverse-prompt / AI-Video-Canvas 一致，发布走 GitHub Releases：
打 ``vX.Y.Z`` 标签 → Actions 构建产物 → ``gh release create``。

本模块只负责**检查并告知**，不静默替换正在运行的程序：自动改写运行中的
二进制在 macOS 上会破坏代码签名与公证。UI 拿到结果后引导用户下载安装包。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from jdka.version import __version__

SOURCE_REPO = "xgq947-ship-it/jd-kuaiche-assistant"

# 发布源必须是**公开**仓库：买家的客户端不带任何凭据，
# 私有仓库的 GitHub API 对匿名请求一律返回 404，更新检查会永远拿不到数据。
# 源码可以继续私有 —— 把安装包发布到一个只放产物、不放源码的公开仓库即可。
RELEASE_REPO = SOURCE_REPO

REPO = RELEASE_REPO  # 兼容旧引用
LATEST_API = f"https://api.github.com/repos/{RELEASE_REPO}/releases/latest"
RELEASES_API = f"https://api.github.com/repos/{RELEASE_REPO}/releases"
RELEASES_PAGE = f"https://github.com/{RELEASE_REPO}/releases/latest"
TIMEOUT = 8

_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _SEMVER.search(text or "")
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    return a > b


@dataclass
class UpdateInfo:
    current: str
    latest: str | None = None
    available: bool = False
    url: str = RELEASES_PAGE
    notes: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest,
            "available": self.available,
            "url": self.url,
            "notes": self.notes[:2000],
            "error": self.error,
        }


def check(current: str = __version__) -> UpdateInfo:
    """查询最新 Release。网络不可用时安静降级，绝不阻断主流程。"""
    request = urllib.request.Request(
        LATEST_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"jd-kuaiche-assistant/{current}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # 私有仓库对匿名请求同样返回 404，和「真的没发过版本」无法区分。
            # 不要只说「尚未发布」——那会让买家以为是正常状态，实际是配置错了。
            return UpdateInfo(
                current=current,
                error=f"读不到发布信息（{RELEASE_REPO} 可能是私有仓库或尚未发布版本）",
            )
        return UpdateInfo(current=current, error=f"检查更新失败：HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 - 更新检查永远不该让应用崩
        return UpdateInfo(current=current, error=f"检查更新失败：{type(exc).__name__}")

    tag = str(payload.get("tag_name") or "")
    return UpdateInfo(
        current=current,
        latest=tag or None,
        available=is_newer(tag, current),
        url=str(payload.get("html_url") or RELEASES_PAGE),
        notes=str(payload.get("body") or ""),
    )


def _asset_for_platform(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """挑当前平台的安装包。带上 GitHub 的 SHA-256，供外壳校验后再安装。"""
    want_windows = sys.platform.startswith("win")
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        matched = name.endswith((".exe", ".msi")) if want_windows else name.endswith(".dmg")
        if not matched:
            continue
        return {
            "name": asset.get("name"),
            "url": asset.get("browser_download_url"),
            "size": asset.get("size"),
            "digest": asset.get("digest"),
        }
    return None


def list_releases(limit: int = 12) -> list[dict[str, Any]]:
    """设置界面的「新功能」列表：正式版本 + 更新说明 + 当前平台安装包。"""
    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"jd-kuaiche-assistant/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 拿不到列表不影响应用使用
        return []
    if not isinstance(payload, list):
        return []

    releases: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        tag = str(item.get("tag_name") or "")
        assets = [a for a in (item.get("assets") or []) if isinstance(a, dict)]
        releases.append(
            {
                "tag": tag,
                "name": str(item.get("name") or tag),
                "published_at": str(item.get("published_at") or "")[:10],
                "notes": [
                    line.lstrip("-*# ").strip()
                    for line in str(item.get("body") or "").splitlines()
                    if line.strip()
                ][:12],
                "url": str(item.get("html_url") or RELEASES_PAGE),
                "newer": is_newer(tag, __version__),
                "asset": _asset_for_platform(assets),
            }
        )
        if len(releases) >= limit:
            break
    return releases
