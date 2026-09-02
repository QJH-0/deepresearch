/**
 * 全局类型定义。
 *
 * 与后端 backend/schemas/*.py 一一对应，改后端字段时这里要同步。
 */

// ── 对话流事件（SSE） ────────────────────────────────────────────────
export type StreamEventType =
  | 'status'
  | 'phase'
  | 'route'
  | 'final'
  | 'error'
  | 'interrupt'
  | 'interrupted'
  | 'cancelled'

export interface StreamEvent {
  type: StreamEventType
  message?: string
  final?: string
  node?: string
  interrupt_id?: string
  value?: Record<string, unknown> | null
  thread_id?: string
  resumable?: boolean
}

// ── 消息 ─────────────────────────────────────────────────────────────
export type MessageRole = 'user' | 'assistant' | 'status'

export interface ChatMessage {
  id: string
  role: MessageRole
  content: string
  /** status 消息可折叠，避免长过程日志淹没正文 */
  collapsed?: boolean
}

// ── 会话历史 ─────────────────────────────────────────────────────────
export interface ThreadItem {
  thread_id: string
  title: string
  query: string
  intent: string
  message_count: number
  completed: boolean
  pinned: boolean
  created_at: string
  updated_at: string
}

// ── 知识库文档 ───────────────────────────────────────────────────────
/** 文档级向量化状态：空 / 处理中 / 已入向量库 / 部分失败 / 全部失败 */
export type VectorState = 'empty' | 'processing' | 'indexed' | 'partial' | 'failed'

export interface DocumentItem {
  doc_id: string
  object_key: string
  filename: string
  file_ext: string
  file_size: number
  content_hash: string
  user_id: string
  status: string
  chunk_count: number
  created_at: string
  indexed_chunks: number
  pending_chunks: number
  failed_chunks: number
  /** 0-100，切片级索引进度 */
  progress: number
  vector_state: VectorState
}

export interface DocumentStats {
  document_count: number
  ready_documents: number
  failed_documents: number
  chunk_count: number
  indexed_chunks: number
  pending_chunks: number
  failed_chunks: number
  size_bytes: number
  progress: number
}

export interface DocumentListResult {
  documents: DocumentItem[]
  total: number
  stats: DocumentStats | null
}

export interface UploadLimits {
  extensions: string[]
  max_file_size_mb: number
  max_files_per_batch: number
}

/** 单个文件的分阶段上传状态（RAG 是多阶段流水线，不是单条进度条） */
export type UploadStage =
  | 'queued'
  | 'uploading'
  | 'parsing'
  | 'embedding'
  | 'done'
  | 'failed'

export interface UploadTask {
  id: string
  filename: string
  size: number
  stage: UploadStage
  /** 上传字节进度 0-100，仅 uploading 阶段有意义 */
  percent: number
  chunkCount: number
  indexedChunks: number
  error: string
  docId: string
}
