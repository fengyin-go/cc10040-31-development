<template>
  <section class="window-panel" data-testid="recalc-window-panel">
    <header class="window-head">
      <div>
        <h3>窗口重算沿革</h3>
        <p class="page-desc">
          按指标、口径和业务日期检索覆盖该日期的重算登记；已确认结果与待执行任务分开标注，
          后完成的补算不会删除早期重算使用的源数据。
        </p>
      </div>
    </header>

    <form class="analysis-form" @submit.prevent="reload(1)">
      <label>
        关联指标
        <input
          v-model="query.metricName"
          data-testid="window-metric"
          placeholder="如：月活跃用户数(MAU)"
          required
        />
      </label>
      <label>
        业务日期
        <input v-model="query.bizDate" data-testid="window-biz-date" type="date" />
      </label>
      <label>
        口径版本
        <input
          v-model="query.caliberVersion"
          data-testid="window-caliber"
          placeholder="如：v2.1"
        />
      </label>
      <label>
        结果状态
        <select v-model="query.resultStatus" data-testid="window-status">
          <option value="">全部</option>
          <option value="已确认">已确认</option>
          <option value="待执行">待执行</option>
          <option value="已取消">已取消</option>
        </select>
      </label>
      <button class="btn primary" data-testid="window-query" type="submit">检索沿革</button>
      <button class="btn ghost" type="button" @click="resetQuery">重置</button>
    </form>

    <p v-if="result" class="window-summary" data-testid="window-summary">
      共 {{ result.total }} 条窗口沿革：
      <span class="tag tag-confirmed">已确认 {{ result.summary.confirmed }}</span>
      <span class="tag tag-pending">待执行 {{ result.summary.pending }}</span>
      <span class="tag tag-cancelled">已取消 {{ result.summary.cancelled }}</span>
    </p>

    <table v-if="result" class="data-table" data-testid="window-table">
      <thead>
        <tr>
          <th>业务日期</th>
          <th>重算窗口</th>
          <th>版本依据</th>
          <th>结果状态</th>
          <th>执行时间</th>
          <th>最终差异</th>
          <th>来源数据快照</th>
          <th>报表解释</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in result.items"
          :key="String(item.registration_id)"
          :data-testid="`window-row-${item.registration_id}`"
          :data-status="item.confirmation_status"
        >
          <td>{{ item.biz_date ?? '—' }}</td>
          <td>
            {{ item.历史时间范围 }}
            <small class="muted">#登记{{ item.registration_id }} / 任务{{ item.task_id }}</small>
          </td>
          <td>{{ item.old_caliber }} → {{ item.new_caliber }}</td>
          <td>
            <span :class="['tag', statusClass(item.confirmation_status)]">
              {{ item.confirmation_status }}
            </span>
          </td>
          <td>{{ item.executed_at ?? '未执行' }}</td>
          <td>{{ item.diff_summary || '待重算后产生' }}</td>
          <td v-if="item.source_snapshot" class="snapshot-cell">
            <dl>
              <dt>来源表</dt>
              <dd>{{ item.source_snapshot.source_table }}</dd>
              <dt>口径依据 / 行数</dt>
              <dd>{{ item.source_snapshot.caliber_basis }} / {{ item.source_snapshot.source_rows }}</dd>
              <dt>旧值 → 新值</dt>
              <dd>{{ item.source_snapshot.old_value }} → {{ item.source_snapshot.new_value }}</dd>
            </dl>
          </td>
          <td v-else class="muted">待执行后留档</td>
          <td>
            <span v-if="item.explains_current_report" class="tag tag-current">解释当前报表</span>
            <span v-else class="muted">历史留档</span>
          </td>
        </tr>
        <tr v-if="!result.items.length">
          <td colspan="8" class="empty-state">该指标与窗口下暂无重算沿革记录</td>
        </tr>
      </tbody>
    </table>

    <footer v-if="result" class="page-foot window-pager">
      <button
        class="btn"
        data-testid="window-prev"
        type="button"
        :disabled="query.page <= 1"
        @click="reload(query.page - 1)"
      >
        上一页
      </button>
      <span>第 {{ result.page }} 页 / 每页 {{ result.size }} 条</span>
      <button
        class="btn"
        data-testid="window-next"
        type="button"
        :disabled="result.page * result.size >= result.total"
        @click="reload(query.page + 1)"
      >
        下一页
      </button>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'

import {
  queryRecalcWindowHistory,
  type WindowHistoryResult,
} from '@/api/backend-service'

const DEFAULT_METRIC = '月活跃用户数(MAU)'

const query = reactive({
  metricName: DEFAULT_METRIC,
  bizDate: '2026-10-23',
  caliberVersion: '',
  resultStatus: '',
  page: 1,
  size: 5,
})
const result = ref<WindowHistoryResult | null>(null)
const errorMessage = ref('')

function statusClass(status: string): string {
  if (status === '已确认') return 'tag-confirmed'
  if (status === '待执行') return 'tag-pending'
  return 'tag-cancelled'
}

function resetQuery() {
  query.metricName = DEFAULT_METRIC
  query.bizDate = ''
  query.caliberVersion = ''
  query.resultStatus = ''
  query.page = 1
  void reload(1)
}

async function reload(page: number) {
  if (!query.metricName.trim()) {
    errorMessage.value = '请先填写关联指标'
    return
  }
  errorMessage.value = ''
  query.page = page
  try {
    result.value = await queryRecalcWindowHistory({
      metricName: query.metricName.trim(),
      bizDate: query.bizDate || undefined,
      caliberVersion: query.caliberVersion || undefined,
      resultStatus: query.resultStatus || undefined,
      page,
      size: query.size,
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '窗口沿革查询失败'
  }
}

void reload(1)
</script>

<style scoped>
.window-panel {
  margin-top: 20px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
}
.window-head h3 {
  margin: 0 0 4px;
}
.window-summary {
  font-size: 13px;
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.tag {
  display: inline-block;
  border-radius: 10px;
  padding: 1px 9px;
  font-size: 12px;
  line-height: 18px;
}
.tag-confirmed {
  background: #dcfce7;
  color: #15803d;
}
.tag-pending {
  background: #fef3c7;
  color: #b45309;
}
.tag-cancelled {
  background: #fee2e2;
  color: #b91c1c;
}
.tag-current {
  background: #dbeafe;
  color: #1d4ed8;
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
.snapshot-cell dl {
  margin: 0;
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 2px 8px;
  font-size: 12px;
}
.snapshot-cell dt {
  color: var(--muted);
}
.snapshot-cell dd {
  margin: 0;
}
.window-pager {
  align-items: center;
  gap: 10px;
}
</style>
