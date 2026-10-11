from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import Settings


class SeatApi:
    def __init__(self, settings: Settings, cookies: dict[str, str], user_info: dict[str, Any]):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            cookies=cookies,
            timeout=settings.request_timeout_seconds,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json;charset=UTF-8",
                "Origin": settings.base_url.rstrip("/"),
                "Referer": settings.base_url.rstrip("/") + "/",
                "lan": "1",
                "token": user_info["token"],
            },
        )
        self.user_info = user_info

    async def close(self) -> None:
        await self.client.aclose()

    def target_date(self) -> date:
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if self.settings.target_date.lower() == "today":
            return today
        if self.settings.target_date.lower() == "tomorrow":
            return today + timedelta(days=1)
        return datetime.strptime(self.settings.target_date, "%Y-%m-%d").date()

    async def _json(self, response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        data = response.json()
        if data.get("code") == 300:
            raise RuntimeError("登录已失效，请重新扫码登录")
        return data

    async def open_scope(self) -> dict[str, Any]:
        response = await self.client.get(
            "/ic-web/seatRoom/openScope",
            params={"roomId": self.settings.room_id},
        )
        return await self._json(response)

    async def seats(self, target: date | None = None) -> dict[str, Any]:
        target = target or self.target_date()
        response = await self.client.get(
            "/ic-web/reserve",
            params={
                "roomIds": self.settings.room_id,
                "resvDates": target.strftime("%Y%m%d"),
                "sysKind": 8,
            },
        )
        return await self._json(response)

    async def areas(self) -> dict[str, Any]:
        """Return the live floor/area tree used by the library frontend."""
        response = await self.client.get(
            "/ic-web/seatMenu",
        )
        return await self._json(response)

    async def device_tips(self, dev_id: int, begin_time: str, resv_rule: dict | None = None) -> dict[str, Any]:
        """Query time-slot availability tips for a specific device.

        ``resv_rule`` should be the ``resvRule`` object from the seat data so
        that ``classKind`` and ``resvRuleId`` are read from the server rather
        than being hard-coded.
        """
        rule = resv_rule or {}
        class_kind = rule.get("classKind") or 8
        rule_id = rule.get("ruleId") or 8
        response = await self.client.get(
            "/ic-web/device/tips",
            params={
                "devId": dev_id,
                "classKind": class_kind,
                "resvRuleId": rule_id,
                "chooseBeginTime": begin_time,
                "isConsole": "false",
            },
        )
        return await self._json(response)

    async def reserve(self, dev_id: int, begin: str, end: str) -> dict[str, Any]:
        acc_no = self.user_info.get("accNo")
        if acc_no is None:
            raise RuntimeError("当前用户信息中没有 accNo")
        payload = {
            "sysKind": 8,
            "appAccNo": acc_no,
            "memberKind": 1,
            "resvMember": [acc_no],
            "resvBeginTime": begin,
            "resvEndTime": end,
            "testName": "",
            "captcha": "",
            "resvProperty": 0,
            "resvDev": [dev_id],
            "memo": "",
        }
        response = await self.client.post("/ic-web/reserve", json=payload)
        return await self._json(response)

    async def my_reservations(
        self, day: date, page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        """Query the signed-in user's reservations, using the site's profile API."""
        response = await self.client.get(
            "/ic-web/reserve/resvInfo",
            params={
                "beginDate": day.isoformat(),
                "endDate": day.isoformat(),
                # The site's "my reservations" view selects these status bits by default.
                "needStatus": 2 + 4 + 128 + 256 + 8192,
                "page": page,
                "pageNum": page_size,
                "orderKey": "gmt_create",
                "orderModel": "desc",
            },
        )
        return await self._json(response)

    async def cancel_reservation(self, uuid: str) -> dict[str, Any]:
        response = await self.client.post("/ic-web/reserve/delete", json={"uuid": uuid})
        return await self._json(response)


def _to_minutes(value: str) -> int:
    hour, minute = (int(part) for part in value[:5].split(":"))
    return hour * 60 + minute


def _to_hhmm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def normalize_area_nodes(data: Any) -> list[dict[str, Any]]:
    """Normalize the site's area response without assuming one exact payload shape."""
    id_keys = ("roomId", "areaId", "spaceId", "kindId", "id")
    name_keys = ("roomName", "areaName", "spaceName", "name", "title")
    child_keys = ("children", "childList", "rooms", "areas", "list", "data")
    seen: set[str] = set()

    def first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return value
        return None

    def walk(value: Any, parent_label: str = "") -> list[dict[str, Any]]:
        if isinstance(value, list):
            result: list[dict[str, Any]] = []
            for item in value:
                result.extend(walk(item, parent_label))
            return result
        if not isinstance(value, dict):
            return []

        raw_id = first_value(value, id_keys)
        raw_name = first_value(value, name_keys)
        own_name = str(raw_name) if raw_name is not None else parent_label
        children: list[dict[str, Any]] = []
        for key in child_keys:
            child_value = value.get(key)
            if child_value is not None and child_value is not value:
                children.extend(walk(child_value, own_name))

        result: list[dict[str, Any]] = []
        if raw_id is not None and raw_name is not None:
            node_id = str(raw_id)
            if node_id not in seen:
                seen.add(node_id)
                label = f"{parent_label} / {raw_name}" if parent_label and parent_label != str(raw_name) else str(raw_name)
                result.append({"id": raw_id, "name": str(raw_name), "label": label})
        result.extend(children)
        return result

    normalized = walk(data)
    return normalized


def build_time_options(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Return valid start/end choices for one seat from server rules."""
    rule = item.get("resvRule") or {}
    interval = max(1, int(rule.get("timeInterval") or 5))
    min_duration = max(1, int(rule.get("minResvTime") or 30))
    max_duration = max(min_duration, int(rule.get("maxResvTime") or 930))
    options: list[dict[str, Any]] = []

    for opening in item.get("openTimes") or []:
        start_bound = _to_minutes(opening["openStartTime"])
        end_bound = _to_minutes(opening["openEndTime"])
        start = start_bound
        while start + min_duration <= end_bound:
            end_limit = min(end_bound, start + max_duration)
            ends = [
                _to_hhmm(end)
                for end in range(start + min_duration, end_limit + 1, interval)
            ]
            if ends:
                options.append({"start": _to_hhmm(start), "ends": ends})
            start += interval
    return options


def is_valid_time_range(item: dict[str, Any], start: str, end: str) -> bool:
    # Reservation paths receive a normalized seat view, while direct callers
    # may pass the original /reserve item. Reuse the options already computed
    # for the view so both paths enforce the same time range.
    options = item.get("time_options")
    if options is None:
        options = build_time_options(item)
    return any(option["start"] == start and end in option["ends"] for option in options)


def build_seat_view(data: dict[str, Any], preferred_names: list[str]) -> list[dict[str, Any]]:
    result = []
    by_name = {item.get("devName"): item for item in data.get("data", [])}
    for name in preferred_names:
        item = by_name.get(name)
        if item:
            result.append(
                {
                    "dev_name": item.get("devName"),
                    "dev_id": item.get("devId"),
                    "room_id": item.get("roomId"),
                    "room_name": item.get("roomName"),
                    "open_state": item.get("openState"),
                    "dev_status": item.get("devStatus"),
                    "open_times": item.get("openTimes", []),
                    "reservations": item.get("resvInfo", []),
                    "reservation_rule": item.get("resvRule"),
                    "time_options": build_time_options(item),
                }
            )
    return result
