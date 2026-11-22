"""口径重算引擎领域服务：根据新口径版本重算历史数据。

跨模块数据流: metric_definition -> caliber_version -> recalc_engine -> query_builder(缓存失效)
口径变更时触发重算，重算完成后失效相关缓存。

窗口重算沿革模型：
  1. 任务「登记」时即固化本次请求的历史区间（起止业务日期）与新旧版本依据，
     写入重算登记表，之后任务状态流转不改写登记材料；
  2. 任务执行确认后，把执行时间、最终差异和逐日源数据快照关联回同一份登记；
  3. 窗口查询按指标 + 业务日期反查所有覆盖该日期的登记，已确认结果与待执行
     任务分别标注，后完成的补算不会删除早期整月重算所用的材料。
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

from app.store import store
from app.services.base import BaseModuleService
from app.services.date_window import DateWindowError, is_valid_biz_date, parse_window

# 任务状态 -> 窗口沿革确认状态
_CONFIRMED_STATUS = "已确认"
_PENDING_STATUS = "待执行"
_CANCELLED_STATUS = "已取消"
_STATUS_MAP = {
    "待调度": _PENDING_STATUS,
    "重算中": _PENDING_STATUS,
    "已完成": _CONFIRMED_STATUS,
    "重算失败": _CANCELLED_STATUS,
}
_FINAL_STATUSES = {"已完成", "重算失败"}


class RecalcEngineService(BaseModuleService):
    MODULE = "recalc_engine"
    ENTITY = "重算任务"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["关联指标", "新口径版本", "历史时间范围", "重算状态", "产出差异"]
    REQUIRED_FIELDS = ["关联指标", "新口径版本", "历史时间范围"]
    STATUS_ORDER = ["待调度", "重算中", "已完成", "重算失败"]
    ACTION_RULES = {"启动重算": "重算中", "取消重算": "重算失败"}
    NEGATIVE_ACTIONS = ["取消重算"]

    # ── 登记：固化窗口与版本依据 ──

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记重算任务，同时把请求的历史区间和版本依据固化到重算登记表。"""
        entry, missing = super().create_entry(values)
        if missing:
            return entry, missing
        assert entry is not None
        try:
            window = parse_window(str(values.get("历史时间范围", "")))
        except DateWindowError:
            # 登记本身保留原始文本，窗口查询时无法按日期命中，由登记表记录异常
            store.rows(self.MODULE).remove(entry)
            return None, ["历史时间范围（需为 yyyy-mm-dd 至 yyyy-mm-dd 的明确窗口，或 yyyy-mm 自然月）"]
        self._build_registration(entry, window)
        return entry, []

    def register_recalc_task(self, metric_name: str, new_caliber: str,
                             time_range: str, old_caliber: str | None = None,
                             ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        """供指标定义等跨模块入口登记重算任务，返回 (任务, 登记, 提示语)。"""
        if not old_caliber:
            active = next(
                (c for c in store.find_by_field("caliber_version", "所属指标", metric_name)
                 if c.get("status") == "已生效"),
                None,
            )
            old_caliber = str(active.get("版本号", "unknown")) if active else "unknown"
        try:
            window = parse_window(time_range)
        except DateWindowError as exc:
            return None, None, str(exc)

        task = {
            "id": store.next_id(self.MODULE),
            "关联指标": metric_name,
            "新口径版本": new_caliber,
            "历史时间范围": time_range,
            "重算状态": "待调度",
            "产出差异": "",
            "status": "待调度",
            "pending": True,
            "abnormal": False,
        }
        store.rows(self.MODULE).append(task)
        registration = self._build_registration(task, window, old_caliber=old_caliber)
        return task, registration, f"已创建重算任务 #{task['id']}"

    def _build_registration(self, task: dict[str, Any], window: Any,
                            old_caliber: str | None = None) -> dict[str, Any]:
        metric_name = str(task.get("关联指标", ""))
        new_caliber = str(task.get("新口径版本", ""))
        if old_caliber is None:
            active = next(
                (c for c in store.find_by_field("caliber_version", "所属指标", metric_name)
                 if c.get("status") == "已生效"),
                None,
            )
            old_caliber = str(active.get("版本号", "unknown")) if active else "unknown"

        registration = {
            "id": store.next_recalc_registration_id(),
            "task_id": int(task.get("id", 0)),
            "关联指标": metric_name,
            "window_start": window.start,
            "window_end": window.end,
            "raw_range": window.raw,
            "old_caliber": old_caliber,
            "new_caliber": new_caliber,
            "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "confirmation_status": _PENDING_STATUS,
            "executed_at": None,
            "diff_summary": "",
        }
        task["registration_id"] = registration["id"]
        store.add_recalc_registration(registration)
        return registration

    # ── 动作与执行：关联执行结果，不删除早期材料 ──

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        result, message = super().run_action(entry_id, action)
        if result is not None:
            registration = self._registration_for(entry_id)
            if registration is not None:
                registration["confirmation_status"] = _STATUS_MAP.get(
                    str(entry.get("status")), _PENDING_STATUS)
        return result, message

    def start_recalc(self, entry_id: int) -> dict[str, Any] | None:
        """启动重算任务并把执行结果、逐日源数据快照关联回登记记录。

        跨模块数据流: meter_definition -> caliber_version -> recalc_engine
        """
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {"error": f"重算任务 {entry_id} 不存在"}

        metric_name = task.get("关联指标", "")
        new_version = task.get("新口径版本", "")
        time_range = task.get("历史时间范围", "")
        registration = self._registration_for(entry_id)

        calibers = store.find_by_field("caliber_version", "所属指标", metric_name)
        active_caliber = next((c for c in calibers if c.get("status") == "已生效"), None)
        old_version = (registration.get("old_caliber") if registration else None) or (
            active_caliber.get("版本号") if active_caliber else "unknown")

        task["status"] = "重算中"
        task["pending"] = True

        diff_percentage = self._diff_percentage(entry_id)
        diff_summary = (
            f"口径从 {old_version} 变更为 {new_version}，"
            f"覆盖时间范围 {time_range}，历史数据差异约 {diff_percentage:.1f}%"
        )

        task["status"] = "已完成"
        task["pending"] = False
        task["产出差异"] = diff_summary

        executed_at = time.strftime("%Y-%m-%d %H:%M:%S")
        if registration is not None:
            # 只更新执行侧字段，登记时固化的窗口与版本依据保持不变
            registration["confirmation_status"] = _CONFIRMED_STATUS
            registration["executed_at"] = executed_at
            registration["diff_summary"] = diff_summary
            self._snapshot_window_sources(registration, old_version, new_version)
            store.record_recalc(
                metric_id=self._metric_id(metric_name),
                old_version=old_version, new_version=new_version,
                time_range=registration["raw_range"], diff_summary=diff_summary,
                metric_name=metric_name, registration_id=registration["id"],
                status=_CONFIRMED_STATUS,
            )

        # 失效该指标所有缓存
        from app.services.cache_layer import CacheLayerService
        cache_svc = CacheLayerService()
        invalidation = cache_svc.invalidate_by_metric(metric_name)

        return {
            "task_id": entry_id,
            "registration_id": registration.get("id") if registration else None,
            "metric": metric_name,
            "old_caliber": old_version,
            "new_caliber": new_version,
            "window": {
                "start": registration["window_start"],
                "end": registration["window_end"],
            } if registration else None,
            "time_range": time_range,
            "diff_summary": diff_summary,
            "status": "已完成",
            "cache_invalidated": invalidation.get("invalidated_entries", 0),
        }

    def _snapshot_window_sources(self, registration: dict[str, Any],
                                 old_version: str, new_version: str) -> None:
        """为窗口内每个业务日期留存本次确认重算实际使用的源数据快照。

        快照按登记维度留档：之后的补算会产生新登记的新快照，
        早期整月重算的快照仍然保留，可回答「早期报表当时用的是哪份」。
        """
        start = date.fromisoformat(registration["window_start"])
        end = date.fromisoformat(registration["window_end"])
        cursor = start
        seq = 0
        while cursor <= end:
            seq += 1
            biz_date = cursor.isoformat()
            old_value = 38000 + seq * 137 + registration["id"] * 11
            new_value = round(old_value * (1 + self._diff_percentage(registration["task_id"]) / 100))
            store.add_source_snapshot({
                "registration_id": registration["id"],
                "biz_date": biz_date,
                "source_table": (
                    f"dwd_active_log@{registration['executed_at'][:10]}快照"
                    f"#登记{registration['id']}"
                ),
                "source_rows": old_value,
                "old_value": old_value,
                "new_value": new_value,
                "old_caliber": old_version,
                "caliber_basis": new_version,
            })
            cursor += timedelta(days=1)

    @staticmethod
    def _diff_percentage(task_id: int) -> float:
        """确定性的模拟差异率，保证同一任务多次核对结果一致。"""
        return round(3.2 + (int(task_id) * 0.7) % 4 + int(task_id) % 2 * 0.3, 1)

    def _registration_for(self, task_id: int) -> dict[str, Any] | None:
        for item in store.recalc_registrations():
            if int(item.get("task_id", 0)) == int(task_id):
                return item
        return None

    @staticmethod
    def _metric_id(metric_name: str) -> int:
        for row in store.rows("metric_definition"):
            if row.get("指标名称") == metric_name:
                return int(row.get("id", 0))
        return 0

    # ── 窗口重算沿革查询 ──

    def query_window_history(
        self,
        *,
        metric_name: str,
        biz_date: str | None = None,
        caliber_version: str | None = None,
        result_status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        """按指标、口径和业务日期返回窗口重算沿革明细（含筛选与翻页）。

        - biz_date 命中所有窗口覆盖该日期的登记（含整月与补算重叠的日期）；
        - result_status 区分「已确认」与「待执行」（另有「已取消」）；
        - 已确认项附带该日期实际使用的源数据快照与版本依据，不受后续补算影响。
        """
        if not metric_name.strip():
            return {"error": "必须指定关联指标才能查询窗口重算沿革"}
        if biz_date is not None and not is_valid_biz_date(biz_date):
            return {"error": "业务日期需为 yyyy-mm-dd 格式"}
        if result_status is not None and result_status not in (
                _CONFIRMED_STATUS, _PENDING_STATUS, _CANCELLED_STATUS):
            return {"error": f"结果状态仅支持：{_CONFIRMED_STATUS}、{_PENDING_STATUS}、{_CANCELLED_STATUS}"}

        registrations = [
            dict(item) for item in store.recalc_registrations()
            if item.get("关联指标") == metric_name
        ]
        if biz_date:
            registrations = [
                item for item in registrations
                if item["window_start"] <= biz_date <= item["window_end"]
            ]
        if caliber_version:
            registrations = [
                item for item in registrations
                if caliber_version in (item.get("old_caliber"), item.get("new_caliber"))
            ]
        if result_status:
            registrations = [
                item for item in registrations
                if item.get("confirmation_status") == result_status
            ]

        # 最近执行/登记的排前面：分析员先看到当前解释该日期报表的结果
        registrations.sort(
            key=lambda item: (item.get("executed_at") or item.get("registered_at") or "",
                              int(item.get("id", 0))),
            reverse=True,
        )

        # 当前解释该业务日期报表的，是最近一次已确认重算（待执行任务不参与）
        confirmed = [item for item in registrations
                     if item.get("confirmation_status") == _CONFIRMED_STATUS]
        current_reg_id = confirmed[0]["id"] if confirmed else None

        total = len(registrations)
        start_index = (page - 1) * size
        page_items = registrations[start_index:start_index + size]
        items = [
            self._build_lineage_item(item, biz_date, current_reg_id) for item in page_items
        ]
        return {
            "items": items,
            "total": total,
            "page": page,
            "size": size,
            "filters": {
                "metric_name": metric_name,
                "biz_date": biz_date,
                "caliber_version": caliber_version,
                "result_status": result_status,
            },
            "summary": {
                "confirmed": sum(
                    1 for item in registrations
                    if item.get("confirmation_status") == _CONFIRMED_STATUS),
                "pending": sum(
                    1 for item in registrations
                    if item.get("confirmation_status") == _PENDING_STATUS),
                "cancelled": sum(
                    1 for item in registrations
                    if item.get("confirmation_status") == _CANCELLED_STATUS),
            },
        }

    def _build_lineage_item(self, registration: dict[str, Any],
                            biz_date: str | None, current_reg_id: int | None) -> dict[str, Any]:
        task = store.find(self.MODULE, int(registration.get("task_id", 0)))
        snapshots = store.source_snapshots(int(registration.get("id", 0)))
        if biz_date:
            snapshots = [snap for snap in snapshots if snap.get("biz_date") == biz_date]
        snapshot = snapshots[0] if snapshots else None
        confirmed = registration.get("confirmation_status") == _CONFIRMED_STATUS
        return {
            "registration_id": registration.get("id"),
            "task_id": registration.get("task_id"),
            "关联指标": registration.get("关联指标"),
            "biz_date": biz_date,
            "window_start": registration.get("window_start"),
            "window_end": registration.get("window_end"),
            "历史时间范围": registration.get("raw_range"),
            "old_caliber": registration.get("old_caliber"),
            "new_caliber": registration.get("new_caliber"),
            "registered_at": registration.get("registered_at"),
            "executed_at": registration.get("executed_at"),
            "confirmation_status": registration.get("confirmation_status"),
            "result_status": registration.get("confirmation_status"),
            "task_status": task.get("status") if task else None,
            "diff_summary": registration.get("diff_summary") or (
                task.get("产出差异", "") if task else ""),
            "explains_current_report": bool(
                confirmed and current_reg_id == registration.get("id")),
            "source_snapshot": snapshot,
        }

    # ── 原有查询能力 ──

    def get_recalc_history(self, metric_name: str | None = None) -> list[dict[str, Any]]:
        """获取重算历史记录。"""
        history = store.recalc_history()
        if metric_name:
            history = [h for h in history if h.get("关联指标") == metric_name]
        return history

    def estimate_recalc_impact(self, entry_id: int) -> dict[str, Any] | None:
        """预估算重算影响范围。

        跨模块数据流: recalc_engine -> query_builder（预估缓存失效范围）
                     recalc_engine -> data_service（预估接口影响）
        """
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {"error": f"重算任务 {entry_id} 不存在"}

        metric_name = task.get("关联指标", "")

        # 查找关联的查询模板和服务接口
        queries = store.find_by_field("query_builder", "关联指标", metric_name)
        services = [row for row in store.rows("data_service")
                    if metric_name in str(row.get("关联指标或模型", ""))]
        subscriptions = store.find_by_field("data_subscription", "关联指标", metric_name)

        return {
            "metric": metric_name,
            "affected_query_templates": len(queries),
            "affected_service_apis": len(services),
            "affected_subscriptions": len(subscriptions),
            "estimated_cache_invalidation": len([q for q in queries if q.get("缓存策略")]),
            "recommended_action": "confirm_and_proceed",
        }
