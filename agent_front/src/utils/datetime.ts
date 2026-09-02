/**
 * 时间格式化与「按时间分组」。
 *
 * 行业惯例：会话侧边栏按 今天 / 昨天 / 近 7 天 / 更早 分组，
 * 因为用户是按「最近用过」来找会话的，而不是按字母或创建时间。
 */
import type { ThreadItem } from '../types'

export type DateGroupKey = 'pinned' | 'today' | 'yesterday' | 'last7' | 'last30' | 'earlier'

export interface ThreadGroup {
  key: DateGroupKey
  label: string
  threads: ThreadItem[]
}

const GROUP_LABELS: Record<DateGroupKey, string> = {
  pinned: '置顶',
  today: '今天',
  yesterday: '昨天',
  last7: '近 7 天',
  last30: '近 30 天',
  earlier: '更早',
}

const DAY_MS = 24 * 60 * 60 * 1000

function startOfDay(ts: number): number {
  const d = new Date(ts)
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}

/** 计算距今天数（按自然日，而非 24h 滚动窗口） */
function daysAgo(ts: number, now: number): number {
  return Math.round((startOfDay(now) - startOfDay(ts)) / DAY_MS)
}

export function groupThreadsByDate(threads: ThreadItem[], now = Date.now()): ThreadGroup[] {
  const buckets = new Map<DateGroupKey, ThreadItem[]>()
  for (const thread of threads) {
    const key: DateGroupKey = thread.pinned ? 'pinned' : resolveBucket(thread.updated_at || thread.created_at, now)
    const list = buckets.get(key)
    if (list) list.push(thread)
    else buckets.set(key, [thread])
  }

  const order: DateGroupKey[] = ['pinned', 'today', 'yesterday', 'last7', 'last30', 'earlier']
  const groups: ThreadGroup[] = []
  for (const key of order) {
    const list = buckets.get(key)
    if (list && list.length > 0) {
      groups.push({ key, label: GROUP_LABELS[key], threads: list })
    }
  }
  return groups
}

function resolveBucket(iso: string, now: number): Exclude<DateGroupKey, 'pinned'> {
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return 'earlier'
  const diff = daysAgo(ts, now)
  if (diff <= 0) return 'today'
  if (diff === 1) return 'yesterday'
  if (diff < 7) return 'last7'
  if (diff < 30) return 'last30'
  return 'earlier'
}

/** 侧边栏行内时间：今天显示 HH:mm，本周显示 周X，更早显示 M/D */
export function formatThreadTime(iso: string, now = Date.now()): string {
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return ''
  const d = new Date(ts)
  const diff = daysAgo(ts, now)
  if (diff <= 0) {
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  }
  if (diff < 7) {
    const weekdays = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
    return weekdays[d.getDay()] ?? formatDateTime(iso)
  }
  return `${d.getMonth() + 1}/${d.getDate()}`
}

/** 文档列表用：YYYY-MM-DD HH:mm */
export function formatDateTime(iso: string): string {
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return '-'
  const d = new Date(ts)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function formatBytes(bytes: number): string {
  if (!bytes || bytes < 1024) return `${bytes || 0} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

/** 「刚刚 / 5 分钟前 / 3 小时前」式相对时间，用于上传最近状态提示 */
export function formatRelative(iso: string, now = Date.now()): string {
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return ''
  const diff = now - ts
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < DAY_MS) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 30 * DAY_MS) return `${Math.floor(diff / DAY_MS)} 天前`
  return formatDateTime(iso)
}
