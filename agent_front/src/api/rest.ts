/**
 * REST API 封装 — run/cancel/resume/threads/documents/memories。
 *
 * 统一处理非 2xx、JSON 解析失败与网络错误。
 * POST body 需 fetch（不能用 EventSource），流式部分见 sse.ts。
 */
import type {
  ThreadItem,
  DocumentItem,
  DocumentListResult,
  DocumentStats,
  UploadLimits,
} from '../types'

export class ApiError extends Error {
  readonly status: number
  constructor(message: string, status = 0) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function currentUserId(): string {
  return (localStorage.getItem('dr.user_id') || 'user01').trim() || 'default_user'
}

export function getUserId(): string {
  return currentUserId()
}

export function setUserId(id: string): void {
  localStorage.setItem('dr.user_id', id.trim() || 'default_user')
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response
  try {
    resp = await fetch(path, init)
  } catch {
    throw new ApiError(
      `无法连接到后端服务（${path}）。请确认 uvicorn 已在 8000 端口启动。`,
      0,
    )
  }
  if (!resp.ok) {
    let detail = ''
    try {
      const text = await resp.text()
      try {
        const parsed = JSON.parse(text) as { detail?: unknown }
        detail = typeof parsed.detail === 'string' ? parsed.detail : text
      } catch {
        detail = text
      }
    } catch {
      detail = ''
    }
    throw new ApiError(detail || `请求失败: ${resp.status}`, resp.status)
  }
  return (await resp.json()) as T
}

// ── 会话管理 ──────────────────────────────────────────
export function fetchThreads(keyword = '', limit = 100): Promise<{ threads: ThreadItem[]; total: number }> {
  const params = new URLSearchParams({ user_id: currentUserId(), limit: String(limit) })
  if (keyword.trim()) params.set('keyword', keyword.trim())
  return request(`/api/v1/research/threads?${params.toString()}`)
}

export function fetchThreadMessages(threadId: string): Promise<{ thread_id: string; messages: { role: string; content: string }[] }> {
  return request(`/api/v1/research/threads/${encodeURIComponent(threadId)}/messages`)
}

export function renameThreadApi(threadId: string, title: string): Promise<ThreadItem | null> {
  return request(`/api/v1/research/threads/${encodeURIComponent(threadId)}/rename`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, user_id: currentUserId() }),
  })
}

export function pinThreadApi(threadId: string, pinned: boolean): Promise<ThreadItem | null> {
  return request(`/api/v1/research/threads/${encodeURIComponent(threadId)}/pin`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pinned, user_id: currentUserId() }),
  })
}

export function deleteThreadApi(threadId: string): Promise<{ deleted: boolean; thread_id: string; message: string }> {
  return request(`/api/v1/research/threads/${encodeURIComponent(threadId)}?user_id=${encodeURIComponent(currentUserId())}`, {
    method: 'DELETE',
  })
}

export function cancelResearch(threadId: string): Promise<unknown> {
  return request('/api/v1/research/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thread_id: threadId }),
  })
}

// ── 历史回滚 ──────────────────────────────────────────
// P0-4 修复：适配后端返回 {history:[{checkpoint_id, next, created_at, interrupts_count}]}
export interface CheckpointItem {
  checkpoint_id: string
  next: string[]
  created_at: string
  interrupts_count: number
}

export function fetchHistory(threadId: string): Promise<{ thread_id: string; history: CheckpointItem[] }> {
  return request(`/api/v1/research/history/${encodeURIComponent(threadId)}`)
}

// P0-4 修复：后端 RollbackRequest 要求 {thread_id, values, as_node?}
export function rollbackThread(threadId: string, checkpointId: string): Promise<unknown> {
  return request('/api/v1/research/rollback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thread_id: threadId, values: { checkpoint_id: checkpointId } }),
  })
}

