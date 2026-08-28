"""核心契约测试。

重点覆盖两件事：

1. **计划状态取值不能再被写反**。上游项目曾因文档写反导致「新建计划一直
   暂停」，而读写两侧同错互相印证、日志全绿。这里用**语义断言**（"启用"
   对应哪个数字）把它钉死。
2. **删除是不可逆动作**，出厂默认必须是暂停而非删除。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from jdka.config import AppConfig, SkuConfig
from jdka.core.naming import build_plan_name, is_tool_managed, parse_plan_name
from jdka.core.policy import PLAN_STATUS_ENABLED, PLAN_STATUS_PAUSED, decide
from jdka.core.state import empty_state, normalize_state
from jdka.jd.report import normalize_plan
from jdka.jd.shared import (
    CAMPAIGN_LIST_ENDPOINT,
    CAMPAIGN_STATUS_ENDPOINT,
    PLAN_OPERATE_ENABLE,
    PLAN_OPERATE_PAUSE,
    PlanNameMismatchError,
    PlanNotToolManagedError,
)
from jdka.jd.plan import JdKuaichePlanService

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)
PLAN_ID = 9358535675
NAME = "AUTO_976513_0827160818_R002"


# --------------------------------------------------------------------------
# 1. 状态取值语义（反转 bug 的回归防线）
# --------------------------------------------------------------------------


def test_jd_status_semantics_are_inverted_from_intuition() -> None:
    """京准通：2 = 启用，1 = 暂停。与字面直觉相反，不要"顺手改回去"。"""
    assert PLAN_STATUS_ENABLED == 2
    assert PLAN_STATUS_PAUSED == 1
    assert PLAN_OPERATE_ENABLE == 2
    assert PLAN_OPERATE_PAUSE == 1


def test_normalize_plan_marks_status_two_as_enabled() -> None:
    enabled = normalize_plan({"campaignId": PLAN_ID, "campaignName": NAME, "status": 2})
    paused = normalize_plan({"campaignId": PLAN_ID, "campaignName": NAME, "status": 1})
    assert enabled is not None and paused is not None
    assert enabled["enabled"] is True
    assert paused["enabled"] is False


def test_policy_asks_to_enable_only_when_plan_is_paused() -> None:
    def plan(status: int) -> dict[str, Any]:
        return {
            "plan_id": "1",
            "plan_name": NAME,
            "status": status,
            "created_at": (NOW - timedelta(minutes=90)).isoformat(),
        }

    config = {
        "order_criteria": {"field": "total_order_cnt", "threshold": 1},
        "min_observe_minutes": 30,
        "max_rotations_per_day": 3,
        "max_daily_spend": 500,
    }
    state = {"spend_today": 0, "rotations_today": 0}

    paused = decide(config=config, state=state, current_plan=plan(PLAN_STATUS_PAUSED),
                    metrics={}, now=NOW)
    assert paused["action"] == "enable"

    running = decide(config=config, state=state, current_plan=plan(PLAN_STATUS_ENABLED),
                     metrics={}, now=NOW)
    assert running["action"] != "enable"


class FakeTransport:
    """只实现 post；刻意不提供 capture_create_body，以走程序化组装。"""

    def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    def post(self, endpoint: str, body: dict[str, Any], *, write: bool = False) -> dict[str, Any]:
        self.calls.append((endpoint, copy.deepcopy(body), write))
        return self.responses[endpoint].pop(0)


def _envelope(data: Any) -> dict[str, Any]:
    return {"code": 1, "data": data, "msg": "成功", "system": {"loginPin": "pin"}}


def _rows(status: int) -> dict[str, Any]:
    return _envelope(
        {
            "data": [{"campaignId": PLAN_ID, "campaignName": NAME, "status": status, "budget": 50}],
            "ext": {},
            "paginator": {"items": 1},
        }
    )


def test_enable_sends_operate_type_two_not_one() -> None:
    """这正是当年的 bug：发了 1（暂停）却断言 status==1，两处同错自证成功。"""
    transport = FakeTransport(
        {
            CAMPAIGN_LIST_ENDPOINT: [_rows(PLAN_STATUS_PAUSED), _rows(PLAN_STATUS_ENABLED)],
            CAMPAIGN_STATUS_ENDPOINT: [_envelope({"success": True})],
        }
    )
    result = JdKuaichePlanService(transport).set_enabled(
        plan_id=PLAN_ID, expected_plan_name=NAME, enabled=True, execute=True
    )
    writes = [b for e, b, w in transport.calls if e == CAMPAIGN_STATUS_ENDPOINT and w]
    assert len(writes) == 1, "写请求必须且只能发一次"
    assert writes[0]["operateType"] == PLAN_OPERATE_ENABLE == 2
    assert result["enabled"] is True
    assert result["after_status"] == PLAN_STATUS_ENABLED


def test_disable_sends_operate_type_one() -> None:
    transport = FakeTransport(
        {
            CAMPAIGN_LIST_ENDPOINT: [_rows(PLAN_STATUS_ENABLED), _rows(PLAN_STATUS_PAUSED)],
            CAMPAIGN_STATUS_ENDPOINT: [_envelope({"success": True})],
        }
    )
    JdKuaichePlanService(transport).set_enabled(
        plan_id=PLAN_ID, expected_plan_name=NAME, enabled=False, execute=True
    )
    writes = [b for e, b, w in transport.calls if e == CAMPAIGN_STATUS_ENDPOINT and w]
    assert writes[0]["operateType"] == PLAN_OPERATE_PAUSE == 1


# --------------------------------------------------------------------------
# 2. 危险动作护栏
# --------------------------------------------------------------------------


def test_delete_is_not_the_factory_default() -> None:
    """交付给第三方时，不可逆动作不能是默认行为。"""
    assert AppConfig().rotate_mode == "pause"


def test_invalid_rotate_mode_falls_back_to_pause(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JDKA_HOME", str(tmp_path))
    cfg = AppConfig(rotate_mode="delete")
    cfg.save()
    monkeypatch.setattr("jdka.config.json.loads", lambda _s: {"rotate_mode": "obliterate"})
    assert AppConfig.load().rotate_mode == "pause"


def test_refuses_to_touch_plans_it_does_not_manage() -> None:
    transport = FakeTransport({CAMPAIGN_LIST_ENDPOINT: [
        _envelope({"data": [{"campaignId": PLAN_ID, "campaignName": "老板手建的计划", "status": 2}],
                   "ext": {}, "paginator": {"items": 1}})]})
    with pytest.raises((PlanNameMismatchError, PlanNotToolManagedError)):
        JdKuaichePlanService(transport).set_enabled(
            plan_id=PLAN_ID, expected_plan_name="老板手建的计划", enabled=False, execute=True
        )
    assert not [c for c in transport.calls if c[2]], "不得对非本工具计划发出任何写请求"


def test_name_mismatch_blocks_delete() -> None:
    transport = FakeTransport({CAMPAIGN_LIST_ENDPOINT: [_rows(PLAN_STATUS_ENABLED)]})
    with pytest.raises(PlanNameMismatchError):
        JdKuaichePlanService(transport).delete(
            plan_id=PLAN_ID, expected_plan_name="AUTO_976513_0000000000_R999", execute=True
        )
    assert not [c for c in transport.calls if c[2]]


def test_preview_never_writes() -> None:
    # 预览态的 delete 仍会读取最终指标快照，因此需要多份只读响应。
    transport = FakeTransport(
        {CAMPAIGN_LIST_ENDPOINT: [_rows(PLAN_STATUS_ENABLED) for _ in range(3)]}
    )
    result = JdKuaichePlanService(transport).delete(
        plan_id=PLAN_ID, expected_plan_name=NAME, execute=False
    )
    assert result["deleted"] is False
    assert not [c for c in transport.calls if c[2]]


# --------------------------------------------------------------------------
# 3. 命名与状态
# --------------------------------------------------------------------------


def test_plan_names_are_tool_scoped_and_roundtrip() -> None:
    name = build_plan_name("10214914976513", 2, now=datetime(2026, 8, 27, 16, 8, 18))
    assert name.startswith("AUTO_976513_")
    assert is_tool_managed(name)
    assert parse_plan_name(name)["round"] == 2
    assert not is_tool_managed("老板手建的计划")


def test_state_resets_daily_counters_on_new_day() -> None:
    state = empty_state()
    state.update({"business_date": "2026-08-26", "rotations_today": 3, "spend_today": 480.0})
    fresh = normalize_state(state)
    assert fresh["rotations_today"] == 0
    assert fresh["spend_today"] == 0.0


def test_sku_config_id_is_stable() -> None:
    assert SkuConfig(sku_id="10214914976513").config_id == "JD_KC_10214914976513"


# --------------------------------------------------------------------------
# 4. 控制面跨域（桌面外壳里页面源是 tauri://localhost）
# --------------------------------------------------------------------------


def test_only_tauri_origins_are_allowed_cross_origin() -> None:
    """放行必须是白名单，绝不能是通配符 —— 否则任意网页都能打本地控制面。"""
    from jdka.server import ALLOWED_ORIGINS

    assert "tauri://localhost" in ALLOWED_ORIGINS
    assert "http://tauri.localhost" in ALLOWED_ORIGINS
    assert "*" not in ALLOWED_ORIGINS
    assert not any(o.startswith("http://localhost") for o in ALLOWED_ORIGINS)


# --------------------------------------------------------------------------
# 5. 授权码
# --------------------------------------------------------------------------


def _issued_key(device: str, note: str = "") -> str:
    """用一次性密钥对签发，测试不依赖作者的真私钥。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    spki = priv.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    from jdka import license as lic

    lic.PUBLIC_KEY_B64URL = lic.b64url_encode(spki)  # 指向本次测试的公钥
    return lic.issue(device=device, private_key_pem=pem, note=note)


