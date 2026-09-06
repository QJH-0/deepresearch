/**
 * useEventStream — 统一事件 reducer（run/resume 共用）+ 断线重连状态机。
 *
 * 核心设计：
 * 1. run 与 resume 共用 consume——两入口只是请求不同，事件处理链唯一
 * 2. 消息模型：thinking 与 sources 挂在消息上（消息级），不是全局块
 * 3. SSE 帧解析健壮性：跨 chunk 的半行 JSON 缓冲处理
 * 4. 未知 type 静默忽略（向前兼容，不变式③）
 * 5. 断线重连：run 主入口非 AbortError 网络错误触发指数退避自动重连，
 *    通过 GET /threads/{id}/state 检查可恢复性，消息对齐后调 resume 续流
 */
import { useChatStore } from '../stores/chat'
import { useInterruptStore } from '../stores/interrupt'
import { useThreadsStore } from '../stores/threads'
import { consumeSSE, postStream } from '../api/sse'
import { fetchThreadState, fetchThreadMessages } from '../api/rest'
import type { EventEnvelope, EventDataMap, EventType } from '../types/events.gen'

const RECONNECT_MAX_ATTEMPTS = 5
const RECONNECT_BASE_DELAY_MS = 1000
const RECONNECT_MAX_DELAY_MS = 30000

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function isAbortError(err: unknown): boolean {
  return err instanceof Error && err.name === 'AbortError'
}

