"""授权码：Ed25519 签名 + 设备绑定的永久授权，离线验证。

签名契约（照抄 AI 画布的做法，破坏它会让验签在「看起来都对」的情况下悄悄失败）：
    签名的对象是 payload 的 **base64url 字符串本身** 的 ASCII 字节，
    不是重新序列化的 JSON。验签前绝不 decode 再 encode。

设备哈希：``SHA-256(installation_id + device_secret + NAMESPACE)``。
不使用硬件序列号 / MAC / 硬盘序列号 —— 这类标识既涉及隐私，也会因换网卡、
虚拟机等情况漂移。两个随机值都只存在本机，从不上传。

能做到什么、做不到什么（不要高估）：
- **能**：别人无法伪造授权码（没有私钥）；一份授权码换台机器就失效。
- **不能**：拿到二进制的人有可能把校验逻辑整段改掉。任何纯本地校验都
  绕不开这一点，除非把核心功能放到服务端。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from jdka.config import app_dir

# 签发方公钥（SPKI DER，base64url）。私钥只在作者手中，绝不进仓库。
PUBLIC_KEY_B64URL = "MCowBQYDK2VwAyEAarwH3ZWzYs9rGHb28XEko15Qf7MH0NxDrRUUdsvkqR4"

NAMESPACE = "jd-kuaiche-assistant-license-v1"
KEY_PREFIX = "JDKA1"
LICENSE_VERSION = 1


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


# --------------------------------------------------------------------------
# 设备身份
# --------------------------------------------------------------------------

def identity_path() -> Path:
    """身份文件与 config.json 分开存放，避免「重置配置」把授权一起弄丢。"""
    return app_dir() / "identity.json"


def _load_identity() -> dict[str, str]:
    path = identity_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("installation_id") and data.get("device_secret"):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    data = {
        "installation_id": str(uuid.uuid4()),
        "device_secret": secrets.token_hex(32),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return data


def device_hash() -> str:
    """本机设备码。买家把它发给作者，作者据此签发授权码。"""
    data = _load_identity()
    material = data["installation_id"] + data["device_secret"] + NAMESPACE
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def format_device_code(value: str | None = None) -> str:
    """分组显示，便于人工核对；复制时仍用原始值。"""
    raw = value or device_hash()
    return "-".join(raw[i : i + 8] for i in range(0, len(raw), 8))


# --------------------------------------------------------------------------
# 授权码
# --------------------------------------------------------------------------

@dataclass
class LicenseStatus:
    licensed: bool
    reason: str = ""
    note: str = ""
    issued: str = ""
    device_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "licensed": self.licensed,
            "reason": self.reason,
            "note": self.note,
            "issued": self.issued,
            "device_code": self.device_code or format_device_code(),
        }


def license_path() -> Path:
    return app_dir() / "license.key"


def encode_payload(payload: dict[str, Any]) -> str:
    """签发方唯一入口；签名的就是本函数的返回值本身。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return b64url_encode(canonical.encode("utf-8"))


def build_key(payload_b64url: str, signature_b64url: str) -> str:
    return f"{KEY_PREFIX}.{payload_b64url}.{signature_b64url}"


def verify(key: str, *, expected_device: str | None = None) -> LicenseStatus:
    """离线验证授权码。任何异常一律判为无效，不抛错。"""
    device = expected_device or device_hash()
    text = (key or "").strip().replace("\n", "").replace(" ", "")
    if not text:
        return LicenseStatus(False, "尚未激活", device_code=format_device_code(device))

    parts = text.split(".")
    if len(parts) != 3 or parts[0] != KEY_PREFIX:
        return LicenseStatus(False, "授权码格式不正确", device_code=format_device_code(device))
    _, payload_b64url, signature_b64url = parts

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.serialization import load_der_public_key

        public_key = load_der_public_key(b64url_decode(PUBLIC_KEY_B64URL))
        # 关键：对 base64url 字符串的字节验签，不要 decode 后重新序列化。
        public_key.verify(
            b64url_decode(signature_b64url),
            payload_b64url.encode("ascii"),
        )
    except InvalidSignature:
        return LicenseStatus(False, "授权码签名无效", device_code=format_device_code(device))
    except Exception:
        return LicenseStatus(False, "授权码无法验证", device_code=format_device_code(device))

    # 验签通过之后才读字段。
    try:
        payload = json.loads(b64url_decode(payload_b64url).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return LicenseStatus(False, "授权码内容损坏", device_code=format_device_code(device))

    if not isinstance(payload, dict) or int(payload.get("v") or 0) != LICENSE_VERSION:
        return LicenseStatus(False, "授权码版本不受支持", device_code=format_device_code(device))
    if str(payload.get("device") or "") != device:
        return LicenseStatus(
            False,
            "该授权码属于另一台设备",
            device_code=format_device_code(device),
        )

    return LicenseStatus(
        True,
        note=str(payload.get("note") or ""),
        issued=str(payload.get("issued") or ""),
        device_code=format_device_code(device),
    )


def load_status() -> LicenseStatus:
    path = license_path()
    if not path.exists():
        return LicenseStatus(False, "尚未激活", device_code=format_device_code())
    try:
        return verify(path.read_text(encoding="utf-8"))
    except OSError:
        return LicenseStatus(False, "授权文件无法读取", device_code=format_device_code())


def activate(key: str) -> LicenseStatus:
    """校验通过才落盘，避免把无效授权码存下来。"""
    status = verify(key)
    if status.licensed:
        path = license_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(key.strip(), encoding="utf-8")
        os.replace(tmp, path)
    return status


def issue(
    *,
    device: str,
    private_key_pem: bytes,
    note: str = "",
    issued: str | None = None,
) -> str:
    """签发一份永久授权码。**只在作者机器上调用**，需要私钥。"""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    device = device.strip().replace("-", "").lower()
    if len(device) != 64 or not all(c in "0123456789abcdef" for c in device):
        raise ValueError("设备码必须是 64 位十六进制（可含分组连字符）")

    payload = {
        "v": LICENSE_VERSION,
        "device": device,
        "issued": issued or date.today().isoformat(),
        "perpetual": True,
    }
    if note:
        payload["note"] = note

    payload_b64url = encode_payload(payload)
    key = load_pem_private_key(private_key_pem, password=None)
    signature = key.sign(payload_b64url.encode("ascii"))
    return build_key(payload_b64url, b64url_encode(signature))
