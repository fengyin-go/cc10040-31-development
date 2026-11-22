"""指标定义领域服务：管理业务指标的计算口径、聚合维度和数据源映射。

作为度量链的起点，指标定义被口径版本、查询构建器、数据服务等多模块引用。
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService


class MetricDefinitionService(BaseModuleService):
    MODULE = "metric_definition"
    ENTITY = "指标"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["指标名称", "计算口径", "统计粒度", "数据源", "业务域"]
    REQUIRED_FIELDS = ["指标名称", "计算口径", "统计粒度"]
    STATUS_ORDER = ["草稿", "已发布", "已停用"]
    ACTION_RULES = {"发布指标": "已发布", "停用指标": "已停用"}
    NEGATIVE_ACTIONS = ["停用指标"]

    def publish_with_caliber(self, entry_id: int, caliber_desc: str) -> tuple[dict[str, Any] | None, str]:
        """发布指标并自动创建初始口径版本。

        跨模块数据流: metric_definition -> caliber_version
        已发布指标必须有至少一个已生效口径版本，查询构建器才能引用。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if entry["status"] != "草稿":
            return None, f"{self.ENTITY} {entry_id} 只能从草稿状态发布"

        entry["status"] = "已发布"
        entry["pending"] = True
        entry["abnormal"] = False

        # 自动创建口径版本文档
        version_id = store.next_id("caliber_version")
        timestamp = time.strftime("%Y-%m-%d")
        caliber = {
            "id": version_id,
            "所属指标": entry.get("指标名称", ""),
            "版本号": "v1.0",
            "口径描述": caliber_desc,
            "变更类型": "初始创建",
            "生效时间": timestamp,
            "status": "已生效",
            "pending": True,
            "abnormal": False,
        }
        store.rows("caliber_version").append(caliber)
        return entry, f"指标已发布并创建口径版本 v1.0"

    def get_metric_chain(self, entry_id: int) -> dict[str, Any] | None:
        """获取指标完整数据链：指标 -> 口径 -> 查询模板 -> 数据服务。

        跨模块数据流: metric_definition -> caliber_version -> query_builder -> data_service
        """
        metric_data = store.get_metric_data(entry_id)
        if metric_data is None:
            return None
        metric = metric_data["metric"]
        chain = {
            "metric_id": entry_id,
            "metric_name": metric.get("指标名称", ""),
            "caliber_count": len(metric_data["calibers"]),
            "caliber_versions": [
                {"id": c["id"], "version": c.get("版本号", ""),
                 "status": c.get("status", ""), "desc": c.get("口径描述", "")}
                for c in metric_data["calibers"]
            ],
            "query_templates": len(metric_data["queries"]),
            "service_apis": len(metric_data["services"]),
        }
        return chain

    def recalc_metric_history(self, entry_id: int, new_caliber_version: str,
                              time_range: str) -> tuple[dict[str, Any] | None, str]:
        """触发指标历史数据按新口径重算。

        跨模块数据流: metric_definition -> caliber_version -> recalc_engine
        登记时由重算引擎固化本次请求的历史区间和版本依据。
        """
        metric = store.find(self.MODULE, entry_id)
        if metric is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        # 查找当前口径
        calibers = store.find_by_field("caliber_version", "所属指标", metric.get("指标名称", ""))
        active = [c for c in calibers if c.get("status") == "已生效"]
        old_version = active[0].get("版本号", "unknown") if active else "unknown"

        from app.services.recalc_engine import RecalcEngineService
        task, registration, message = RecalcEngineService().register_recalc_task(
            metric_name=str(metric.get("指标名称", "")),
            new_caliber=new_caliber_version,
            time_range=time_range,
            old_caliber=str(old_version),
        )
        if task is None:
            return None, message

        store.record_recalc(
            entry_id, str(old_version), new_caliber_version, time_range,
            "待重算后产生", metric_name=str(metric.get("指标名称", "")),
            registration_id=registration["id"] if registration else None,
            status="待执行",
        )
        return task, message