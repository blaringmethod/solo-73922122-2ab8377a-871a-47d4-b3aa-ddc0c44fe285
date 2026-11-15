"""刷卡流水按停靠事实关联。

规则
----
* 站点为线路级共享输入；定位按 ``trip_id``（趟号）列分组，每组独立求停靠事实。
* 仅使用状态为“事实”（fact）的确定停靠，取其到/离站定位点时刻构成闭区间。
* 同一趟号下，刷卡时刻落在任一事实闭区间 ``[到站, 离站]`` 内即归该站；
  事实区间按线路顺序互不相交，故一条流水最多命中一个站。
* 未命中任何确定停靠闭区间（含定位中不存在的趟号、该趟无事实、时刻在区间外）
  一律标记为未匹配。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .analysis import analyze
from .models import InputError, Position, Stop, Swipe, utc_iso
from .parsing import parse_station_csv, parse_swipe_csv, parse_trip_position_csv

UNMATCHED_ROUTE_INDEX: Optional[int] = None
UNMATCHED_STOP_NAME = ""
UNMATCHED_STATUS = "unmatched"
MATCHED_STATUS = "matched"


@dataclass(frozen=True)
class FactWindow:
    """单趟内一个确定停靠事实的闭区间窗口。"""

    route_index: int  # 1 基线路站序
    stop_name: str
    arrival: object  # datetime（UTC）
    departure: object  # datetime（UTC）


@dataclass(frozen=True)
class AssociationRow:
    """一条刷卡流水的关联结果。"""

    transaction_id: str
    trip_id: str
    swipe_time: str  # UTC ISO
    route_index: Optional[int]  # 1 基站序；未匹配为 None
    stop_name: str
    status: str  # matched | unmatched
    reason: str


def _group_by_trip(items: Sequence[Swipe] | Sequence[Position]) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for item in items:
        grouped.setdefault(item.trip_id, []).append(item)
    return grouped


def _facts_for_trip(
    stops: Sequence[Stop],
    positions: Sequence[Position],
    *,
    trip_id: str,
    warnings: list[str],
) -> list[FactWindow]:
    """求单趟确定停靠事实，返回按到站时刻排序的闭区间窗口列表。

    没有任何可用停靠（``analyze`` 抛 InputError）时该趟没有事实，
    调用方会把该趟全部刷卡标为未匹配，而不是让整次重算失败。
    """

    if not positions:
        return []
    try:
        result = analyze(stops, positions)
    except InputError as exc:
        warnings.append(f"趟号 {trip_id} 未形成确定停靠事实：{exc.message}")
        return []
    windows: list[FactWindow] = []
    for report in result.reports:
        if report.status != "fact":
            continue
        a, b = report.intervals[0]
        windows.append(
            FactWindow(
                route_index=report.stop.index + 1,
                stop_name=report.stop.name,
                arrival=result.positions[a].time,
                departure=result.positions[b].time,
            )
        )
    windows.sort(key=lambda window: window.arrival)
    return windows


def compute_associations(
    station_bytes: bytes,
    position_bytes: bytes,
    swipe_bytes: bytes,
    *,
    station_filename: str = "站点.csv",
    position_filename: str = "定位.csv",
    swipe_filename: str = "刷卡.csv",
) -> list[AssociationRow]:
    """解析三组输入并计算全部刷卡流水的归站结果。

    解析阶段的输入错误直接抛出 InputError；单趟无事实属于业务结果而非错误。
    """

    stops, _ = parse_station_csv(station_bytes, filename=station_filename)
    positions, _ = parse_trip_position_csv(position_bytes, filename=position_filename)
    swipes, _ = parse_swipe_csv(swipe_bytes, filename=swipe_filename)

    positions_by_trip = _group_by_trip(positions)

    warnings: list[str] = []
    facts_by_trip: dict[str, list[FactWindow]] = {}
    for trip_id, trip_positions in positions_by_trip.items():
        facts_by_trip[trip_id] = _facts_for_trip(
            stops, trip_positions, trip_id=trip_id, warnings=warnings
        )

    rows: list[AssociationRow] = []
    for swipe in swipes:
        facts = facts_by_trip.get(swipe.trip_id)
        if not facts:
            rows.append(
                AssociationRow(
                    transaction_id=swipe.transaction_id,
                    trip_id=swipe.trip_id,
                    swipe_time=utc_iso(swipe.time),
                    route_index=UNMATCHED_ROUTE_INDEX,
                    stop_name=UNMATCHED_STOP_NAME,
                    status=UNMATCHED_STATUS,
                    reason=(
                        f"趟号 {swipe.trip_id} 没有定位数据"
                        if facts is None
                        else f"趟号 {swipe.trip_id} 没有确定停靠事实"
                    ),
                )
            )
            continue

        hit: Optional[FactWindow] = None
        for window in facts:
            if window.arrival <= swipe.time <= window.departure:
                hit = window
                break
        if hit is None:
            rows.append(
                AssociationRow(
                    transaction_id=swipe.transaction_id,
                    trip_id=swipe.trip_id,
                    swipe_time=utc_iso(swipe.time),
                    route_index=UNMATCHED_ROUTE_INDEX,
                    stop_name=UNMATCHED_STOP_NAME,
                    status=UNMATCHED_STATUS,
                    reason="刷卡时刻不在任何确定停靠的到离站闭区间内",
                )
            )
        else:
            rows.append(
                AssociationRow(
                    transaction_id=swipe.transaction_id,
                    trip_id=swipe.trip_id,
                    swipe_time=utc_iso(swipe.time),
                    route_index=hit.route_index,
                    stop_name=hit.stop_name,
                    status=MATCHED_STATUS,
                    reason="",
                )
            )
    return rows
