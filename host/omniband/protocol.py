"""protocol.py - NDJSON codec for the OmniBand V2 link (protocol/PROTOCOL.md).

Device -> host lines carry k=hello|tele|evt|ges|scope|cal|ack|pong|diag.
Host -> device lines are the command set in constants.json host_commands.
All timestamps the device emits are int64 microseconds (esp_timer).
"""
from __future__ import annotations

import json
from typing import Any, Optional

MAX_LINE = 256


def loads(line: str | bytes) -> Optional[dict[str, Any]]:
    """Parse one NDJSON line. Returns None on malformed/oversized input."""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8", "replace")
        except Exception:
            return None
    line = line.strip()
    if not line or len(line) > MAX_LINE or "\x00" in line:
        return None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":")) + "\n"


# ------------------------------------------------------------ host -> device
def cmd_set_mode(mode: int, cmd_id: str = "h") -> str:
    return dumps({"k": "set_mode", "id": cmd_id, "mode": int(mode)})


def cmd_set_thr(ch: int, on: float, off: Optional[float] = None,
                cmd_id: str = "h") -> str:
    o: dict[str, Any] = {"k": "set_thr", "id": cmd_id, "ch": int(ch), "on": on}
    if off is not None:
        o["off"] = off
    return dumps(o)


def cmd_recal(what: str, cmd_id: str = "h") -> str:
    return dumps({"k": "recal", "id": cmd_id, "what": what})


def cmd_cal_step(step: str, cmd_id: str = "h") -> str:
    return dumps({"k": "cal_step", "id": cmd_id, "step": step})


def cmd_set_cfg(key: str, val: Any, cmd_id: str = "h") -> str:
    return dumps({"k": "set_cfg", "id": cmd_id, "key": key, "val": val})


def cmd_save(cmd_id: str = "h") -> str:
    return dumps({"k": "save", "id": cmd_id})


def cmd_defaults(cmd_id: str = "h") -> str:
    return dumps({"k": "defaults", "id": cmd_id})


def cmd_scope(on: bool, rate: int = 2, cmd_id: str = "h") -> str:
    return dumps({"k": "scope", "id": cmd_id, "on": 1 if on else 0,
                  "rate": int(rate)})


def cmd_ping(th: int, cmd_id: str = "h") -> str:
    return dumps({"k": "ping", "id": cmd_id, "th": int(th)})


def cmd_set_baud(baud: int, cmd_id: str = "h") -> str:
    return dumps({"k": "set_baud", "id": cmd_id, "baud": int(baud)})


def cmd_night(on: bool, cmd_id: str = "h") -> str:
    return dumps({"k": "night", "id": cmd_id, "on": 1 if on else 0})


def cmd_sos_cancel(cmd_id: str = "h") -> str:
    return dumps({"k": "sos_cancel", "id": cmd_id})


def cmd_oled_msg(text: str, ms: int = 2000, cmd_id: str = "h") -> str:
    return dumps({"k": "oled_msg", "id": cmd_id, "text": text[:20], "ms": ms})


def cmd_diag(cmd_id: str = "h") -> str:
    return dumps({"k": "diag", "id": cmd_id})


def cmd_hello() -> str:
    return dumps({"k": "hello"})


# ------------------------------------------------------------ device -> host
def parse_tele(obj: dict) -> Optional[dict]:
    """Normalize a tele line; None if shape is wrong."""
    e = obj.get("e")
    if not (isinstance(e, list) and len(e) == 4):
        return None
    return {
        "t": int(obj.get("t", 0)),
        "m": int(obj.get("m", 0)),
        "env": [float(x) for x in e],
        "s": int(obj.get("s", 0)),
        "st": str(obj.get("st", "")),
    }


def parse_evt(obj: dict) -> Optional[dict]:
    if obj.get("ch") not in (1, 2, 3, 4):
        return None
    ty = obj.get("ty")
    if ty not in ("down", "up", "veto"):
        return None
    return {
        "t": int(obj.get("t", 0)),
        "ch": int(obj["ch"]),
        "ty": ty,
        "a": float(obj.get("a", 0)),
        "d": int(obj.get("d", 0)),
        "veto": bool(obj.get("veto", 0)),
    }


def parse_ges(obj: dict) -> Optional[dict]:
    g = obj.get("g")
    if not isinstance(g, str) or not g:
        return None
    return {
        "t": int(obj.get("t", 0)),
        "g": g,
        "a": float(obj.get("a", 0)),
        "d": int(obj.get("d", 0)),
        "m": int(obj.get("m", 0)),
    }


class SeqDeduper:
    """Window dedupe for device seq numbers (kLinkSeqWindow=256)."""

    def __init__(self, window: int = 256) -> None:
        self.window = window
        self._seen: set[int] = set()
        self._order: list[int] = []

    def fresh(self, seq: int) -> bool:
        s = int(seq)
        if s in self._seen:
            return False
        self._seen.add(s)
        self._order.append(s)
        if len(self._order) > self.window * 2:
            for old in self._order[: self.window]:
                self._seen.discard(old)
            self._order = self._order[self.window :]
        return True
