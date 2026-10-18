"""AutoCat 录制文件的读写（JSON / XML）。

两种格式共享同一份数据模型：

    {
        "version": 1,
        "created_at": "2024-01-01T12:00:00",
        "meta": {"source": "gui", "screen": "1920x1080"},
        "events": [
            {"t": 0.0,    "type": "key_press",   "key": "a"},
            {"t": 0.12,   "type": "key_release", "key": "a"},
            {"t": 0.30,   "type": "mouse_move",  "x": 100, "y": 200},
            {"t": 0.50,   "type": "mouse_click", "x": 100, "y": 200,
             "button": "left", "pressed": true},
            {"t": 0.90,   "type": "mouse_scroll", "x": 100, "y": 200,
             "dx": 0, "dy": -1}
        ]
    }

JSON 直接序列化该结构；XML 把事件字段写成属性：

    <autocat version="1" created_at="...">
      <meta source="gui"/>
      <events count="5">
        <event t="0.0" type="key_press" key="a"/>
        ...
      </events>
    </autocat>
"""

from __future__ import annotations

import json
import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Iterable, List, Optional

log = logging.getLogger("autocat.storage")

#: 数据模型版本，将来结构不兼容时递增
SCHEMA_VERSION = 1

#: 事件字段的类型表，用于 XML 读写时做字符串 <-> 原生类型转换
FIELD_TYPES = {
    "t": float,
    "type": str,
    "key": str,
    "x": int,
    "y": int,
    "dx": int,
    "dy": int,
    "button": str,
    "pressed": bool,
}

#: 录制文件支持的扩展名
FORMATS = ("json", "xml")


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def detect_format(path: str, default: str = "json") -> str:
    """根据扩展名判断格式；无法判断时返回 ``default``。"""
    ext = os.path.splitext(str(path))[1].lower().lstrip(".")
    return ext if ext in FORMATS else default


def _guess_format_from_content(text: str) -> str:
    """内容嗅探：以 ``<`` 开头当作 XML，否则当 JSON。"""
    return "xml" if text.lstrip()[:1] == "<" else "json"


def _coerce(field: str, value):
    """按 :data:`FIELD_TYPES` 把字符串转成原生类型。"""
    kind = FIELD_TYPES.get(field)
    if kind is None or isinstance(value, kind):
        return value
    if kind is bool:
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if kind is float:
        return float(value)
    if kind is int:
        return int(float(value))
    return str(value)


def _coerce_event(raw: dict) -> dict:
    return {k: _coerce(k, v) for k, v in raw.items()}


# ---------------------------------------------------------------------------
# 序列化：JSON
# ---------------------------------------------------------------------------
def _dumps_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _loads_json(text: str) -> dict:
    data = json.loads(text)
    return _normalize(payload=data)


# ---------------------------------------------------------------------------
# 序列化：XML
# ---------------------------------------------------------------------------
def _dumps_xml(payload: dict) -> str:
    root = ET.Element(
        "autocat",
        attrib={
            "version": str(payload.get("version", SCHEMA_VERSION)),
            "created_at": str(payload.get("created_at", "")),
        },
    )

    meta = payload.get("meta") or {}
    if meta:
        meta_el = ET.SubElement(root, "meta")
        for key, value in meta.items():
            ET.SubElement(meta_el, str(key)).text = "" if value is None else str(value)

    events = payload.get("events") or []
    events_el = ET.SubElement(root, "events", attrib={"count": str(len(events))})
    for event in events:
        attrib = {}
        for key, value in event.items():
            if isinstance(value, bool):
                attrib[key] = "true" if value else "false"
            else:
                attrib[key] = str(value)
        ET.SubElement(events_el, "event", attrib=attrib)

    try:  # Python 3.9+
        ET.indent(root, space="  ")
    except AttributeError:  # pragma: no cover - 老版本解释器
        pass

    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + body