// ── 记忆 ──────────────────────────────────────────────
export function fetchMemories(userId = '', query = '', limit = 200): Promise<{
  memories: { id: string; text: string; kind: string; created_at: string; updated_at: string }[]
  total: number
}> {
  const params = new URLSearchParams()
  if (userId) params.set('user_id', userId)
  if (query) params.set('query', query)
  params.set('limit', String(limit))
  return request(`/api/v1/research/memories?${params.toString()}`)
}

// ── 知识库 ────────────────────────────────────────────
export function fetchDocuments(keyword = ''): Promise<DocumentListResult> {
  const params = new URLSearchParams({ user_id: currentUserId(), with_stats: 'true' })
  if (keyword.trim()) params.set('keyword', keyword.trim())
  return request(`/api/v1/documents/list?${params.toString()}`)
}

export function fetchDocumentStats(): Promise<DocumentStats> {
  return request(`/api/v1/documents/stats?user_id=${encodeURIComponent(currentUserId())}`)
}

export function fetchUploadLimits(): Promise<UploadLimits> {
  return request('/api/v1/documents/extensions')
}

export function deleteDocument(docId: string): Promise<{ deleted: boolean; doc_id: string; message: string }> {
  return request(`/api/v1/documents/${encodeURIComponent(docId)}`, { method: 'DELETE' })
}

export function batchDeleteDocuments(docIds: string[]): Promise<{ deleted: number; doc_ids: string[]; message: string }> {
  return request('/api/v1/documents/batch', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ doc_ids: docIds, user_id: currentUserId() }),
  })
}

export function retryDocument(docId: string): Promise<{ doc_id: string; retried: number; published: number; message: string }> {
  return request(`/api/v1/documents/${encodeURIComponent(docId)}/retry`, { method: 'POST' })
}

export function uploadDocument(
  file: File,
  onProgress?: (percent: number) => void,
): Promise<{ filename: string; doc_id: string; object_key: string; chunks: number; status: string; message: string }> {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    form.append('file', file)
    form.append('user_id', currentUserId())
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/v1/documents/upload')
    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    })
    xhr.addEventListener('load', () => {
      let payload: unknown = null
      try { payload = JSON.parse(xhr.responseText) } catch { payload = null }
      if (xhr.status >= 200 && xhr.status < 300) { resolve(payload as never); return }
      const detail = payload && typeof payload === 'object' && 'detail' in payload
        ? String((payload as { detail: unknown }).detail)
        : xhr.responseText || `上传失败: ${xhr.status}`
      reject(new ApiError(detail, xhr.status))
    })
    xhr.addEventListener('error', () =>
      reject(new ApiError('网络错误，上传失败。请确认后端服务已启动。', 0)),
    )
    xhr.addEventListener('abort', () => reject(new ApiError('上传已取消', 0)))
    xhr.send(form)
  })
}

// ── P7-4: 导出 ──────────────────────────────────────
export function exportMarkdownUrl(threadId: string): string {
  return `/api/v1/research/threads/${encodeURIComponent(threadId)}/export/md`
}

export function exportPdfUrl(threadId: string): string {
  return `/api/v1/research/threads/${encodeURIComponent(threadId)}/export/pdf`
}

export async function exportMarkdown(threadId: string): Promise<Blob> {
  const resp = await fetch(exportMarkdownUrl(threadId))
  if (!resp.ok) throw new ApiError(`导出失败: ${resp.status}`, resp.status)
  return resp.blob()
}

export async function exportPdf(threadId: string): Promise<Blob> {
  const resp = await fetch(exportPdfUrl(threadId))
  if (!resp.ok) throw new ApiError(`导出失败: ${resp.status}`, resp.status)
  return resp.blob()
}

// ── 适配器 ────────────────────────────────────────────
export function toChatMessages(
  threadId: string,
  raw: { role: string; content: string }[],
): import('../types').ChatMessage[] {
  return raw.map((m, idx) => ({
    id: `load-${threadId}-${idx}`,
    role: m.role === 'user' ? 'user' : 'assistant',
    content: m.content,
  })) as import('../types').ChatMessage[]
}

export type { DocumentItem }
