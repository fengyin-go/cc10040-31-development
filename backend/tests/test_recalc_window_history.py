"""窗口重算沿革测试。

场景：同一指标先重算整月（任务A），之后更正其中一周（任务B），
分析员从具体业务日期搜索，需要：
  - 重叠日能同时看到整月与补算两条沿革，且当前报表由最近确认的补算解释；
  - 月内但补算范围外的日期只能看到整月沿革；
  - 两次实际使用的口径版本与源数据快照都能在沿革中找到，补算不删除旧材料；
  - 待执行任务与已确认结果区分开，筛选和翻页限定在同一指标与窗口。
"""
from __future__ import annotations

import pytest

from app.services.recalc_engine import RecalcEngineService
from app.services.date_window import parse_window, DateWindowError
from app.store import store

METRIC_PREFIX = "窗口沿革测试指标_成交额"


@pytest.fixture(autouse=True)
def _scenario():
    """构造整月重算 + 周内补算 + 待执行补算的三任务场景。

    内存仓库是全局单例，用例间不重置，因此每条用例使用唯一指标名隔离数据。
    """
    suffix = f"{store.next_id('metric_definition')}_{len(store.recalc_registrations())}"
    metric = f"{METRIC_PREFIX}_{suffix}"
    metric_id = store.next_id("metric_definition")
    store.rows("metric_definition").append({
        "id": metric_id, "status": "已发布", "pending": False, "abnormal": False,
        "指标名称": metric, "计算口径": "成交额", "统计粒度": "天",
        "数据源": "dwd_trade_detail", "业务域": "交易",
    })
    for offset, (version, status) in enumerate((
        ("v1.0", "已过期"), ("v2.0", "已生效"), ("v2.1", "待生效")
    )):
        store.rows("caliber_version").append({
            "id": store.next_id("caliber_version"), "所属指标": metric, "版本号": version,
            "口径描述": f"{version}口径", "变更类型": "口径调整",
            "生效时间": "2026-10-01", "status": status,
            "pending": status == "待生效", "abnormal": False,
        })

    svc = RecalcEngineService()
    # 任务A：整月重算，确认完成
    task_a, reg_a, _ = svc.register_recalc_task(metric, "v2.0", "2026-10-01 至 2026-10-31", "v1.0")
    result_a = svc.start_recalc(task_a["id"])
    # 任务B：其中一周补算，确认完成（不删除任务A的材料）
    task_b, reg_b, _ = svc.register_recalc_task(metric, "v2.1", "2026-10-20 至 2026-10-26", "v2.0")
    result_b = svc.start_recalc(task_b["id"])
    # 任务C：月末再次补算，仍待执行
    task_c, reg_c, _ = svc.register_recalc_task(metric, "v2.1", "2026-10-27 至 2026-10-31", "v2.0")
    yield {
        "svc": svc, "metric": metric,
        "task_a": task_a, "task_b": task_b, "task_c": task_c,
        "reg_a": reg_a, "reg_b": reg_b, "reg_c": reg_c,
        "result_a": result_a, "result_b": result_b,
    }


class TestDateWindowParser:
    def test_full_range_and_month_abbr_and_single_day(self):
        window = parse_window("2026-10-01 至 2026-10-31")
        assert (window.start, window.end) == ("2026-10-01", "2026-10-31")
        month = parse_window("2026-10")
        assert (month.start, month.end) == ("2026-10-01", "2026-10-31")
        single = parse_window("2026-10-15")
        assert single.start == single.end == "2026-10-15"

    def test_invalid_range_raises(self):
        with pytest.raises(DateWindowError):
            parse_window("")
        with pytest.raises(DateWindowError):
            parse_window("not-a-date")

    def test_create_entry_rejects_unparseable_window_without_orphan(self):
        svc = RecalcEngineService()
        before = len(store.rows("recalc_engine"))
        entry, missing = svc.create_entry({
            "关联指标": "无窗口指标", "新口径版本": "v2.0", "历史时间范围": "十月上旬",
        })
        assert entry is None
        assert missing  # 返回可提示的字段问题
        assert len(store.rows("recalc_engine")) == before  # 没有遗留半成品任务
        assert all(r.get("关联指标") != "无窗口指标" for r in store.recalc_registrations())

    def test_registration_solidifies_window(self, _scenario):
        reg = _scenario["reg_a"]
        assert reg["window_start"] == "2026-10-01"
        assert reg["window_end"] == "2026-10-31"
        assert reg["old_caliber"] == "v1.0"
        assert reg["new_caliber"] == "v2.0"
        assert reg["confirmation_status"] == "已确认"


