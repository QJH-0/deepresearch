/**
 * useEventStream — 统一事件 reducer（run/resume 共用）。
 *
 * 核心设计：
 * 1. run 与 resume 共用 consume——两入口只是请求不同，事件处理链唯一
 * 2. 消息模型：thinking 与 sources 挂在消息上（消息级），不是全局块
 * 3. SSE 帧解析健壮性：跨 chunk 的半行 JSON 缓冲处理
 * 4. 未知 type 静默忽略（向前兼容，不变式③）
 */
import { useChatStore } from '../stores/chat'
import { useInterruptStore } from '../stores/interrupt'
import { useThreadsStore } from '../stores/threads'
import { consumeSSE, postStream } from '../api/sse'
import type { EventEnvelope, EventDataMap, EventType } from '../types/events.gen'

export function useEventStream() {
  const chat = useChatStore()
  const intr = useInterruptStore()
  const threads = useThreadsStore()

  /**
   * 统一事件分发器 — 根据事件类型调用对应 store 方法。
   * 这就是消灭 ChatView 两段重复事件处理的唯一处理逻辑。
   */
  function dispatch(threadId: string, env: EventEnvelope): void {
    switch (env.type as EventType) {
      case 'run.started': {
        chat.ensureThread(threadId)
        chat.startAssistantMessage(threadId)
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
        // 如果还没有 streaming 消息，创建一个
        if (!chat.getMessages(threadId).find((m) => m.id === d.message_id)) {
          chat.startAssistantMessage(threadId, d.message_id, d.node)
        }
        break
      }
      case 'message.delta': {
        const d = env.data as EventDataMap['message.delta']
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
        // 事件驱动刷新会话列表（标题可能已自动生成）
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
        // 不变式③：未知 type 静默忽略
        break
    }
  }

  /**
   * 消费 SSE Response — run/resume 共用。
   */
  async function consume(threadId: string, resp: Response): Promise<void> {
    await consumeSSE(
      resp,
      (env) => dispatch(threadId, env),
      () => { /* onDone: 可选回调 */ },
      (err) => {
        chat.markError(threadId, { code: 'CLIENT_ERROR', message: err.message })
      },
    )
  }

  /** 发起新研究 */
  async function run(threadId: string, query: string, options?: {
    user_id?: string
    tenant_id?: string
    hitl_enabled?: boolean
  }): Promise<void> {
    chat.ensureThread(threadId)
    chat.addUserMessage(threadId, query)
    void threads.refresh()

    const resp = await postStream('/api/v1/research/stream', {
      query,
      user_id: options?.user_id || threads.userId,
      thread_id: threadId,
      tenant_id: options?.tenant_id || 'default_tenant',
      hitl_enabled: options?.hitl_enabled ?? false,
    })
    await consume(threadId, resp)
  }

  /** 恢复中断的研究 */
  async function resume(threadId: string, payload: Record<string, unknown>): Promise<void> {
    // 清除 interrupt 状态
    intr.clear(threadId)
    chat.ensureThread(threadId)

    const resp = await postStream('/api/v1/research/resume', {
      thread_id: threadId,
      resume_value: payload,
    })
    await consume(threadId, resp)
  }

  return { run, resume, consume, dispatch }
}