export function useEventStream() {
  const chat = useChatStore()
  const intr = useInterruptStore()
  const threads = useThreadsStore()

  /** 每个线程的重连尝试次数 */
  const reconnectAttempts = new Map<string, number>()

  function getAttempts(threadId: string): number {
    return reconnectAttempts.get(threadId) || 0
  }

  function setAttempts(threadId: string, n: number): void {
    reconnectAttempts.set(threadId, n)
  }

  /** 判断用户是否已主动取消（chat store 存在被标记 cancelled 的消息） */
  function isUserCancelled(threadId: string): boolean {
    const t = chat.threads.get(threadId)
    if (!t) return false
    // 只有存在明确被标记为 cancelled 的 streaming 消息才算用户取消
    const streaming = t.streamingMessageId
    if (streaming) {
      const msg = t.messages.find((m) => m.id === streaming)
      return msg?.status === 'cancelled'
    }
    // 检查最后一条 assistant 消息是否为 cancelled
    const lastAssistant = [...t.messages].reverse().find((m) => m.role === 'assistant')
    return lastAssistant?.status === 'cancelled'
  }

  /**
   * 统一事件分发器 — 根据事件类型调用对应 store 方法。
   */
  function dispatch(threadId: string, env: EventEnvelope): void {
    switch (env.type as EventType) {
      case 'run.started': {
        chat.ensureThread(threadId)
        chat.setRunning(threadId)
        break
      }
      case 'agent.status': {
        const d = env.data as EventDataMap['agent.status']
        chat.setNodeStatus(threadId, d)
        chat.appendThinkingLog(threadId, d.node, d.label)
        break
      }
      case 'message.start': {
        const d = env.data as EventDataMap['message.start']
        if (!chat.getMessages(threadId).find((m) => m.id === d.message_id)) {
          chat.startAssistantMessage(threadId, d.message_id, d.node)
        }
        break
      }
      case 'message.delta': {
        const d = env.data as EventDataMap['message.delta']
        // 容错：delta 先于 message.start 到达时惰性初始化
        if (!chat.getMessages(threadId).find((m) => m.id === d.message_id)) {
          chat.startAssistantMessage(threadId, d.message_id)
        }
        chat.appendDelta(threadId, d.message_id, d.text)
        break
      }
      case 'message.thinking': {
        const d = env.data as EventDataMap['message.thinking']
        chat.appendThinking(threadId, d.message_id, d.text)
        break
      }
      case 'sources.found': {
        const d = env.data as EventDataMap['sources.found']
        chat.addSources(threadId, d.sources)
        break
      }
      case 'interrupt.raised': {
        const d = env.data as EventDataMap['interrupt.raised']
        intr.raise(threadId, d)
        break
      }
      case 'run.completed': {
        const d = env.data as EventDataMap['run.completed']
        chat.finish(threadId, d)
        void threads.refresh(threadId)
        break
      }
      case 'run.cancelled': {
        chat.markCancelled(threadId)
        void threads.refresh(threadId)
        break
      }
      case 'run.error': {
        const d = env.data as EventDataMap['run.error']
        chat.markError(threadId, d)
        void threads.refresh(threadId)
        break
      }
      default:
        break
    }
  }

  /**
   * 消费 SSE Response — run/resume 共用。
   * 不在此层捕获非 AbortError 错误，交给 runWithReconnect 决策。
   */
  async function consume(threadId: string, resp: Response): Promise<void> {
    await consumeSSE(
      resp,
      (env) => dispatch(threadId, env),
      () => { /* onDone */ },
      (err) => {
        if (!isAbortError(err)) {
          chat.markError(threadId, { code: 'CLIENT_ERROR', message: err.message })
        }
      },
    )
  }

  /**
   * 消息对齐：以服务端返回为唯一事实，整体替换本地消息列表。
   */
  async function syncThreadMessages(threadId: string): Promise<void> {
    try {
      const data = await fetchThreadMessages(threadId)
      chat.replaceThreadMessages(threadId, data.messages || [])
    } catch {
      // 消息同步失败不阻断重连流程
    }
  }

  /**
   * 指数退避重连调度：检查状态 → 消息对齐 → resume 续流。
   */
  async function scheduleReconnect(threadId: string): Promise<void> {
    const attempts = getAttempts(threadId) + 1
    setAttempts(threadId, attempts)

    if (attempts > RECONNECT_MAX_ATTEMPTS) {
      chat.markError(threadId, { code: 'RECONNECT_FAILED', message: '连接已断开，自动恢复失败' })
      chat.setReconnecting(threadId, false)
      return
    }

    const delay = Math.min(
      RECONNECT_BASE_DELAY_MS * 2 ** (attempts - 1),
      RECONNECT_MAX_DELAY_MS,
    )
    chat.setReconnecting(threadId, true)
    await sleep(delay)

    // 重连前检查：用户可能已切走或发起新 run
    if (threads.currentThreadId !== threadId) {
      chat.setReconnecting(threadId, false)
      return
    }
    if (isUserCancelled(threadId)) {
      chat.setReconnecting(threadId, false)
      return
    }

    // ① 状态检查：决定是否继续重连
    let state
    try {
      state = await fetchThreadState(threadId)
    } catch {
      // 状态接口失败，继续递归重试
      await scheduleReconnect(threadId)
      return
    }

    if (state.status === 'awaiting_input') {
      // HITL 中断态：中断卡片通过 interrupt store 重建，停止重连
      chat.setReconnecting(threadId, false)
      setAttempts(threadId, 0)
      void intr.rebuild(threadId)
      return
    }

    if (!state.resumable) {
      // 已结束或不可恢复：拉一次消息对齐后停止
      await syncThreadMessages(threadId)
      chat.setReconnecting(threadId, false)
      return
    }

    // ② 消息对齐：清除旧的半截 streaming 消息
    await syncThreadMessages(threadId)

    // ③ resume 续流（mode=continue）
    try {
      const resp = await postStream('/api/v1/research/resume', {
        thread_id: threadId,
        mode: 'continue',
      })
      chat.setReconnecting(threadId, false)
      await consumeSSE(
        resp,
        (env) => dispatch(threadId, env),
        () => {
          setAttempts(threadId, 0)
        },
        (err) => {
          if (!isAbortError(err)) {
            chat.markError(threadId, { code: 'CLIENT_ERROR', message: err.message })
          }
        },
      )
      setAttempts(threadId, 0)
    } catch (err) {
      if (isAbortError(err) || isUserCancelled(threadId)) {
        chat.setReconnecting(threadId, false)
        return
      }
      await scheduleReconnect(threadId)
    }
  }

  /** 发起新研究（含断线重连） */
  async function run(threadId: string, query: string, options?: {
    user_id?: string
    tenant_id?: string
    hitl_enabled?: boolean
  }): Promise<void> {
    chat.ensureThread(threadId)
    chat.setUserStopped(threadId, false)
    chat.addUserMessage(threadId, query)
    void threads.refresh()

    try {
      const resp = await postStream('/api/v1/research/stream', {
        query,
        user_id: options?.user_id || threads.userId,
        thread_id: threadId,
        tenant_id: options?.tenant_id || 'default_tenant',
        hitl_enabled: options?.hitl_enabled ?? false,
      })
      await consume(threadId, resp)
      setAttempts(threadId, 0)
    } catch (err) {
      if (isAbortError(err) || isUserCancelled(threadId)) return
      if (getAttempts(threadId) >= RECONNECT_MAX_ATTEMPTS) {
        chat.markError(threadId, { code: 'RECONNECT_FAILED', message: '连接已断开，自动恢复失败' })
        return
      }
      await scheduleReconnect(threadId)
    }
  }

  /**
   * 智能续流入口：区分"续流上次任务"和"开始新任务"。
   *
   * 逻辑：
   * 1. 用户手动停止后（userStopped=true），再次输入含"继续/continue/resume"等关键词 → 续流
   * 2. 用户手动停止后，输入其他内容 → 新任务
   * 3. 非手动停止场景（如网络断开后的重连）→ 由 scheduleReconnect 处理，不走此入口
   */
  const RESUME_KEYWORDS = ['继续', '续流', 'resume', 'continue', '接着', '接着来', 'go on', 'proceed']

  function matchesResumeKeyword(query: string): boolean {
    const normalized = query.trim().toLowerCase()
    return RESUME_KEYWORDS.some((kw) => normalized.includes(kw.toLowerCase()))
  }

  async function runOrResume(threadId: string, query: string, options?: {
    user_id?: string
    tenant_id?: string
    hitl_enabled?: boolean
  }): Promise<void> {
    // 只有用户手动停止后，才需要判断是续流还是新任务
    if (chat.isUserStopped(threadId)) {
      if (matchesResumeKeyword(query)) {
        // 关键词匹配 → 尝试续流
        try {
          const state = await fetchThreadState(threadId)
          if (state.resumable && state.status !== 'running') {
            chat.ensureThread(threadId)
            chat.addUserMessage(threadId, query)
            void threads.refresh()
            await syncThreadMessages(threadId)
            const resp = await postStream('/api/v1/research/resume', {
              thread_id: threadId,
              mode: 'continue',
            })
            chat.setUserStopped(threadId, false)
            await consume(threadId, resp)
            setAttempts(threadId, 0)
            return
          }
        } catch {
          // 状态检查失败，降级走新 run
        }
      }
      // 不匹配关键词 或 checkpoint 不可恢复 → 新任务，清除标记
      chat.setUserStopped(threadId, false)
    }
    // 非手动停止场景，或关键词不匹配 → 新任务
    await run(threadId, query, options)
  }

  /** 恢复中断的研究（HITL 入口，不套重连状态机） */
  async function resume(threadId: string, payload: Record<string, unknown>): Promise<void> {
    intr.clear(threadId)
    chat.ensureThread(threadId)

    const resp = await postStream('/api/v1/research/resume', {
      thread_id: threadId,
      resume_value: payload,
    })
    await consume(threadId, resp)
  }

  return { run, resume, runOrResume, consume, dispatch, scheduleReconnect, syncThreadMessages }
}
