/**
 * Chat Store — 消息流管理。
 *
 * 核心设计：
 * - Map<thread_id, ThreadChat> 结构，切会话不丢消息（修复旧版全局单例问题）
 * - thinking 和 sources 挂在消息上（消息级），不是全局块
 * - 消息状态：streaming | done | error | cancelled
 */
import { defineStore } from 'pinia'
import { reactive, ref } from 'vue'
import type { SourceItem } from '../types/events.gen'

// ── 消息类型 ──────────────────────────────────────────
export type MessageStatus = 'streaming' | 'done' | 'error' | 'cancelled'
export type MessageRole = 'user' | 'assistant'

export interface ThinkingLog {
  node: string
  message: string
  time: string
}

export interface ChatMessage {
  id: string
  role: MessageRole
  content: string
  thinking?: string
  thinkingLogs?: ThinkingLog[]
  sources?: SourceItem[]
  status?: MessageStatus
  nodeId?: string
}

interface ThreadChat {
  messages: ChatMessage[]
  /** 当前 streaming 消息的 ID（用于追加 delta） */
  streamingMessageId: string | null
  /** agent 节点状态时间线 */
  agentTimeline: { node: string; label: string; phase: string; ts: number }[]
  /** 是否正在运行 */
  running: boolean
  /** 错误信息 */
  error: string
}

function createThreadChat(): ThreadChat {
  return {
    messages: [],
    streamingMessageId: null,
    agentTimeline: [],
    running: false,
    error: '',
  }
}

export const useChatStore = defineStore('chat', () => {
  // ── 状态 ──────────────────────────────────────────
  const threads = reactive<Map<string, ThreadChat>>(new Map())
  const currentThreadId = ref('')

  // ── 辅助 ──────────────────────────────────────────
  function getThread(threadId: string): ThreadChat {
    let t = threads.get(threadId)
    if (!t) {
      t = createThreadChat()
      threads.set(threadId, t)
    }
    return t
  }

  function ensureThread(threadId: string): ThreadChat {
    currentThreadId.value = threadId
    return getThread(threadId)
  }

  // ── 消息操作 ──────────────────────────────────────
  function addUserMessage(threadId: string, content: string): string {
    const t = getThread(threadId)
    const id = `u-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`
    t.messages.push({ id, role: 'user', content })
    return id
  }

  function startAssistantMessage(threadId: string, messageId?: string, nodeId?: string): string {
    const t = getThread(threadId)
    const id = messageId || `a-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`
    t.messages.push({
      id,
      role: 'assistant',
      content: '',
      status: 'streaming',
      nodeId,
      thinking: '',
      thinkingLogs: [],
      sources: [],
    })
    t.streamingMessageId = id
    t.running = true
    return id
  }

  function appendDelta(threadId: string, messageId: string, text: string): void {
    const t = getThread(threadId)
    const msg = t.messages.find((m) => m.id === messageId)
    if (msg) msg.content += text
  }

  function appendThinking(threadId: string, messageId: string, text: string): void {
    const t = getThread(threadId)
    const msg = t.messages.find((m) => m.id === messageId)
    if (msg) {
      msg.thinking = (msg.thinking || '') + text
    }
  }

  function appendThinkingLog(threadId: string, node: string, message: string): void {
    const t = getThread(threadId)
    // 追加到当前 streaming 消息，或最后一条 assistant 消息
    const msg = t.messages.find((m) => m.id === t.streamingMessageId) ||
      [...t.messages].reverse().find((m) => m.role === 'assistant')
    if (!msg) return
    if (!msg.thinkingLogs) msg.thinkingLogs = []
    const msg_text = message.trim()
    if (!msg_text) return
    const last = msg.thinkingLogs[msg.thinkingLogs.length - 1]
    if (last && last.message === msg_text && last.node === node) return
    const time = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    msg.thinkingLogs.push({ node, message: msg_text, time })
    if (msg.thinkingLogs.length > 80) {
      msg.thinkingLogs = msg.thinkingLogs.slice(-80)
    }
  }

  function setNodeStatus(threadId: string, data: { node: string; label: string; phase: string }): void {
    const t = getThread(threadId)
    t.agentTimeline.push({ ...data, ts: Date.now() })
  }

  function addSources(threadId: string, sources: SourceItem[]): void {
    const t = getThread(threadId)
    const msg = t.messages.find((m) => m.id === t.streamingMessageId) ||
      [...t.messages].reverse().find((m) => m.role === 'assistant')
    if (msg) {
      if (!msg.sources) msg.sources = []
      msg.sources.push(...sources)
    }
  }

  function finish(threadId: string, _data: { message_id: string; final_state: string }): void {
    const t = getThread(threadId)
    if (t.streamingMessageId) {
      const msg = t.messages.find((m) => m.id === t.streamingMessageId)
      if (msg) msg.status = 'done'
    }
    t.streamingMessageId = null
    t.running = false
    t.error = ''
  }

  function markCancelled(threadId: string): void {
    const t = getThread(threadId)
    if (t.streamingMessageId) {
      const msg = t.messages.find((m) => m.id === t.streamingMessageId)
      if (msg) {
        msg.status = 'cancelled'
        if (!msg.content) msg.content = '（已取消）'
      }
    }
    t.streamingMessageId = null
    t.running = false
  }

  function markError(threadId: string, data: { code: string; message: string }): void {
    const t = getThread(threadId)
    if (t.streamingMessageId) {
      const msg = t.messages.find((m) => m.id === t.streamingMessageId)
      if (msg) {
        msg.status = 'error'
        if (!msg.content) msg.content = `❌ ${data.message}`
      }
    }
    t.streamingMessageId = null
    t.running = false
    t.error = data.message
  }

  function setMessages(threadId: string, messages: ChatMessage[]): void {
    const t = getThread(threadId)
    t.messages = messages
    t.running = false
    t.streamingMessageId = null
  }

  function clearThread(threadId: string): void {
    threads.delete(threadId)
  }

  // ── 选择器 ────────────────────────────────────────
  function getMessages(threadId: string): ChatMessage[] {
    return getThread(threadId).messages
  }

  function isRunning(threadId: string): boolean {
    return getThread(threadId).running
  }

  function getError(threadId: string): string {
    return getThread(threadId).error
  }

  function getAgentTimeline(threadId: string) {
    return getThread(threadId).agentTimeline
  }

  return {
    threads,
    currentThreadId,
    ensureThread,
    addUserMessage,
    startAssistantMessage,
    appendDelta,
    appendThinking,
    appendThinkingLog,
    setNodeStatus,
    addSources,
    finish,
    markCancelled,
    markError,
    setMessages,
    clearThread,
    getMessages,
    isRunning,
    getError,
    getAgentTimeline,
  }
})
