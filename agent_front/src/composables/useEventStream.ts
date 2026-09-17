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

/** 每个线程的 messageId 映射表：将后端各节点的 message_id 映射到主气泡 ID，实现多节点 token 合并到一个气泡 */
const messageIdMap = new Map<string, Map<string, string>>()

function getMsgIdMap(threadId: string): Map<string, string> {
  let m = messageIdMap.get(threadId)
  if (!m) {
    m = new Map()
    messageIdMap.set(threadId, m)
  }
  return m
}

function clearMsgIdMap(threadId: string): void {
  messageIdMap.delete(threadId)
}

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
   *
   * 多气泡合并策略：同一次 run 内各节点（plan/deep_dive/analyze/write）的
   * message.start + message.delta 事件统一合并到第一个节点创建的主气泡上，
   * 避免一次研究产出多个气泡。
   */
  function dispatch(threadId: string, env: EventEnvelope): void {
    const idMap = getMsgIdMap(threadId)

    switch (env.type as EventType) {
      case 'run.started': {
        chat.ensureThread(threadId)
        chat.setRunning(threadId)
        clearMsgIdMap(threadId)
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
        if (!idMap.has(d.message_id)) {
          if (idMap.size === 0) {
            // 第一个 message.start → 创建主气泡
            chat.startAssistantMessage(threadId, d.message_id, d.node)
            idMap.set(d.message_id, d.message_id)
          } else {
            // 后续节点的 message.start → 映射到主气泡，不创建新气泡
            const primaryId = idMap.values().next().value!
            idMap.set(d.message_id, primaryId)
          }
        }
        break
      }
      case 'message.delta': {
        const d = env.data as EventDataMap['message.delta']
        // 通过映射表找到主气泡 ID，没有则惰性初始化
        let primaryId = idMap.get(d.message_id)
        if (!primaryId) {
          if (idMap.size === 0) {
            chat.startAssistantMessage(threadId, d.message_id)
            idMap.set(d.message_id, d.message_id)
            primaryId = d.message_id
          } else {
            primaryId = idMap.values().next().value!
            idMap.set(d.message_id, primaryId)
          }
        }
        chat.appendDelta(threadId, primaryId, d.text)
        break
      }
      case 'message.thinking': {
        const d = env.data as EventDataMap['message.thinking']
        const primaryId = idMap.get(d.message_id) || d.message_id
        chat.appendThinking(threadId, primaryId, d.text)
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
        clearMsgIdMap(threadId)
        void threads.refresh()
        break
      }
      case 'run.cancelled': {
        chat.markCancelled(threadId)
        clearMsgIdMap(threadId)
        void threads.refresh()
        break
      }
      case 'run.error': {
        const d = env.data as EventDataMap['run.error']
        chat.markError(threadId, d)
        clearMsgIdMap(threadId)
        void threads.refresh()
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
   * 停止后续研的归宿。
   *
   * 关键词分流是「猜用户意图」，停止态输入框里的两个显式入口是「用户明确表态」。
   * 两者共存：随手打字走关键词，想明确表态就点按钮。关键词分流的已知误判
   * （例如「继续调研AI」既像续流又像新任务）由显式入口兜底。
   */
  const RESUME_KEYWORDS = ['继续', '续流', 'resume', 'continue', '接着', '接着来', 'go on', 'proceed']

  function matchesResumeKeyword(query: string): boolean {
    const normalized = query.trim().toLowerCase()
    return RESUME_KEYWORDS.some((kw) => normalized.includes(kw.toLowerCase()))
  }

  /** checkpoint 是否真的能续。不能续时任何续流入口都必须退回新研究，不能吞掉用户输入 */
  async function canResumeFromCheckpoint(threadId: string): Promise<boolean> {
    try {
      const state = await fetchThreadState(threadId)
      return Boolean(state.resumable) && state.status !== 'running'
    } catch {
      return false
    }
  }

  /** 续流：从最后 checkpoint 的节点接着跑，已检索的证据与已形成的结论全部保留 */
  async function continueFromCheckpoint(threadId: string): Promise<void> {
    chat.setUserStopped(threadId, false)
    await syncThreadMessages(threadId)
    try {
      const resp = await postStream('/api/v1/research/resume', {
        thread_id: threadId,
        mode: 'continue',
      })
      await consume(threadId, resp)
      setAttempts(threadId, 0)
    } catch (err) {
      if (isAbortError(err) || isUserCancelled(threadId)) return
      await scheduleReconnect(threadId)
    }
  }

  /** 补条件续研：把新条件追加进 state 后从入口重跑，旧的检索结果仍在 */
  async function resumeWithModify(threadId: string, condition: string): Promise<void> {
    chat.setUserStopped(threadId, false)
    await syncThreadMessages(threadId)
    try {
      const resp = await postStream('/api/v1/research/resume', {
        thread_id: threadId,
        mode: 'modify',
        resume_value: condition,
      })
      await consume(threadId, resp)
      setAttempts(threadId, 0)
    } catch (err) {
      if (isAbortError(err) || isUserCancelled(threadId)) return
      await scheduleReconnect(threadId)
    }
  }

  async function runOrResume(threadId: string, query: string, options?: {
    user_id?: string
    tenant_id?: string
    hitl_enabled?: boolean
  }): Promise<void> {
    if (chat.isUserStopped(threadId) && (await canResumeFromCheckpoint(threadId))) {
      chat.ensureThread(threadId)
      chat.addUserMessage(threadId, query)
      void threads.refresh()

      if (matchesResumeKeyword(query)) {
        await continueFromCheckpoint(threadId)
      } else {
        await resumeWithModify(threadId, query)
      }
      return
    }

    // 非手动停止，或 checkpoint 已不可续 → 新任务
    await run(threadId, query, options)
  }

  /**
   * 显式入口（停止态输入框）：「补充条件继续研究」→ mode=modify。
   *
   * 不依赖关键词匹配 —— 点这个按钮就是明确要「在原有研究上补条件」。
   * checkpoint 不可续时退回新研究，避免把用户输入丢掉。
   */
  async function resumeWithCondition(threadId: string, condition: string, options?: {
    user_id?: string
    tenant_id?: string
    hitl_enabled?: boolean
  }): Promise<void> {
    if (!(await canResumeFromCheckpoint(threadId))) {
      await run(threadId, condition, options)
      return
    }
    chat.ensureThread(threadId)
    chat.addUserMessage(threadId, condition)
    void threads.refresh()
    await resumeWithModify(threadId, condition)
  }

  /**
   * 显式入口（停止态输入框）：「换个主题重新研究」→ 开新 thread 走 /run。
   *
   * 必须开新 thread：复用当前 thread 会让上一次研究的证据与结论混进新主题，
   * 报告里出现与主题无关的引用。旧 thread 与其 checkpoint 保持不动，用户仍可切回续研。
   */
  async function startNewTopic(topic: string, options?: {
    user_id?: string
    tenant_id?: string
    hitl_enabled?: boolean
  }): Promise<void> {
    const newThreadId = threads.startNewThread()
    await run(newThreadId, topic, options)
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

  return {
    run, resume, runOrResume, resumeWithCondition, startNewTopic,
    consume, dispatch, scheduleReconnect, syncThreadMessages,
  }
}
