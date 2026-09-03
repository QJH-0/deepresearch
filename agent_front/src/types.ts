/**
 * 全局类型定义（重构版）。
 *
 * 事件类型从 events.gen.ts 统一导出。
 * 旧代码的 import type { StreamEvent } from '../types' 仍可工作。
 * 消息类型从 stores/chat.ts 统一导出。
 */

// ── 兼容导出：旧事件类型（新代码请用 types/events.gen.ts） ──────────
import type { EventEnvelope, EventType } from './types/events.gen'

export type StreamEventType = EventType
export type StreamEvent = EventEnvelope

// ── 消息（兼容旧代码，新代码用 stores/chat.ts 的 ChatMessage） ────────
export type MessageRole = 'user' | 'assistant' | 'status'

export interface ChatMessage {
  id: string
  role: MessageRole
  content: string
  collapsed?: boolean
}

// ── 会话历史 ──────────────────────────────────────────
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

// ── 知识库文档 ────────────────────────────────────────
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

export type UploadStage = 'queued' | 'uploading' | 'parsing' | 'embedding' | 'done' | 'failed'

export interface UploadTask {
  id: string
  filename: string
  size: number
  stage: UploadStage
  percent: number
  chunkCount: number
  indexedChunks: number
  error: string
  docId: string
}