def test_valid_key_activates_and_carries_note(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JDKA_HOME", str(tmp_path))
    from jdka import license as lic

    device = lic.device_hash()
    status = lic.verify(_issued_key(device, "老王"), expected_device=device)
    assert status.licensed is True
    assert status.note == "老王"


def test_key_from_another_device_is_rejected(tmp_path, monkeypatch) -> None:
    """授权码换台机器必须失效，否则一份码可以到处传。"""
    monkeypatch.setenv("JDKA_HOME", str(tmp_path))
    from jdka import license as lic

    key = _issued_key("a" * 64)
    assert lic.verify(key, expected_device="b" * 64).licensed is False


def test_tampered_payload_is_rejected(tmp_path, monkeypatch) -> None:
    """改 payload 而不重签必须验不过 —— 这正是签名要防的事。"""
    monkeypatch.setenv("JDKA_HOME", str(tmp_path))
    from jdka import license as lic

    device = lic.device_hash()
    key = _issued_key(device)
    prefix, payload_b64, signature = key.split(".")
    forged_payload = lic.encode_payload(
        {"v": 1, "device": device, "issued": "2099-01-01", "perpetual": True, "note": "白嫖"}
    )
    forged = f"{prefix}.{forged_payload}.{signature}"
    assert lic.verify(forged, expected_device=device).licensed is False


def test_garbage_keys_never_activate(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JDKA_HOME", str(tmp_path))
    from jdka import license as lic

    for bad in ["", "   ", "not-a-key", "JDKA1.abc", "JDKA1.a.b.c", "OTHER.a.b"]:
        assert lic.verify(bad).licensed is False


def test_invalid_key_is_not_persisted(tmp_path, monkeypatch) -> None:
    """无效码不能落盘，否则下次启动会读到一份垃圾授权文件。"""
    monkeypatch.setenv("JDKA_HOME", str(tmp_path))
    from jdka import license as lic

    assert lic.activate("JDKA1.bogus.bogus").licensed is False
    assert not lic.license_path().exists()


def test_device_hash_is_stable_and_uses_no_hardware_ids(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JDKA_HOME", str(tmp_path))
    from jdka import license as lic

    first = lic.device_hash()
    assert first == lic.device_hash(), "同一台机器上必须稳定"
    assert len(first) == 64
    identity = json.loads(lic.identity_path().read_text(encoding="utf-8"))
    assert set(identity) == {"installation_id", "device_secret"}


def test_monitor_refuses_to_start_without_license(tmp_path, monkeypatch) -> None:
    """纵深防御：只拦 HTTP 网关不够，真正开始花钱的入口要独立校验。"""
    monkeypatch.setenv("JDKA_HOME", str(tmp_path))
    from jdka.config import AppConfig, SkuConfig
    from jdka.service import MonitorService

    service = MonitorService()
    service.config = AppConfig(skus=[SkuConfig(sku_id="10214914976513")])
    result = service.start()
    assert result["ok"] is False
    assert "激活" in result["message"]
    assert service._running is False


def test_release_repo_must_be_public_for_updates_to_work() -> None:
    """发布源写错会让买家永远收不到更新，且症状是「看起来正常」的 404。

    这里只能钉住「Rust 下载白名单与 Python 检查源指向同一个仓库」——
    仓库是否公开要靠部署时确认，代码断言不了。
    """
    from pathlib import Path

    from jdka.update import RELEASE_REPO

    rust = Path(__file__).resolve().parent.parent / "src-tauri/src/updater.rs"
    assert f'"{RELEASE_REPO}"' in rust.read_text(encoding="utf-8"), (
        "updater.rs 的下载白名单必须与 update.py 的 RELEASE_REPO 一致"
    )


def test_auto_rotate_is_off_by_default(tmp_path, monkeypatch) -> None:
    """自动轮换会真实花钱，不能在用户「只是打开看一眼」时悄悄开始。"""
    monkeypatch.setenv("JDKA_HOME", str(tmp_path))
    assert AppConfig().auto_rotate is False


def test_auto_rotate_survives_save_and_load(tmp_path, monkeypatch) -> None:
    """曾经声明了却没接线：存进去读不出来等于开关是假的。"""
    monkeypatch.setenv("JDKA_HOME", str(tmp_path))
    cfg = AppConfig(auto_rotate=True)
    cfg.save()
    assert AppConfig.load().auto_rotate is True
