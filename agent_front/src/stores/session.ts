/**
 * 会话状态单例（跨组件共享）。
 *
 * 侧边栏和对话页需要同一份「会话列表 + 当前会话」，用 provide/inject
 * 或父子传参都要绕一圈，这里直接用一个模块级单例，语义最简单。
 */
import { ref } from 'vue'
import type { ThreadItem } from '../types'
import type { ThinkingLog, ThinkingState } from '../components/chat/ThinkingBlock.vue'
import {
  deleteThread as apiDeleteThread,
  fetchThreads,
  getUserId,
  pinThread as apiPinThread,
  renameThread as apiRenameThread,
  setUserId as persistUserId,
} from '../api'

const userId = ref(getUserId())
const threads = ref<ThreadItem[]>([])
const currentThreadId = ref('')
const threadsLoading = ref(false)
const threadsError = ref('')
/**
 * 「新建会话」信号。
 * 侧边栏在对话页之外的路由上也可能点新建，用信号通知对话页，
 * 避免父组件去拿子组件实例（路由切换过程中拿到的还是旧组件）。
 */
const newChatSignal = ref(0)

/**
 * 思考块全局状态。
 *
 * 存储在 store 中（而非组件内），确保切换会话再切回时：
 * - 如果任务未完成，思考块状态得以保留
 * - 如果任务已完成，思考块会自动隐藏
 */
const thinkingBlock = ref<{
  threadId: string
  userMessageId: string
  state: ThinkingState
  logs: ThinkingLog[]
} | null>(null)

/** 从后端拉取会话列表（后端已按「置顶优先 + 最近活跃倒序」返回） */
async function loadThreads(keyword = ''): Promise<void> {
  threadsLoading.value = true
  threadsError.value = ''
  try {
    const data = await fetchThreads(keyword)
    threads.value = data.threads || []
  } catch (err) {
    threadsError.value = err instanceof Error ? err.message : '加载会话历史失败'
    threads.value = []
  } finally {
    threadsLoading.value = false
  }
}

/**
 * 新建会话。
 *
 * 关键点：只重置「当前会话」，不动 threads 列表 —— 历史会话始终留在侧边栏，
 * 这正是之前版本的 bug（新建后历史历史就找不到了）。
 */
function startNewThread(): string {
  const id = `thread_${Date.now()}`
  currentThreadId.value = id
  return id
}

function requestNewChat(): void {
  newChatSignal.value += 1
}

function selectThread(threadId: string): void {
  currentThreadId.value = threadId
}

/**
 * 设置思考块（对话页调用）。
 */
function setThinkingBlock(
  threadId: string,
  userMessageId: string,
  state: ThinkingState,
  logs: ThinkingLog[] = [],
): void {
  thinkingBlock.value = { threadId, userMessageId, state, logs }
}

/**
 * 更新思考块状态。
 */
function updateThinkingBlock(state: ThinkingState, logs?: ThinkingLog[]): void {
  if (!thinkingBlock.value) return
  thinkingBlock.value.state = state
  if (logs) thinkingBlock.value.logs = logs
}

/**
 * 追加思考日志。
 */
function appendThinkingLog(message: string, node = '') {
  if (!thinkingBlock.value) return
  const msg = message.trim()
  if (!msg) return
  const logs = thinkingBlock.value.logs
  const last = logs[logs.length - 1]
  if (last && last.message === msg && last.node === node) return
  const time = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  logs.push({ node, message: msg, time })
  if (logs.length > 60) {
    thinkingBlock.value.logs = logs.slice(-60)
  }
}

/**
 * 清除思考块。
 */
function clearThinkingBlock(): void {
  thinkingBlock.value = null
}

/**
 * 获取当前会话对应的思考块。
 *
 * 如果思考块 belong 于当前会话则返回，否则返回 null。
 */
function getThinkingBlockForCurrentThread(): typeof thinkingBlock.value {
  if (!thinkingBlock.value) return null
  if (thinkingBlock.value.threadId !== currentThreadId.value) return null
  return thinkingBlock.value
}

async function renameThread(threadId: string, title: string): Promise<boolean> {
  const trimmed = title.trim()
  if (!trimmed) return false
  // 先本地改，失败再回滚 —— 重命名是高频小操作，不该等网络往返
  const target = threads.value.find((t) => t.thread_id === threadId)
  const previous = target?.title
  if (target) target.title = trimmed

  try {
    const updated = await apiRenameThread(threadId, trimmed)
    if (updated) target && Object.assign(target, updated)
    return true
  } catch {
    if (target && previous !== undefined) target.title = previous
    return false
  }
}

async function togglePin(threadId: string, pinned: boolean): Promise<void> {
  const target = threads.value.find((t) => t.thread_id === threadId)
  if (target) target.pinned = pinned
  try {
    const updated = await apiPinThread(threadId, pinned)
    if (updated && target) Object.assign(target, updated)
  } catch {
    // 置顶失败只影响排序，刷新后会自愈，不打断用户
  }
}

async function removeThread(threadId: string): Promise<boolean> {
  const removed = threads.value.find((t) => t.thread_id === threadId)
  threads.value = threads.value.filter((t) => t.thread_id !== threadId)
  try {
    const result = await apiDeleteThread(threadId)
    if (!result.deleted && removed) threads.value = [removed, ...threads.value]
    if (result.deleted && currentThreadId.value === threadId) currentThreadId.value = ''
    return result.deleted
  } catch {
    if (removed) threads.value = [removed, ...threads.value]
    return false
  }
}

function changeUserId(next: string): void {
  userId.value = next.trim() || 'default_user'
  persistUserId(userId.value)
  currentThreadId.value = ''
  void loadThreads()
}

export function useSession() {
  return {
    userId,
    threads,
    currentThreadId,
    threadsLoading,
    threadsError,
    newChatSignal,
    thinkingBlock,
    loadThreads,
    startNewThread,
    requestNewChat,
    selectThread,
    renameThread,
    togglePin,
    removeThread,
    changeUserId,
    setThinkingBlock,
    updateThinkingBlock,
    appendThinkingLog,
    clearThinkingBlock,
    getThinkingBlockForCurrentThread,
  }
}
