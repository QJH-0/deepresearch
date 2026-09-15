/**
 * REST API 封装 — auth/run/cancel/resume/threads/documents/memories。
 *
 * 统一处理非 2xx、JSON 解析失败与网络错误；所有请求自动附带 Bearer 令牌，
 * 遇到 401 统一清理会话并跳转登录页。
 *
 * 注意：身份由令牌决定，调用方不再传 user_id —— 请求体/查询参数里的 user_id
 * 会被后端忽略。
 */
import type {
  ThreadItem,
  DocumentItem,
  DocumentListResult,
  DocumentStats,
  UploadLimits,
} from '../types'
import { authHeaders, redirectToLogin, setSession, type AuthUser } from './token'

export class ApiError extends Error {
  readonly status: number
  constructor(message: string, status = 0) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function readDetail(resp: Response): Promise<string> {
  try {
    const text = await resp.text()
    try {
      const parsed = JSON.parse(text) as { detail?: unknown }
      return typeof parsed.detail === 'string' ? parsed.detail : text
    } catch {
      return text
    }
  } catch {
    return ''
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = { ...authHeaders(), ...(init?.headers as Record<string, string> | undefined) }
  let resp: Response
  try {
    resp = await fetch(path, { ...init, headers })
  } catch {
    throw new ApiError(
      `无法连接到后端服务（${path}）。请确认 uvicorn 已在 8000 端口启动。`,
      0,
    )
  }

  if (resp.status === 401) {
    redirectToLogin()
    throw new ApiError('登录状态已失效，请重新登录', 401)
  }

  if (!resp.ok) {
    throw new ApiError((await readDetail(resp)) || `请求失败: ${resp.status}`, resp.status)
  }
  return (await resp.json()) as T
}

// ── 认证 ──────────────────────────────────────────────

export interface LoginResult extends AuthUser {
  access_token: string
  token_type: string
  expires_in: number
}

/** 用用户名口令换取令牌，并写入会话存储。 */
export async function login(userId: string, password: string): Promise<AuthUser> {
  let resp: Response
  try {
    resp = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, password }),
    })
  } catch {
    throw new ApiError('无法连接到后端服务，请确认服务已启动。', 0)
  }

  if (!resp.ok) {
    throw new ApiError((await readDetail(resp)) || '登录失败', resp.status)
  }

  const data = (await resp.json()) as LoginResult
  const user: AuthUser = { user_id: data.user_id, role: data.role }
  setSession(data.access_token, user)
  return user
}

/** 校验当前令牌是否仍有效，返回身份。 */
export function fetchMe(): Promise<AuthUser> {
  return request<AuthUser>('/api/v1/auth/me')
}

// ── 会话管理 ──────────────────────────────────────────

export function fetchThreads(keyword = '', limit = 100): Promise<{ threads: ThreadItem[]; total: number }> {
  const params = new URLSearchParams({ limit: String(limit) })
  if (keyword.trim()) params.set('keyword', keyword.trim())
  return request(`/api/v1/research/threads?${params.toString()}`)
}

export interface ThreadState {
  thread_id: string
  status: string
  resumable: boolean
  next_nodes: string[]
  interrupted_by_restart?: boolean
}

export function fetchThreadState(threadId: string): Promise<ThreadState> {
  return request(`/api/v1/research/threads/${encodeURIComponent(threadId)}/state`)
}

export function fetchThreadMessages(threadId: string): Promise<{ thread_id: string; messages: { role: string; content: string }[] }> {
  return request(`/api/v1/research/threads/${encodeURIComponent(threadId)}/messages`)
}

export function renameThreadApi(threadId: string, title: string): Promise<ThreadItem | null> {
  return request(`/api/v1/research/threads/${encodeURIComponent(threadId)}/rename`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
}

export function pinThreadApi(threadId: string, pinned: boolean): Promise<ThreadItem | null> {
  return request(`/api/v1/research/threads/${encodeURIComponent(threadId)}/pin`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pinned }),
  })
}

export function deleteThreadApi(threadId: string): Promise<{ deleted: boolean; thread_id: string; message: string }> {
  return request(`/api/v1/research/threads/${encodeURIComponent(threadId)}`, {
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
// 后端返回 {history:[{checkpoint_id, next, created_at, interrupts_count}]}
export interface CheckpointItem {
  checkpoint_id: string
  next: string[]
  created_at: string
  interrupts_count: number
}

export function fetchHistory(threadId: string): Promise<{ thread_id: string; history: CheckpointItem[] }> {
  return request(`/api/v1/research/history/${encodeURIComponent(threadId)}`)
}

export function rollbackThread(threadId: string, checkpointId: string): Promise<unknown> {
  return request('/api/v1/research/rollback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thread_id: threadId, values: { checkpoint_id: checkpointId } }),
  })
}

// ── 记忆 ──────────────────────────────────────────────
export function fetchMemories(query = '', limit = 200): Promise<{
  memories: { id: string; text: string; kind: string; created_at: string; updated_at: string }[]
  total: number
}> {
  const params = new URLSearchParams()
  if (query) params.set('query', query)
  params.set('limit', String(limit))
  return request(`/api/v1/research/memories?${params.toString()}`)
}

// ── 知识库 ────────────────────────────────────────────
export function fetchDocuments(keyword = ''): Promise<DocumentListResult> {
  const params = new URLSearchParams({ with_stats: 'true' })
  if (keyword.trim()) params.set('keyword', keyword.trim())
  return request(`/api/v1/documents/list?${params.toString()}`)
}

export function fetchDocumentStats(): Promise<DocumentStats> {
  return request('/api/v1/documents/stats')
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
    body: JSON.stringify({ doc_ids: docIds }),
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
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/v1/documents/upload')
    // XHR 无法走 request()，令牌需手动附带
    for (const [key, value] of Object.entries(authHeaders())) {
      xhr.setRequestHeader(key, value)
    }
    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    })
    xhr.addEventListener('load', () => {
      if (xhr.status === 401) {
        redirectToLogin()
        reject(new ApiError('登录状态已失效，请重新登录', 401))
        return
      }
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

// ── 导出 ──────────────────────────────────────────────
// 导出接口同样要求认证，因此不能用 <a href> 直链，必须走 fetch 再落盘
export async function exportMarkdown(threadId: string): Promise<Blob> {
  return downloadBlob(`/api/v1/research/threads/${encodeURIComponent(threadId)}/export/md`)
}

export async function exportPdf(threadId: string): Promise<Blob> {
  return downloadBlob(`/api/v1/research/threads/${encodeURIComponent(threadId)}/export/pdf`)
}

async function downloadBlob(path: string): Promise<Blob> {
  let resp: Response
  try {
    resp = await fetch(path, { headers: authHeaders() })
  } catch {
    throw new ApiError('无法连接到后端服务，导出失败。', 0)
  }
  if (resp.status === 401) {
    redirectToLogin()
    throw new ApiError('登录状态已失效，请重新登录', 401)
  }
  if (!resp.ok) {
    throw new ApiError(`导出失败: ${resp.status}`, resp.status)
  }
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
