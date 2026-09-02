/**
 * 统一 API 客户端。
 *
 * 所有请求走 vite proxy (/api -> http://127.0.0.1:8000)，
 * 统一处理非 2xx、JSON 解析失败与网络错误，避免每个页面各写一套 fetch。
 */
import type {
  ChatMessage,
  DocumentItem,
  DocumentListResult,
  DocumentStats,
  ThreadItem,
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
  } catch (err) {
    // 后端没起来时，用户需要的是「后端不可达」而不是一个 TypeError
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

// ── 会话历史 ─────────────────────────────────────────────────────────

export function fetchThreads(keyword = '', limit = 100): Promise<{ threads: ThreadItem[]; total: number }> {
  const params = new URLSearchParams({
    user_id: currentUserId(),
    limit: String(limit),
  })
  if (keyword.trim()) params.set('keyword', keyword.trim())
  return request(`/api/v1/research/threads?${params.toString()}`)
}

export function fetchThreadMessages(threadId: string): Promise<{ thread_id: string; messages: { role: string; content: string }[] }> {
  return request(`/api/v1/research/threads/${encodeURIComponent(threadId)}/messages`)
}

export function renameThread(threadId: string, title: string): Promise<ThreadItem | null> {
  return request(`/api/v1/research/threads/${encodeURIComponent(threadId)}/rename`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, user_id: currentUserId() }),
  })
}

export function pinThread(threadId: string, pinned: boolean): Promise<ThreadItem | null> {
  return request(`/api/v1/research/threads/${encodeURIComponent(threadId)}/pin`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pinned, user_id: currentUserId() }),
  })
}

export function deleteThread(threadId: string): Promise<{ deleted: boolean; thread_id: string; message: string }> {
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

// ── 知识库 ───────────────────────────────────────────────────────────

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

export function batchDeleteDocuments(
  docIds: string[],
): Promise<{ deleted: number; doc_ids: string[]; message: string }> {
  return request('/api/v1/documents/batch', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ doc_ids: docIds, user_id: currentUserId() }),
  })
}

export function retryDocument(docId: string): Promise<{ doc_id: string; retried: number; published: number; message: string }> {
  return request(`/api/v1/documents/${encodeURIComponent(docId)}/retry`, { method: 'POST' })
}

/**
 * 上传文档。
 *
 * 用 XMLHttpRequest 而不是 fetch —— fetch 拿不到上传字节进度，
 * 而 RAG 上传的「上传中」阶段必须有真实字节进度才有意义。
 */
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
      try {
        payload = JSON.parse(xhr.responseText)
      } catch {
        payload = null
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload as never)
        return
      }
      const detail =
        payload && typeof payload === 'object' && 'detail' in payload
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

/** 文档 -> 消息列表的适配（后端返回的是 LangGraph 的 human/ai 序列） */
export function toChatMessages(
  threadId: string,
  raw: { role: string; content: string }[],
): ChatMessage[] {
  return raw.map((m, idx) => ({
    id: `load-${threadId}-${idx}`,
    role: m.role === 'user' ? 'user' : 'assistant',
    content: m.content,
  }))
}

export type { DocumentItem }