class TestWindowHistoryOverlap:
    def test_overlap_day_returns_both_recalcs_with_diffs_and_sources(self, _scenario):
        """重叠日 2026-10-22：整月与补算都能解释该日期，差异与来源数据可核对。"""
        result = _scenario["svc"].query_window_history(
            metric_name=_scenario["metric"], biz_date="2026-10-22")
        # 任务C窗口 10-27 起，22 日不在其中 -> 实际命中整月 A、补算 B 两条
        assert result["total"] == 2
        items = result["items"]
        reg_ids = {item["registration_id"] for item in items}
        assert reg_ids == {_scenario["reg_a"]["id"], _scenario["reg_b"]["id"]}

        # 最近确认的补算 B 解释当前报表，整月 A 保留但不再是当前解释
        by_id = {item["registration_id"]: item for item in items}
        assert by_id[_scenario["reg_b"]["id"]]["explains_current_report"] is True
        assert by_id[_scenario["reg_a"]["id"]]["explains_current_report"] is False

        # 两条都是已确认，都带差异摘要和该日期的源数据快照
        for item in items:
            assert item["confirmation_status"] == "已确认"
            assert item["diff_summary"]
            snapshot = item["source_snapshot"]
            assert snapshot is not None
            assert snapshot["biz_date"] == "2026-10-22"
            assert "source_table" in snapshot and "old_value" in snapshot and "new_value" in snapshot

        # 两次实际使用的版本依据都能在沿革中找到
        assert by_id[_scenario["reg_a"]["id"]]["new_caliber"] == "v2.0"
        assert by_id[_scenario["reg_b"]["id"]]["new_caliber"] == "v2.1"
        assert by_id[_scenario["reg_a"]["id"]]["source_snapshot"]["caliber_basis"] == "v2.0"
        assert by_id[_scenario["reg_b"]["id"]]["source_snapshot"]["caliber_basis"] == "v2.1"

    def test_day_outside_patch_only_returns_month_recalc(self, _scenario):
        """月内但补算范围外的 2026-10-10：只能检索到整月重算 A。"""
        result = _scenario["svc"].query_window_history(
            metric_name=_scenario["metric"], biz_date="2026-10-10")
        assert result["total"] == 1
        item = result["items"][0]
        assert item["registration_id"] == _scenario["reg_a"]["id"]
        assert item["explains_current_report"] is True
        assert item["source_snapshot"]["caliber_basis"] == "v2.0"

    def test_later_patch_keeps_earlier_month_material(self, _scenario):
        """补算 B 完成后，整月 A 在重叠日的快照仍然留档。"""
        snapshots_a = store.source_snapshots(_scenario["reg_a"]["id"])
        overlap = [s for s in snapshots_a if s["biz_date"] == "2026-10-22"]
        assert len(overlap) == 1
        snapshots_b = store.source_snapshots(_scenario["reg_b"]["id"])
        assert any(s["biz_date"] == "2026-10-22" for s in snapshots_b)
        # 两份快照来自不同批次，材料不互相覆盖
        assert overlap[0]["source_table"] != next(
            s for s in snapshots_b if s["biz_date"] == "2026-10-22")["source_table"]

    def test_pending_task_is_distinguished(self, _scenario):
        """待执行任务 C 在其窗口内可见，但状态为待执行、无源数据快照。"""
        result = _scenario["svc"].query_window_history(
            metric_name=_scenario["metric"], biz_date="2026-10-28")
        statuses = {item["registration_id"]: item["confirmation_status"] for item in result["items"]}
        assert statuses[_scenario["reg_c"]["id"]] == "待执行"
        pending_item = next(i for i in result["items"]
                            if i["registration_id"] == _scenario["reg_c"]["id"])
        assert pending_item["source_snapshot"] is None
        assert pending_item["explains_current_report"] is False
        assert result["summary"]["pending"] == 1
        assert result["summary"]["confirmed"] == 1

    def test_status_filter_separates_confirmed_and_pending(self, _scenario):
        confirmed = _scenario["svc"].query_window_history(
            metric_name=_scenario["metric"], biz_date="2026-10-28", result_status="已确认")
        assert confirmed["total"] == 1
        assert confirmed["items"][0]["registration_id"] == _scenario["reg_a"]["id"]
        pending = _scenario["svc"].query_window_history(
            metric_name=_scenario["metric"], biz_date="2026-10-28", result_status="待执行")
        assert pending["total"] == 1
        assert pending["items"][0]["registration_id"] == _scenario["reg_c"]["id"]

    def test_caliber_filter(self, _scenario):
        """按口径版本筛选：v2.1 只命中补算 B 与待执行 C，不命中整月 A。"""
        result = _scenario["svc"].query_window_history(
            metric_name=_scenario["metric"], caliber_version="v2.1")
        reg_ids = {item["registration_id"] for item in result["items"]}
        assert reg_ids == {_scenario["reg_b"]["id"], _scenario["reg_c"]["id"]}

    def test_pagination_stays_within_same_metric_and_window(self, _scenario):
        """翻页按同一指标与窗口切片，total 不随页码变化。"""
        first = _scenario["svc"].query_window_history(
            metric_name=_scenario["metric"], biz_date="2026-10-22", page=1, size=1)
        second = _scenario["svc"].query_window_history(
            metric_name=_scenario["metric"], biz_date="2026-10-22", page=2, size=1)
        assert first["total"] == second["total"] == 2
        assert len(first["items"]) == len(second["items"]) == 1
        assert first["items"][0]["registration_id"] != second["items"][0]["registration_id"]
        for payload in (first, second):
            assert payload["filters"]["metric_name"] == _scenario["metric"]
            assert payload["filters"]["biz_date"] == "2026-10-22"

    def test_invalid_arguments(self, _scenario):
        missing_metric = _scenario["svc"].query_window_history(metric_name="")
        assert "error" in missing_metric
        bad_date = _scenario["svc"].query_window_history(
            metric_name=_scenario["metric"], biz_date="10/22/2026")
        assert "error" in bad_date
        bad_status = _scenario["svc"].query_window_history(
            metric_name=_scenario["metric"], result_status="不存在")
        assert "error" in bad_status


