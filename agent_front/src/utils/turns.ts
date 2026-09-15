/**
 * 把消息流按「轮」分组，供对话页渲染。
 *
 * 分组规则（与后端约定一致）：
 * - 后端中间节点的产出是 JSON 结构化结果，不进消息流；因此
 *   **只有带正文的助手消息才是答案**，其余助手消息都是过程节点。
 * - 一轮 = 用户提问 → 若干过程节点 → 最终答案。
 *
 * 过程步骤优先取 **agent 状态时间线**：它由 `agent.status` 事件驱动、每次运行
 * 都会完整记录；而挂在消息上的 thinkingLogs 在没有助手消息时会被丢弃
 * （例如直答路径只有 intent 一个中间节点、且它不产生正文），
 * 不能作为过程步骤的唯一来源。时间线缺失时（如从服务端加载的历史会话）
 * 回退到按消息推导。
 *
 * 抽成纯函数而非写在组件 computed 里，是为了能直接单测分组规则
 * —— 这是「过程收进卡片、正文只留答案」这一体验的核心逻辑。
 */

export type ProcessStepState = 'running' | 'done' | 'error' | 'pending'

export interface ProcessStep {
  /** 节点标识（intent / plan / web_search …） */
  node: string
  /** 展示名 */
  label: string
  /** 该节点的过程要点（按时间顺序） */
  details: string[]
  state: ProcessStepState
}

/** 分组所需的最小消息形状（与 chat store 的 ChatMessage 结构兼容） */
export interface TurnMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  thinkingLogs?: { node: string; message: string; time: string }[]
  status?: 'streaming' | 'done' | 'error' | 'cancelled'
  nodeId?: string
  /** 发送该条用户消息时的时间线长度，用于把时间线切分到轮 */
  timelineStart?: number
}

/** agent 状态时间线条目 */
export interface TimelineEntry {
  node: string
  label: string
  phase: string
  /** 节点自报的进度文案，作为步骤细节展示 */
  detail?: string
  ts: number
}

export interface Turn<T extends TurnMessage = TurnMessage> {
  key: string
  user: T | null
  steps: ProcessStep[]
  thinking: string
  answer: T | null
}

function stateFromMessage(message: TurnMessage): ProcessStepState {
  if (message.status === 'streaming') return 'running'
  if (message.status === 'error' || message.status === 'cancelled') return 'error'
  return 'done'
}

function stateFromPhase(phase: string): ProcessStepState {
  if (phase === 'completed') return 'done'
  if (phase === 'error' || phase === 'failed') return 'error'
  return 'running'
}

/** 把节点日志按节点归并，供时间线步骤补充细节 */
function collectLogsByNode(messages: readonly TurnMessage[]): Record<string, string[]> {
  const byNode: Record<string, string[]> = {}
  for (const message of messages) {
    for (const log of message.thinkingLogs || []) {
      if (!log.message) continue
      ;(byNode[log.node] ||= []).push(log.message)
    }
  }
  return byNode
}

/**
 * 按时间线切片的出现顺序产出步骤：同一节点只保留一行，状态取最后一次 phase。
 *
 * 细节优先取时间线自带的 detail（节点自报的进度文案，如「意图判定完成: multiagent」），
 * 再补上挂在消息上的同节点日志；两者都为空时该步骤只显示节点名。
 */
function stepsFromTimeline(
  slice: readonly TimelineEntry[],
  labels: Record<string, string>,
  logsByNode: Record<string, string[]>,
): ProcessStep[] {
  const order: string[] = []
  const phaseOf: Record<string, string> = {}
  const detailsOf: Record<string, string[]> = {}

  for (const entry of slice) {
    if (!entry.node) continue
    let collected = detailsOf[entry.node]
    if (!collected) {
      order.push(entry.node)
      collected = []
      detailsOf[entry.node] = collected
    }
    phaseOf[entry.node] = entry.phase
    const detail = (entry.detail || '').trim()
    if (detail && !collected.includes(detail)) collected.push(detail)
  }

  return order.map((node) => {
    const merged = [...(detailsOf[node] || []), ...(logsByNode[node] || [])]
    return {
      node,
      label: labels[node] || node,
      details: [...new Set(merged)],
      state: stateFromPhase(phaseOf[node] ?? 'running'),
    }
  })
}

/** 时间线不可用时的回退：按过程消息推导步骤 */
function stepsFromMessages(
  messages: readonly TurnMessage[],
  labels: Record<string, string>,
): ProcessStep[] {
  return messages
    .filter((message) => message.nodeId)
    .map((message) => {
      const node = message.nodeId as string
      return {
        node,
        label: labels[node] || node,
        details: (message.thinkingLogs || []).map((log) => log.message).filter(Boolean),
        state: stateFromMessage(message),
      }
    })
}

export function groupTurns<T extends TurnMessage>(
  messages: readonly T[],
  labels: Record<string, string> = {},
  timeline: readonly TimelineEntry[] = [],
): Turn<T>[] {
  // 第一遍：按用户消息切分成轮，记录每轮的起止与过程消息
  interface Bucket {
    key: string
    user: T | null
    timelineStart: number
    processMessages: T[]
    thinking: string
    answer: T | null
  }

  const buckets: Bucket[] = []
  let current: Bucket | null = null

  for (const message of messages) {
    if (message.role === 'user') {
      current = {
        key: message.id,
        user: message,
        timelineStart: message.timelineStart ?? timeline.length,
        processMessages: [],
        thinking: '',
        answer: null,
      }
      buckets.push(current)
      continue
    }
    if (!current) {
      current = {
        key: `lead-${message.id}`,
        user: null,
        timelineStart: 0,
        processMessages: [],
        thinking: '',
        answer: null,
      }
      buckets.push(current)
    }
    if (message.content) {
      current.answer = message
      continue
    }
    current.processMessages.push(message)
    if (message.thinking) current.thinking += message.thinking
  }

  // 第二遍：把时间线按轮切分，产出步骤
  return buckets.map((bucket, index) => {
    const start = bucket.timelineStart
    const end = buckets[index + 1]?.timelineStart ?? timeline.length
    const slice = end > start ? timeline.slice(start, end) : []
    const logsByNode = collectLogsByNode(bucket.processMessages)

    return {
      key: bucket.key,
      user: bucket.user,
      answer: bucket.answer,
      thinking: bucket.thinking,
      steps: slice.length
        ? stepsFromTimeline(slice, labels, logsByNode)
        : stepsFromMessages(bucket.processMessages, labels),
    }
  })
}
