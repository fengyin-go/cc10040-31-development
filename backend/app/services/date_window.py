"""重算历史区间（窗口）解析与按业务日期匹配的纯函数工具。

重算任务登记时的「历史时间范围」可能是完整区间、单日或自然月缩写，
统一在这里解析成可比较的起止业务日期，窗口查询和登记留档共用同一套口径。
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass

# 允许的区间连接词："2026-10-01 至 2026-10-31"、"2026-10-01~2026-10-31" 等
_RANGE_SPLIT = re.compile(r"\s*(?:至|到|~|—|–|-{2,}|,)\s*")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$")


class DateWindowError(ValueError):
    """历史时间范围无法解析为明确的业务日期窗口。"""


@dataclass(frozen=True)
class DateWindow:
    """登记时固化下来的历史区间，边界均为闭区间。"""

    start: str
    end: str
    raw: str

    def covers(self, biz_date: str) -> bool:
        """业务日期是否落在本次重算窗口内（含边界）。"""
        return self.start <= biz_date <= self.end

    def as_dict(self) -> dict[str, str]:
        return {"window_start": self.start, "window_end": self.end, "raw_range": self.raw}


def _resolve_date(token: str, *, month_end: bool = False) -> str:
    token = token.strip()
    match = _DATE_RE.match(token)
    if not match:
        raise DateWindowError(f"无法识别的业务日期：{token}")
    year, month, day = match.groups()
    if day is None:
        # "2026-10" 视为整个自然月
        last_day = calendar.monthrange(int(year), int(month))[1]
        return f"{year}-{month}-{last_day:02d}" if month_end else f"{year}-{month}-01"
    return f"{year}-{month}-{day}"


def parse_window(time_range: str) -> DateWindow:
    """把「历史时间范围」解析成闭区间 DateWindow。

    支持：
      - 完整区间：2026-10-01 至 2026-10-31 / 2026-10-01~2026-10-31
      - 自然月缩写：2026-10（等价于该月 1 日到月末）
      - 单日：2026-10-15（窗口起止相同）
    """
    raw = (time_range or "").strip()
    if not raw:
        raise DateWindowError("历史时间范围为空，无法确定重算窗口")

    parts = [part for part in _RANGE_SPLIT.split(raw) if part.strip()]
    if len(parts) == 1:
        token = parts[0]
        match = _DATE_RE.match(token)
        if match and match.group(3) is None:
            start = _resolve_date(token)
            end = _resolve_date(token, month_end=True)
        else:
            start = end = _resolve_date(token)
    elif len(parts) == 2:
        start = _resolve_date(parts[0])
        end = _resolve_date(parts[1], month_end=True) if _DATE_RE.match(parts[1].strip()) and not _DATE_RE.match(parts[1].strip()).group(3) else _resolve_date(parts[1])
    else:
        raise DateWindowError(f"历史时间范围包含过多日期片段：{raw}")

    if start > end:
        raise DateWindowError(f"历史时间范围起始日期晚于结束日期：{raw}")
    return DateWindow(start=start, end=end, raw=raw)


def is_valid_biz_date(biz_date: str) -> bool:
    """窗口查询入参校验：必须是 yyyy-mm-dd 完整业务日期。"""
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", biz_date or ""))