class TestSeedDemoScenario:
    """内置演示数据：整月任务 + 周补算 + 待执行任务的窗口核对。"""

    def test_seed_demo_overlap_and_outside_days(self):
        svc = RecalcEngineService()
        demo_metric = "月活跃用户数(MAU)"
        # 重叠日 10-23：整月 v2.0 与周补算 v2.1 同时命中
        overlap = svc.query_window_history(metric_name=demo_metric, biz_date="2026-10-23")
        assert overlap["total"] == 2
        versions = sorted(item["new_caliber"] for item in overlap["items"])
        assert versions == ["v2.0", "v2.1"]
        # 当前报表由最近确认的补算 v2.1 解释
        current = [item for item in overlap["items"] if item["explains_current_report"]]
        assert len(current) == 1 and current[0]["new_caliber"] == "v2.1"
        # 早期整月重算的源数据快照仍然保留
        earlier = next(item for item in overlap["items"] if item["new_caliber"] == "v2.0")
        assert earlier["source_snapshot"]["caliber_basis"] == "v2.0"

        # 月内但补算范围外的 10-08：只有整月重算
        outside = svc.query_window_history(metric_name=demo_metric, biz_date="2026-10-08")
        assert outside["total"] == 1
        assert outside["items"][0]["new_caliber"] == "v2.0"

    def test_seed_demo_pending_visible_but_not_confirmed(self):
        svc = RecalcEngineService()
        result = svc.query_window_history(
            metric_name="月活跃用户数(MAU)", biz_date="2026-10-29")
        # 整月已确认 + 月末待执行
        assert result["summary"]["confirmed"] == 1
        assert result["summary"]["pending"] == 1
        pending = [i for i in result["items"] if i["confirmation_status"] == "待执行"]
        assert len(pending) == 1 and pending[0]["source_snapshot"] is None
