import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export interface WindowHistoryQuery {
  metricName: string
  bizDate?: string
  caliberVersion?: string
  resultStatus?: string
  page?: number
  size?: number
}

export interface WindowHistoryItem {
  registration_id: number
  task_id: number
  关联指标: string
  biz_date: string | null
  window_start: string
  window_end: string
  历史时间范围: string
  old_caliber: string
  new_caliber: string
  registered_at: string
  executed_at: string | null
  confirmation_status: string
  result_status: string
  task_status: string | null
  diff_summary: string
  explains_current_report: boolean
  source_snapshot: {
    biz_date: string
    source_table: string
    source_rows: number
    old_value: number
    new_value: number
    caliber_basis: string
  } | null
}

export interface WindowHistoryResult {
  items: WindowHistoryItem[]
  total: number
  page: number
  size: number
  filters: Record<string, string | null>
  summary: { confirmed: number; pending: number; cancelled: number }
}

/** 窗口重算沿革：按指标、口径和业务日期检索覆盖该日期的重算登记与差异明细。 */
export async function queryRecalcWindowHistory(
  query: WindowHistoryQuery,
): Promise<WindowHistoryResult> {
  const params = new URLSearchParams({ metric_name: query.metricName })
  if (query.bizDate) params.set('biz_date', query.bizDate)
  if (query.caliberVersion) params.set('caliber_version', query.caliberVersion)
  if (query.resultStatus) params.set('result_status', query.resultStatus)
  params.set('page', String(query.page ?? 1))
  params.set('size', String(query.size ?? 10))
  const response = await request(`/api/cross/recalc/window-history?${params.toString()}`)
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    throw new Error(detail?.detail ? `窗口沿革查询失败：${detail.detail}` : '窗口沿革查询失败')
  }
  return await response.json()
}
