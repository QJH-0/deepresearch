/**
 * 事件协议类型定义（从后端 event-protocol.json 自动对应生成）。
 *
 * 单一信源：后端 backend/schemas/events.py → event-protocol.json → 本文件。
 * 前端所有事件处理代码引用本文件类型，杜绝前后端字段不一致。
 */

// ── 事件类型枚举 ──────────────────────────────────────
export type EventType =
  | 'run.started'
  | 'agent.status'
  | 'message.start'
  | 'message.delta'
  | 'message.thinking'
  | 'sources.found'
  | 'interrupt.raised'
  | 'run.completed'
  | 'run.cancelled'
  | 'run.error'

// ── 事件 data 类型 ────────────────────────────────────
export interface RunStartedData {
  thread_id: string
  run_id: string
}

export interface AgentStatusData {
  node: string
  label: string
  phase: string
}

export interface MessageStartData {
  message_id: string
  role?: string
  node?: string
}

export interface MessageDeltaData {
  message_id: string
  text: string
}

export interface MessageThinkingData {
  message_id: string
  text: string
}

export interface SourceItem {
  url?: string | null
  title?: string
  snippet?: string
  source_type?: 'web' | 'kb'
  chunk_id?: string | null
}

export interface SourcesFoundData {
  sources: SourceItem[]
}

export type InterruptKind = 'plan_approval' | 'clarification' | 'report_review'

export interface InterruptRaisedData {
  interrupt_id: string
  kind: InterruptKind
  payload: Record<string, unknown>
}

export interface RunCompletedData {
  message_id: string
  final_state: string
}

export interface RunCancelledData {
  reason: string
}

export interface RunErrorData {
  code: string
  message: string
}

// ── 事件信封 ──────────────────────────────────────────
export interface EventEnvelope<T extends EventType = EventType, D = unknown> {
  type: T
  ts: number
  data: D
}

// ── 类型映射表（type → data 类型） ────────────────────
export interface EventDataMap {
  'run.started': RunStartedData
  'agent.status': AgentStatusData
  'message.start': MessageStartData
  'message.delta': MessageDeltaData
  'message.thinking': MessageThinkingData
  'sources.found': SourcesFoundData
  'interrupt.raised': InterruptRaisedData
  'run.completed': RunCompletedData
  'run.cancelled': RunCancelledData
  'run.error': RunErrorData
}

/** 提取特定事件类型对应的 data 类型 */
export type EventData<T extends EventType> = EventDataMap[T]

/** 通用事件处理函数类型 */
export type EventHandler<T extends EventType = EventType> = (
  data: EventData<T>,
  envelope: EventEnvelope,
) => void
