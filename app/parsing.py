"""CSV 解析与输入校验。

支持 UTF-8（含 BOM）与 GB18030、逗号/分号/制表符分隔、常见中英文表头。
表头缺失时按固定列序回退解析，并在结果中给出提示。
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .models import (
    MAX_POINTS,
    MAX_STOPS,
    MAX_SWIPES,
    MAX_UPLOAD_BYTES,
    InputError,
    Position,
    Stop,
    Swipe,
)

ROLE_LABELS = {
    "name": "站名",
    "lat": "纬度",
    "lon": "经度",
    "radius": "半径（米）",
    "time": "UTC 时刻",
    "transaction_id": "流水号",
    "trip_id": "趟号",
}

_BRACKETS_RE = re.compile(r"[\s_\-\.·（）()\[\]【】{}]+")
_ANGLE_MARKS_RE = re.compile(r"[°º˚度\s]+")
_RADIUS_UNIT_RE = re.compile(r"(?i)\s*(?:m|米)\s*$")


@dataclass
class ParsedInput:
    stops: list[Stop]
    positions: list[Position]
    warnings: list[str] = field(default_factory=list)


def decode_bytes(data: bytes, *, label: str) -> str:
    """把上传字节解码为文本，拒绝空文件、二进制内容和超限文件。"""

    if data is None or len(data) == 0:
        raise InputError(f"{label}为空，请上传包含表头和数据的 CSV 文件。", code="empty_file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise InputError(
            f"{label}超过大小上限（{MAX_UPLOAD_BYTES // 1024} KiB）。",
            code="file_too_large",
        )
    if b"\x00" in data:
        raise InputError(f"{label}包含二进制内容，请上传文本 CSV 文件。", code="binary_file")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise InputError(
            f"{label}无法按 UTF-8 或 GB18030 解码，请另存为 UTF-8 CSV 后重试。",
            code="bad_encoding",
        )
    # ﻿ 可能出现在非开头的 BOM
    text = text.replace("﻿", "")
    if not text.strip():
        raise InputError(f"{label}只有空白字符，没有可解析的内容。", code="empty_file")
    return text


def _read_rows(text: str) -> list[tuple[int, list[str]]]:
    """按推断出的分隔符读取 CSV，返回 (行号, 单元格) 列表，跳过全空行。"""

    sample_lines = [line for line in text.splitlines() if line.strip()][:20]
    sample = "\n".join(sample_lines)
    dialect: type[csv.Dialect] | csv.Dialect
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows: list[tuple[int, list[str]]] = []
    try:
        reader = csv.reader(io.StringIO(text), dialect)
        for cells in reader:
            if not cells or all(not cell.strip() for cell in cells):
                continue
            rows.append((reader.line_num, cells))
    except csv.Error as exc:
        raise InputError(f"CSV 格式错误：{exc}", code="bad_csv") from exc
    return rows


def _normalize_header(cell: str) -> str:
    return _BRACKETS_RE.sub("", cell.strip().lower())


def _match_roles(cell: str) -> set[str]:
    """识别一个表头单元格可能扮演的角色；可能同时匹配多个（如“经纬度”）。"""

    norm = _normalize_header(cell)
    if not norm:
        return set()
    roles: set[str] = set()

    # 合并列（如“经纬度”“lat/lon”）必须拆成两列，标记为同时占用两个角色
    has_lat_hint = (
        "纬度" in norm or "纬" in norm or "lat" in norm or norm in {"y", "y坐标", "ycoord", "ycoordinate"}
    )
    has_lon_hint = (
        "经度" in norm
        or "经" in norm
        or "lon" in norm
        or "lng" in norm
        or "long" in norm
        or norm in {"x", "x坐标", "xcoord", "xcoordinate"}
    )
    if has_lat_hint and has_lon_hint:
        roles.update({"lat", "lon"})

    if (
        "时间" in norm
        or "时刻" in norm
        or "time" in norm
        or norm in {"utc", "datetime", "date", "ts", "timestamp"}
    ):
        roles.add("time")
    if "半径" in norm or "radius" in norm or norm in {"r", "米", "站圈"}:
        roles.add("radius")
    if "纬度" in norm or "lat" in norm or norm in {"y", "y坐标", "ycoord", "ycoordinate"}:
        roles.add("lat")
    if (
        "经度" in norm
        or "lon" in norm
        or "lng" in norm
        or "long" in norm
        or norm in {"x", "x坐标", "xcoord", "xcoordinate"}
    ):
        roles.add("lon")
    if (
        "站名" in norm
        or norm.endswith("名称")
        or "stopname" in norm
        or "stationname" in norm
        or norm in {"name", "stop", "station", "站点", "车站", "名称"}
    ):
        roles.add("name")
    if (
        "流水号" in norm
        or "交易号" in norm
        or "交易流水" in norm
        or "transactionid" in norm
        or "transid" in norm
        or "txnid" in norm
        or norm in {"id", "uuid", "流水", "交易编号", "流水编号"}
    ):
        roles.add("transaction_id")
    if (
        "趟号" in norm
        or "班次号" in norm
        or "车次号" in norm
        or "tripid" in norm
        or "tripno" in norm
        or "tripsn" in norm
        or norm in {"trip", "班次", "车次", "趟次"}
    ):
        roles.add("trip_id")
    return roles


def _resolve_columns(
    header: list[str],
    required: tuple[str, ...],
    *,
    label: str,
) -> tuple[dict[str, int], list[str]]:
    """把表头映射到角色列号；重复或缺失都视为输入错误。"""

    mapping: dict[str, int] = {}
    unknown: list[str] = []
    for idx, cell in enumerate(header):
        roles = _match_roles(cell)
        if not roles:
            if cell.strip():
                unknown.append(cell.strip())
            continue
        if len(roles) > 1:
            raise InputError(
                f"{label}第 {idx + 1} 列表头“{cell.strip()}”含义不明确，"
                "请把经度、纬度拆成两列。",
                code="ambiguous_header",
            )
        role = next(iter(roles))
        if role not in required:
            unknown.append(cell.strip())
            continue
        if role in mapping:
            raise InputError(
                f"{label}存在多个“{ROLE_LABELS[role]}”列（第 {mapping[role] + 1} 列与第 "
                f"{idx + 1} 列），请只保留一列。",
                code="duplicate_column",
            )
        mapping[role] = idx
    missing = [ROLE_LABELS[role] for role in required if role not in mapping]
    if missing:
        raise InputError(
            f"{label}缺少必需的列：{'、'.join(missing)}。",
            code="missing_column",
            details=[f"识别到的列：{', '.join(header)}"],
        )
    return mapping, unknown


def _looks_like_header(header: list[str]) -> bool:
    return any(_match_roles(cell) for cell in header)


def _row_get(cells: list[str], idx: int, *, label: str, line: int) -> str:
    if idx >= len(cells):
        raise InputError(
            f"{label}第 {line} 行缺少第 {idx + 1} 列数据。",
            code="row_too_short",
        )
    return cells[idx].strip()


def _parse_float(cell: str, *, field_label: str, label: str, line: int) -> float:
    raw = cell.strip()
    cleaned = _ANGLE_MARKS_RE.sub("", raw)
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    try:
        value = float(cleaned)
    except ValueError as exc:
        raise InputError(
            f"{label}第 {line} 行的{field_label}“{raw}”不是有效数字。",
            code="bad_number",
        ) from exc
    if not math.isfinite(value):
        raise InputError(
            f"{label}第 {line} 行的{field_label}“{raw}”必须是有限数字。",
            code="bad_number",
        )
    return value


def _parse_lat(cell: str, *, label: str, line: int) -> float:
    value = _parse_float(cell, field_label="纬度", label=label, line=line)
    if not (-90.0 <= value <= 90.0):
        raise InputError(
            f"{label}第 {line} 行纬度 {value:g} 超出 [-90, 90] 范围。",
            code="out_of_range",
        )
    return value


def _parse_lon(cell: str, *, label: str, line: int) -> float:
    value = _parse_float(cell, field_label="经度", label=label, line=line)
    if not (-180.0 <= value <= 180.0):
        raise InputError(
            f"{label}第 {line} 行经度 {value:g} 超出 [-180, 180] 范围。",
            code="out_of_range",
        )
    return value


def _parse_radius(cell: str, *, label: str, line: int) -> float:
    raw = cell.strip()
    cleaned = _RADIUS_UNIT_RE.sub("", _ANGLE_MARKS_RE.sub("", raw)).strip()
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    try:
        value = float(cleaned)
    except ValueError as exc:
        raise InputError(
            f"{label}第 {line} 行半径“{raw}”不是有效数字（单位：米）。",
            code="bad_number",
        ) from exc
    if not math.isfinite(value):
        raise InputError(
            f"{label}第 {line} 行半径“{raw}”必须是有限数字。",
            code="bad_number",
        )
    if value <= 0:
        raise InputError(
            f"{label}第 {line} 行半径必须大于 0 米，实际为 {value:g}。",
            code="out_of_range",
        )
    return value


_TIME_HINT = "例如 2026-11-15T08:30:00Z 或 2026-11-15 08:30:00Z"


def _parse_time(cell: str, *, label: str, line: int) -> tuple[datetime, bool]:
    """解析时刻，返回 (UTC aware datetime, 是否未带时区)。"""

    raw = cell.strip()
    if not raw:
        raise InputError(f"{label}第 {line} 行时刻为空。", code="bad_time")
    text = raw
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = text.replace("/", "-")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InputError(
            f"{label}第 {line} 行时刻“{raw}”无法解析（{_TIME_HINT}）。",
            code="bad_time",
        ) from exc
    naive = parsed.tzinfo is None
    if naive:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        try:
            parsed = parsed.astimezone(timezone.utc)
        except (OverflowError, OSError) as exc:
            raise InputError(
                f"{label}第 {line} 行时刻“{raw}”超出可处理范围。",
                code="bad_time",
            ) from exc
    return parsed, naive


def _trim_trailing_empty(cells: list[str]) -> list[str]:
    out = list(cells)
    while out and not out[-1].strip():
        out.pop()
    return out


def parse_station_csv(data: bytes, *, filename: str = "站点文件") -> tuple[list[Stop], list[str]]:
    label = f"站点文件（{filename}）"
    text = decode_bytes(data, label=label)
    rows = _read_rows(text)
    if not rows:
        raise InputError(f"{label}没有表头或数据行。", code="empty_file")

    required = ("name", "lat", "lon", "radius")
    _, header_cells = rows[0]
    warnings: list[str] = []
    if _looks_like_header(header_cells):
        mapping, unknown = _resolve_columns(header_cells, required, label=label)
        data_rows = rows[1:]
        if unknown:
            warnings.append(f"{label}忽略了无关列：{'、'.join(unknown)}。")
        indices = [mapping[role] for role in required]
    else:
        data_rows = rows
        mapping = {role: idx for idx, role in enumerate(required)}
        indices = list(range(len(required)))
        if len(header_cells) < len(required):
            raise InputError(
                f"{label}未识别到表头，且首行列数不足 {len(required)} 列。"
                "请提供表头：站名, 纬度, 经度, 半径米。",
                code="missing_header",
            )
        warnings.append(
            f"{label}未识别到表头，已按固定列序（站名, 纬度, 经度, 半径米）解析。"
        )

    if not data_rows:
        raise InputError(f"{label}只有表头，没有站点数据行。", code="no_data")

    stops: list[Stop] = []
    seen_names: dict[str, int] = {}
    max_idx = max(indices)
    for offset, (line, raw_cells) in enumerate(data_rows, start=1):
        cells = _trim_trailing_empty(raw_cells)
        if len(cells) <= max_idx:
            raise InputError(
                f"{label}第 {line} 行只有 {len(cells)} 列，"
                f"至少需要 {max_idx + 1} 列。",
                code="row_too_short",
            )
        name = _row_get(cells, mapping["name"], label=label, line=line)
        if not name:
            raise InputError(f"{label}第 {line} 行站名为空。", code="empty_name")
        lat = _parse_lat(_row_get(cells, mapping["lat"], label=label, line=line), label=label, line=line)
        lon = _parse_lon(_row_get(cells, mapping["lon"], label=label, line=line), label=label, line=line)
        radius = _parse_radius(
            _row_get(cells, mapping["radius"], label=label, line=line), label=label, line=line
        )
        if name in seen_names:
            warnings.append(
                f"{label}存在同名站点“{name}”（第 {seen_names[name]} 行与第 {line} 行），"
                "已按线路顺序区分为不同站点。"
            )
        else:
            seen_names[name] = line
        stops.append(Stop(index=len(stops), name=name, lat=lat, lon=lon, radius_m=radius))
        if len(stops) > MAX_STOPS:
            raise InputError(
                f"{label}包含超过 {MAX_STOPS} 个站点（最多 {MAX_STOPS} 站）。",
                code="too_many_stops",
            )

    if not stops:
        raise InputError(f"{label}没有有效站点。", code="no_data")
    return stops, warnings


def parse_position_csv(data: bytes, *, filename: str = "定位文件") -> tuple[list[Position], list[str]]:
    label = f"定位文件（{filename}）"
    text = decode_bytes(data, label=label)
    rows = _read_rows(text)
    if not rows:
        raise InputError(f"{label}没有表头或数据行。", code="empty_file")

    required = ("time", "lat", "lon")
    _, header_cells = rows[0]
    warnings: list[str] = []
    if _looks_like_header(header_cells):
        mapping, unknown = _resolve_columns(header_cells, required, label=label)
        data_rows = rows[1:]
        if unknown:
            warnings.append(f"{label}忽略了无关列：{'、'.join(unknown)}。")
        indices = [mapping[role] for role in required]
    else:
        data_rows = rows
        mapping = {role: idx for idx, role in enumerate(required)}
        indices = list(range(len(required)))
        if len(header_cells) < len(required):
            raise InputError(
                f"{label}未识别到表头，且首行列数不足 {len(required)} 列。"
                "请提供表头：时刻, 纬度, 经度。",
                code="missing_header",
            )
        warnings.append(
            f"{label}未识别到表头，已按固定列序（时刻, 纬度, 经度）解析。"
        )

    if not data_rows:
        raise InputError(f"{label}只有表头，没有定位数据行。", code="no_data")
    if len(data_rows) > MAX_POINTS:
        raise InputError(
            f"{label}包含 {len(data_rows)} 个定位点，超过单趟上限 {MAX_POINTS} 个。",
            code="too_many_points",
        )

    positions: list[Position] = []
    max_idx = max(indices)
    naive_seen = False
    for (line, raw_cells) in data_rows:
        cells = _trim_trailing_empty(raw_cells)
        if len(cells) <= max_idx:
            raise InputError(
                f"{label}第 {line} 行只有 {len(cells)} 列，至少需要 {max_idx + 1} 列。",
                code="row_too_short",
            )
        time, naive = _parse_time(
            _row_get(cells, mapping["time"], label=label, line=line), label=label, line=line
        )
        naive_seen = naive_seen or naive
        lat = _parse_lat(_row_get(cells, mapping["lat"], label=label, line=line), label=label, line=line)
        lon = _parse_lon(_row_get(cells, mapping["lon"], label=label, line=line), label=label, line=line)

        if positions:
            previous = positions[-1]
            if time == previous.time:
                raise InputError(
                    f"{label}第 {line} 行时刻与上一行相同（{_format_time(time)}），"
                    "定位时刻必须严格递增。",
                    code="time_not_increasing",
                )
            if time < previous.time:
                raise InputError(
                    f"{label}第 {line} 行时刻 {_format_time(time)} 早于上一行 "
                    f"{_format_time(previous.time)}，定位时刻必须严格递增。",
                    code="time_not_increasing",
                )
        positions.append(
            Position(index=len(positions), time=time, lat=lat, lon=lon, raw_time=cells[mapping["time"]].strip())
        )

    if not positions:
        raise InputError(f"{label}没有有效定位点。", code="no_data")
    if naive_seen:
        warnings.append(f"{label}存在未带时区的时刻，已按 UTC 解释。")
    if len(positions) < 2:
        warnings.append(f"{label}只有 1 个定位点，无法形成“连续至少两条”的停靠。")
    return positions, warnings


def _format_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def parse_trip_position_csv(
    data: bytes, *, filename: str = "定位文件"
) -> tuple[list[Position], list[str]]:
    """解析带趟号的定位 CSV（按趟号归站流程专用）。

    必需列：趟号、UTC 时刻、纬度、经度。同一趟号内时刻必须严格递增；
    不同趟号分别校验顺序。总行数上限沿用 MAX_POINTS。
    """

    label = f"定位文件（{filename}）"
    text = decode_bytes(data, label=label)
    rows = _read_rows(text)
    if not rows:
        raise InputError(f"{label}没有表头或数据行。", code="empty_file")

    required = ("trip_id", "time", "lat", "lon")
    _, header_cells = rows[0]
    warnings: list[str] = []
    if _looks_like_header(header_cells):
        mapping, unknown = _resolve_columns(header_cells, required, label=label)
        data_rows = rows[1:]
        if unknown:
            warnings.append(f"{label}忽略了无关列：{'、'.join(unknown)}。")
        indices = [mapping[role] for role in required]
    else:
        data_rows = rows
        mapping = {role: idx for idx, role in enumerate(required)}
        indices = list(range(len(required)))
        if len(header_cells) < len(required):
            raise InputError(
                f"{label}未识别到表头，且首行列数不足 {len(required)} 列。"
                "请提供表头：趟号, 时刻, 纬度, 经度。",
                code="missing_header",
            )
        warnings.append(
            f"{label}未识别到表头，已按固定列序（趟号, 时刻, 纬度, 经度）解析。"
        )

    if not data_rows:
        raise InputError(f"{label}只有表头，没有定位数据行。", code="no_data")
    if len(data_rows) > MAX_POINTS:
        raise InputError(
            f"{label}包含 {len(data_rows)} 个定位点，超过上限 {MAX_POINTS} 个。",
            code="too_many_points",
        )

    positions: list[Position] = []
    max_idx = max(indices)
    naive_seen = False
    last_time_by_trip: dict[str, datetime] = {}
    for (line, raw_cells) in data_rows:
        cells = _trim_trailing_empty(raw_cells)
        if len(cells) <= max_idx:
            raise InputError(
                f"{label}第 {line} 行只有 {len(cells)} 列，至少需要 {max_idx + 1} 列。",
                code="row_too_short",
            )
        trip_id = _row_get(cells, mapping["trip_id"], label=label, line=line)
        if not trip_id:
            raise InputError(f"{label}第 {line} 行趟号为空。", code="empty_trip_id")
        time, naive = _parse_time(
            _row_get(cells, mapping["time"], label=label, line=line), label=label, line=line
        )
        naive_seen = naive_seen or naive
        lat = _parse_lat(_row_get(cells, mapping["lat"], label=label, line=line), label=label, line=line)
        lon = _parse_lon(_row_get(cells, mapping["lon"], label=label, line=line), label=label, line=line)

        previous = last_time_by_trip.get(trip_id)
        if previous is not None:
            if time == previous:
                raise InputError(
                    f"{label}第 {line} 行（趟号 {trip_id}）时刻与该趟上一行相同"
                    f"（{_format_time(time)}），同趟定位时刻必须严格递增。",
                    code="time_not_increasing",
                )
            if time < previous:
                raise InputError(
                    f"{label}第 {line} 行（趟号 {trip_id}）时刻 {_format_time(time)} "
                    f"早于该趟上一行 {_format_time(previous)}，同趟定位时刻必须严格递增。",
                    code="time_not_increasing",
                )
        last_time_by_trip[trip_id] = time
        positions.append(
            Position(
                index=len(positions),
                time=time,
                lat=lat,
                lon=lon,
                raw_time=cells[mapping["time"]].strip(),
                trip_id=trip_id,
            )
        )

    if not positions:
        raise InputError(f"{label}没有有效定位点。", code="no_data")
    if naive_seen:
        warnings.append(f"{label}存在未带时区的时刻，已按 UTC 解释。")
    return positions, warnings


def parse_inputs(
    station_bytes: bytes,
    position_bytes: bytes,
    *,
    station_filename: str = "站点.csv",
    position_filename: str = "定位.csv",
) -> ParsedInput:
    stops, stop_warnings = parse_station_csv(station_bytes, filename=station_filename)
    positions, position_warnings = parse_position_csv(position_bytes, filename=position_filename)
    return ParsedInput(
        stops=stops,
        positions=positions,
        warnings=stop_warnings + position_warnings,
    )


def parse_swipe_csv(data: bytes, *, filename: str = "刷卡文件") -> tuple[list[Swipe], list[str]]:
    """解析刷卡流水 CSV：必需列为流水号、趟号、UTC 时刻；最多 MAX_SWIPES 条。

    流水号在文件内必须唯一；时刻解析复用通用规则（未带时区按 UTC 解释）。
    """

    label = f"刷卡文件（{filename}）"
    text = decode_bytes(data, label=label)
    rows = _read_rows(text)
    if not rows:
        raise InputError(f"{label}没有表头或数据行。", code="empty_file")

    required = ("transaction_id", "trip_id", "time")
    _, header_cells = rows[0]
    warnings: list[str] = []
    if _looks_like_header(header_cells):
        mapping, unknown = _resolve_columns(header_cells, required, label=label)
        data_rows = rows[1:]
        if unknown:
            warnings.append(f"{label}忽略了无关列：{'、'.join(unknown)}。")
        indices = [mapping[role] for role in required]
    else:
        data_rows = rows
        mapping = {role: idx for idx, role in enumerate(required)}
        indices = list(range(len(required)))
        if len(header_cells) < len(required):
            raise InputError(
                f"{label}未识别到表头，且首行列数不足 {len(required)} 列。"
                "请提供表头：流水号, 趟号, 时刻UTC。",
                code="missing_header",
            )
        warnings.append(
            f"{label}未识别到表头，已按固定列序（流水号, 趟号, 时刻UTC）解析。"
        )

    if not data_rows:
        raise InputError(f"{label}只有表头，没有刷卡数据行。", code="no_data")
    if len(data_rows) > MAX_SWIPES:
        raise InputError(
            f"{label}包含 {len(data_rows)} 条流水，超过单文件上限 {MAX_SWIPES} 条。",
            code="too_many_swipes",
        )

    swipes: list[Swipe] = []
    max_idx = max(indices)
    seen_ids: dict[str, int] = {}
    naive_seen = False
    for (line, raw_cells) in data_rows:
        cells = _trim_trailing_empty(raw_cells)
        if len(cells) <= max_idx:
            raise InputError(
                f"{label}第 {line} 行只有 {len(cells)} 列，至少需要 {max_idx + 1} 列。",
                code="row_too_short",
            )
        transaction_id = _row_get(
            cells, mapping["transaction_id"], label=label, line=line
        )
        if not transaction_id:
            raise InputError(f"{label}第 {line} 行流水号为空。", code="empty_transaction_id")
        trip_id = _row_get(cells, mapping["trip_id"], label=label, line=line)
        if not trip_id:
            raise InputError(f"{label}第 {line} 行趟号为空。", code="empty_trip_id")
        time, naive = _parse_time(
            _row_get(cells, mapping["time"], label=label, line=line), label=label, line=line
        )
        naive_seen = naive_seen or naive
        if transaction_id in seen_ids:
            raise InputError(
                f"{label}流水号“{transaction_id}”重复（第 {seen_ids[transaction_id]} 行与第 "
                f"{line} 行），流水号必须唯一。",
                code="duplicate_transaction_id",
            )
        seen_ids[transaction_id] = line
        swipes.append(
            Swipe(
                index=len(swipes),
                transaction_id=transaction_id,
                trip_id=trip_id,
                time=time,
                raw_time=cells[mapping["time"]].strip(),
            )
        )

    if not swipes:
        raise InputError(f"{label}没有有效刷卡流水。", code="no_data")
    if naive_seen:
        warnings.append(f"{label}存在未带时区的时刻，已按 UTC 解释。")
    return swipes, warnings