def _loads_xml(text: str) -> dict:
    root = ET.fromstring(text)
    if root.tag != "autocat":
        raise ValueError(f"不是 AutoCat 录制文件（根节点为 <{root.tag}>）")

    meta_el = root.find("meta")
    meta = {}
    if meta_el is not None:
        for child in meta_el:
            meta[child.tag] = child.text or ""

    events: List[dict] = []
    events_el = root.find("events")
    if events_el is not None:
        for node in events_el.findall("event"):
            events.append(_coerce_event(dict(node.attrib)))

    return _normalize(
        payload={
            "version": root.get("version", SCHEMA_VERSION),
            "created_at": root.get("created_at", ""),
            "meta": meta,
            "events": events,
        }
    )


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
def _normalize(payload: dict) -> dict:
    """统一 payload 结构：补默认值、做类型转换、按时间排序。"""
    if isinstance(payload, list):  # 容错：纯事件数组
        payload = {"events": payload}

    events = [_coerce_event(dict(e)) for e in (payload.get("events") or [])]
    events.sort(key=lambda e: float(e.get("t", 0.0)))

    return {
        "version": int(float(payload.get("version", SCHEMA_VERSION) or SCHEMA_VERSION)),
        "created_at": str(payload.get("created_at") or ""),
        "meta": dict(payload.get("meta") or {}),
        "events": events,
    }


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def dumps(payload_or_events, fmt: str = "json", meta: Optional[dict] = None) -> str:
    """把事件列表（或完整 payload）序列化成字符串。"""
    payload = _as_payload(payload_or_events, meta)
    return _dumps_xml(payload) if fmt.lower() == "xml" else _dumps_json(payload)


def loads(text: str, fmt: Optional[str] = None) -> dict:
    """从字符串解析录制内容，返回归一化后的 payload。"""
    fmt = (fmt or _guess_format_from_content(text)).lower()
    return _loads_xml(text) if fmt == "xml" else _loads_json(text)


def _as_payload(payload_or_events, meta: Optional[dict] = None) -> dict:
    """允许传入「事件列表」或「完整 payload」。"""
    if isinstance(payload_or_events, dict) and "events" in payload_or_events:
        payload = dict(payload_or_events)
        if meta:
            payload["meta"] = {**payload.get("meta", {}), **meta}
        payload.setdefault("version", SCHEMA_VERSION)
        payload.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
        return payload

    return {
        "version": SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "meta": dict(meta or {}),
        "events": list(payload_or_events or []),
    }


def save_recording(
    path: str,
    events,
    meta: Optional[dict] = None,
    fmt: Optional[str] = None,
) -> dict:
    """把事件写入文件，返回实际保存的 payload。

    参数：
        path: 目标文件；扩展名决定格式（除非显式给出 ``fmt``）
        events: 事件列表，或完整的 payload dict
        meta: 附加元信息
        fmt: ``"json"`` / ``"xml"``，为 None 时按扩展名判断
    """
    fmt = (fmt or detect_format(path)).lower()
    payload = _as_payload(events, meta)
    text = _dumps_xml(payload) if fmt == "xml" else _dumps_json(payload)

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    log.info("已保存: %s（%d 条事件，%s）", path, len(payload["events"]), fmt.upper())
    return payload


def load_recording(path: str) -> dict:
    """读取录制文件，返回 ``{version, created_at, meta, events}``。"""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    payload = loads(text, fmt=detect_format(path, default=""))
    # 扩展名不可靠时（例如 .txt）上面的 detect 会给空串，交给内容嗅探兜底
    return payload


def recording_duration(events: Iterable[dict]) -> float:
    """录制时长（秒）= 最后一条事件的时间戳。"""
    last = 0.0
    for event in events:
        last = max(last, float(event.get("t", 0.0)))
    return last


def describe(path: str) -> dict:
    """读取文件并给出摘要，供界面列表展示。"""
    payload = load_recording(path)
    events = payload["events"]
    return {
        "path": path,
        "name": os.path.basename(path),
        "format": detect_format(path),
        "count": len(events),
        "duration": recording_duration(events),
        "created_at": payload.get("created_at", ""),
        "meta": payload.get("meta", {}),
        "size": os.path.getsize(path) if os.path.exists(path) else 0,
    }


def format_duration(seconds: float) -> str:
    """把秒数格式化成 ``mm:ss`` 或 ``h:mm:ss``。"""
    seconds = max(0.0, float(seconds))
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
