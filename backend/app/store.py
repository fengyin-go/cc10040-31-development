"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
同时提供跨模块查询和数据流处理能力。
"""
from __future__ import annotations

import time
import threading
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._id_counters: dict[str, int] = {}
        # Cache layer storage
        self._cache: dict[str, tuple[float, Any]] = {}
        # Cross-module journal
        self._query_log: list[dict[str, Any]] = []
        # Cohort snapshots
        self._cohort_snapshots: dict[str, list[dict[str, Any]]] = {}
        # Anomaly records
        self._anomaly_records: list[dict[str, Any]] = []
        # Recalc history（保留旧形态，窗口沿革以登记表为准）
        self._recalc_history: list[dict[str, Any]] = []
        # 重算登记：登记时固化窗口与版本依据，后续执行不改写登记材料
        self._recalc_registrations: list[dict[str, Any]] = []
        # 每次确认重算实际使用的源数据快照（按登记 + 业务日期留档，不被后续补算删除）
        self._recalc_source_snapshots: list[dict[str, Any]] = []
        # Lock for thread safety
        self._lock = threading.Lock()
        self._bootstrap_recalc_demo()

    @property
    def lock(self):
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        rows = self.rows(module)
        if not rows:
            return 1
        return max(int(row.get("id", 0)) for row in rows) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ── Cross-module query helpers ──

    def find_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        """查找模块中 field 等于 value 的所有记录。"""
        return [row for row in self.rows(module) if row.get(field) == value]

    def find_by_status(self, module: str, status: str) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get("status") == status]

    # ── Query journal ──

    def log_query(self, metric_id: int, operator: str, sql_summary: str,
                  row_count: int, duration_ms: float, status: str = "正常") -> dict[str, Any]:
        entry = {
            "id": len(self._query_log) + 1,
            "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "操作人": operator,
            "涉及指标": f"metric_{metric_id}",
            "查询语句摘要": sql_summary,
            "返回行数": str(row_count),
            "status": status,
            "duration_ms": duration_ms,
            "pending": status != "正常",
            "abnormal": status in ("慢查询", "异常终止"),
        }
        self._query_log.append(entry)
        return entry

    def query_logs(self) -> list[dict[str, Any]]:
        return self._query_log

    # ── Cache operations ──

    def cache_get(self, key: str) -> Any | None:
        """获取缓存，过期返回 None。"""
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            del self._cache[key]
            return None
        return value

    def cache_set(self, key: str, value: Any, ttl: int) -> None:
        """设置缓存，ttl 单位为秒。"""
        self._cache[key] = (time.time() + ttl, value)

    def cache_invalidate(self, key: str) -> bool:
        return self._cache.pop(key, None) is not None

    def cache_invalidate_by_prefix(self, prefix: str) -> int:
        count = 0
        for key in list(self._cache):
            if key.startswith(prefix):
                del self._cache[key]
                count += 1
        return count

    # ── Cohort operations ──

    def save_cohort_snapshot(self, cohort_id: int, members: list[dict[str, Any]]) -> None:
        self._cohort_snapshots[str(cohort_id)] = members

    def get_cohort_snapshot(self, cohort_id: int) -> list[dict[str, Any]] | None:
        return self._cohort_snapshots.get(str(cohort_id))

    # ── Anomaly detection operations ──

    def record_anomaly(self, metric_id: int, severity: str, description: str,
                       value: float, threshold: float) -> dict[str, Any]:
        record = {
            "id": len(self._anomaly_records) + 1,
            "指标ID": metric_id,
            "严重程度": severity,
            "描述": description,
            "当前值": value,
            "阈值": threshold,
            "时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._anomaly_records.append(record)
        return record

    def anomaly_records(self) -> list[dict[str, Any]]:
        return self._anomaly_records

    # ── Recalc operations ──

    def record_recalc(self, metric_id: int, old_version: str, new_version: str,
                      time_range: str, diff_summary: str,
                      metric_name: str | None = None,
                      registration_id: int | None = None,
                      status: str | None = None) -> dict[str, Any]:
        record = {
            "id": len(self._recalc_history) + 1,
            "关联指标": metric_name or f"metric_{metric_id}",
            "旧口径": old_version,
            "新口径": new_version,
            "历史时间范围": time_range,
            "产出差异": diff_summary,
            "重算时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if registration_id is not None:
            record["登记ID"] = registration_id
        if status is not None:
            record["确认状态"] = status
        self._recalc_history.append(record)
        return record

    def recalc_history(self) -> list[dict[str, Any]]:
        return self._recalc_history

    # ── 重算登记与窗口沿革 ──

    def next_recalc_registration_id(self) -> int:
        return max((int(item.get("id", 0)) for item in self._recalc_registrations), default=0) + 1

    def add_recalc_registration(self, registration: dict[str, Any]) -> dict[str, Any]:
        self._recalc_registrations.append(registration)
        return registration

    def find_recalc_registration(self, registration_id: int) -> dict[str, Any] | None:
        for item in self._recalc_registrations:
            if int(item.get("id", 0)) == registration_id:
                return item
        return None

    def recalc_registrations(self) -> list[dict[str, Any]]:
        return self._recalc_registrations

    def add_source_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self._recalc_source_snapshots.append(snapshot)
        return snapshot

    def source_snapshots(self, registration_id: int) -> list[dict[str, Any]]:
        """某次确认重算实际使用的源数据快照；后续补算不会删除这里的留档。"""
        return [
            dict(item) for item in self._recalc_source_snapshots
            if int(item.get("registration_id", 0)) == registration_id
        ]

    # ── 连贯演示：同一指标先整月重算、再按周补算、另有待执行任务 ──

    def _bootstrap_recalc_demo(self) -> None:
        """构造可演示窗口重叠沿革的样例，只依赖内存数据，可重复初始化。"""
        metric_name = "月活跃用户数(MAU)"
        if self.find("metric_definition", 900) is not None:
            return

        self.rows("metric_definition").append({
            "id": 900, "status": "已发布", "pending": False, "abnormal": False,
            "指标名称": metric_name, "计算口径": "按设备去重的自然月活跃用户",
            "统计粒度": "天/月", "数据源": "dwd_user_active_daily", "业务域": "用户增长",
        })
        calibers = [
            {"id": 900, "版本号": "v1.0", "口径描述": "按设备ID去重统计月活",
             "变更类型": "初始创建", "生效时间": "2026-08-01", "status": "已过期"},
            {"id": 901, "版本号": "v2.0", "口径描述": "按登录账号去重统计月活",
             "变更类型": "口径调整", "生效时间": "2026-10-05", "status": "已生效"},
            {"id": 902, "版本号": "v2.1", "口径描述": "剔除内部测试账号后按账号去重",
             "变更类型": "口径修订", "生效时间": "2026-11-01", "status": "待生效"},
        ]
        for caliber in calibers:
            self.rows("caliber_version").append({
                "id": caliber["id"], "所属指标": metric_name,
                "版本号": caliber["版本号"], "口径描述": caliber["口径描述"],
                "变更类型": caliber["变更类型"], "生效时间": caliber["生效时间"],
                "status": caliber["status"],
                "pending": caliber["status"] == "待生效", "abnormal": False,
            })

        # 任务4：整月重算（已完成）；任务5：一周补算（已完成）；任务6：待执行的再次补算
        tasks = [
            {"id": 4, "status": "已完成", "pending": False, "abnormal": False,
             "关联指标": metric_name, "新口径版本": "v2.0",
             "历史时间范围": "2026-10-01 至 2026-10-31",
             "重算状态": "已完成",
             "产出差异": "v1.0→v2.0 整月重算，10月MAU 1,200,000→1,266,000，差异 +5.5%"},
            {"id": 5, "status": "已完成", "pending": False, "abnormal": False,
             "关联指标": metric_name, "新口径版本": "v2.1",
             "历史时间范围": "2026-10-20 至 2026-10-26",
             "重算状态": "已完成",
             "产出差异": "v2.0→v2.1 周内补算，剔除测试账号后周窗口差异 -1.2%"},
            {"id": 6, "status": "待调度", "pending": True, "abnormal": False,
             "关联指标": metric_name, "新口径版本": "v2.1",
             "历史时间范围": "2026-10-27 至 2026-10-31",
             "重算状态": "待调度", "产出差异": ""},
        ]
        for task in tasks:
            self.rows("recalc_engine").append(task)

        registrations = [
            {"id": 1, "task_id": 4, "关联指标": metric_name,
             "window_start": "2026-10-01", "window_end": "2026-10-31",
             "raw_range": "2026-10-01 至 2026-10-31",
             "old_caliber": "v1.0", "new_caliber": "v2.0",
             "registered_at": "2026-10-05 09:12:00", "confirmation_status": "已确认",
             "executed_at": "2026-10-05 10:40:00", "diff_summary": tasks[0]["产出差异"]},
            {"id": 2, "task_id": 5, "关联指标": metric_name,
             "window_start": "2026-10-20", "window_end": "2026-10-26",
             "raw_range": "2026-10-20 至 2026-10-26",
             "old_caliber": "v2.0", "new_caliber": "v2.1",
             "registered_at": "2026-10-28 14:05:00", "confirmation_status": "已确认",
             "executed_at": "2026-10-28 15:20:00", "diff_summary": tasks[1]["产出差异"]},
            {"id": 3, "task_id": 6, "关联指标": metric_name,
             "window_start": "2026-10-27", "window_end": "2026-10-31",
             "raw_range": "2026-10-27 至 2026-10-31",
             "old_caliber": "v2.0", "new_caliber": "v2.1",
             "registered_at": "2026-11-02 08:30:00", "confirmation_status": "待执行",
             "executed_at": None, "diff_summary": ""},
        ]
        self._recalc_registrations.extend(registrations)

        # 两次确认重算各自使用的源数据快照，按业务日期留档（补算不覆盖整月留档）
        for day in range(1, 32):
            biz_date = f"2026-10-{day:02d}"
            self._recalc_source_snapshots.append({
                "registration_id": 1, "biz_date": biz_date,
                "source_table": "dwd_user_active_daily@2026-10-05快照",
                "source_rows": 40000 + day * 100,
                "old_value": 38710 + day * 96,
                "new_value": 40839 + day * 101,
                "caliber_basis": "v2.0",
            })
        for day in range(20, 27):
            biz_date = f"2026-10-{day:02d}"
            self._recalc_source_snapshots.append({
                "registration_id": 2, "biz_date": biz_date,
                "source_table": "dwd_user_active_daily@2026-10-28快照",
                "source_rows": 42100 + day * 100,
                "old_value": 42859 + day * 101,
                "new_value": 42345 + day * 100,
                "caliber_basis": "v2.1",
            })

        for reg in registrations:
            self.record_recalc(
                metric_id=900, old_version=reg["old_caliber"],
                new_version=reg["new_caliber"], time_range=reg["raw_range"],
                diff_summary=reg["diff_summary"] or "待重算后产生",
                metric_name=metric_name, registration_id=reg["id"],
                status=reg["confirmation_status"],
            )


    # ── Data artifacts for cross-module consumption ──

    def get_metric_data(self, metric_id: int) -> dict[str, Any] | None:
        """Get metric data with its calibers, queries, and services."""
        metric = self.find("metric_definition", metric_id)
        if metric is None:
            return None
        return {
            "metric": metric,
            "calibers": self.find_by_field("caliber_version", "所属指标",
                                           metric.get("指标名称", "")),
            "queries": self.find_by_field("query_builder", "关联指标",
                                          metric.get("指标名称", "")),
            "services": [row for row in self.rows("data_service")
                         if metric.get("指标名称", "") in str(row.get("关联指标或模型", ""))],
            "subscriptions": self.find_by_field("data_subscription", "关联指标",
                                                 metric.get("指标名称", "")),
        }

    def get_dimension_model_data(self, model_id: int) -> dict[str, Any] | None:
        """Get dimension model with its drill-down paths and pivot configs."""
        model = self.find("dimension_model", model_id)
        if model is None:
            return None
        return {
            "model": model,
            "drill_paths": self.find_by_field("drill_down", "起始指标",
                                              model.get("模型名称", "")),
            "pivot_configs": self.find_by_field("pivot_table", "数据源指标",
                                                 model.get("模型名称", "")),
        }

    def get_cohort_analysis(self, cohort_id: int) -> dict[str, Any] | None:
        """Get cohort with experiments and segment comparisons."""
        cohort = self.find("user_cohort", cohort_id)
        if cohort is None:
            return None
        cohort_name = cohort.get("分群名称", "")
        return {
            "cohort": cohort,
            "experiments": [row for row in self.rows("experiment_group")
                            if cohort_name in str(row.get("筛选条件", ""))],
            "comparisons": [row for row in self.rows("segment_compare")
                            if cohort_name in (str(row.get("分群A", "")) + str(row.get("分群B", "")))],
            "anomaly_rules": [row for row in self.rows("anomaly_detector")
                              if cohort_name in str(row.get("目标指标", ""))],
        }

    def get_field_lineage(self, field_name: str) -> dict[str, Any]:
        """Trace a field's complete lineage chain."""
        entries = self.find_by_field("field_lineage", "目标字段", field_name)
        if not entries:
            return {"field": field_name, "lineage": [], "depth": 0}

        lineage = [{
            "source_fields": entry.get("来源字段列表", ""),
            "transform": entry.get("转换逻辑", ""),
            "intermediate_table": entry.get("中间表", ""),
            "depth": entry.get("血缘深度", ""),
            "status": entry.get("status", ""),
        } for entry in entries]

        return {
            "field": field_name,
            "lineage": lineage,
            "depth": len(lineage),
        }


store = Store()