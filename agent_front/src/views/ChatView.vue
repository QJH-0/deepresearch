<script setup lang="ts">
/**
 * 对话页 — 仿 DeepSeek 思考模式。
 *
 * 核心设计：
 * - 对话界面只显示用户提问 + 模型最终回答
 * - 中间过程全部进入可折叠的「思考气泡」（ThinkingBlock），挂在用户消息下方
 * - 思考气泡实时显示执行进度，任务完成后可展开回看
 * - 切换会话时正确加载历史；切回时恢复状态
 * - 中断后输入「继续」自动走 resume 路径
 * - 会话列表自动刷新（任务启动/完成/取消时）
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import MessageBubble from '../components/chat/MessageBubble.vue'
import Composer from '../components/chat/Composer.vue'
import InterruptCard from '../components/chat/InterruptCard.vue'
import ThinkingBlock from '../components/chat/ThinkingBlock.vue'
import type { ChatMessage, StreamEvent } from '../types'
import { fetchThreadMessages, toChatMessages, cancelResearch } from '../api'
import { streamSSE } from '../utils/sse'
import { useSession } from '../stores/session'

const route = useRoute()
const {
  userId,
  currentThreadId,
  threads,
  loadThreads,
  startNewThread,
  selectThread,
  newChatSignal,
  thinkingBlock,
  setThinkingBlock,
  updateThinkingBlock,
  appendThinkingLog,
  clearThinkingBlock,
  getThinkingBlockForCurrentThread,
} = useSession()

const messages = ref<ChatMessage[]>([])
const loading = ref(false)
const errorMessage = ref('')
const hitlEnabled = ref(false)
const activeInterrupt = ref<StreamEvent | null>(null)
const messageList = ref<HTMLElement | null>(null)
const composer = ref<InstanceType<typeof Composer> | null>(null)
const loadingHistory = ref(false)
const currentAbortController = ref<AbortController | null>(null)
const suppressThreadLoad = ref(false)
/** 标记当前会话是否有正在进行的任务（用于区分「继续」和「新问题」） */
const hasActiveTask = ref(false)

const starterPrompts = [
  {
    title: '深度调研',
    prompt:
      '请调研"企业知识库 Agent 平台"市场，按市场规模、主要竞品、收费模式三部分输出，并在每部分附上可追溯来源链接。',
  },
  {
    title: '方案对比',
    prompt:
      '我们要做多 Agent 研究助手，请对比"纯大模型直答""RAG 单 Agent""多 Agent 协作"三种方案，给出优缺点、适用场景与推荐结论。',
  },
  {
    title: '知识问答',
    prompt: '请解释这个项目里"意图分流"的作用，以及简单问题和复杂问题分别会走哪条链路。',
  },
  {
    title: '落地计划',
    prompt: '请把"上线一个可用的 DeepResearch MVP"拆成两周计划，按每天输出任务、验收标准和风险点。',
  },
]

const isEmptyConversation = computed(
  () => messages.value.filter((m) => m.role !== 'status').length === 0,
)

function scrollToBottom() {
  void nextTick(() => {
    const el = messageList.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function resetMessages(welcome: string) {
  messages.value = [{ id: `m-${Date.now()}`, role: 'assistant', content: welcome }]
}

/**
 * 处理用户发送消息。
 *
 * 关键改进：如果用户输入「继续」且当前会话有未完成任务，
 * 自动走 resume 路径而非新建任务。
 */
async function runResearch(text: string) {
  const userText = text.trim()
  if (!userText || loading.value) return

  // 检测「继续」指令：如果有活跃的 interrupt，走 resume
  if (activeInterrupt.value) {
    await resumeTask({ approved: true })
    return
  }

  // 检测「继续」关键词 + 有活跃任务
  if (hasActiveTask.value && /^(继续|继续任务|resume|continue|恢复)$/i.test(userText)) {
    await resumeTask({ approved: true })
    return
  }

  if (!currentThreadId.value) {
    suppressThreadLoad.value = true
    startNewThread()
  }
  const threadId = currentThreadId.value

  loading.value = true
  hasActiveTask.value = true
  errorMessage.value = ''
  activeInterrupt.value = null

  const userMessageId = `u-${Date.now()}`
  messages.value.push({ id: userMessageId, role: 'user', content: userText })

  // 创建思考块（仿 DeepSeek），挂在用户消息下方
  setThinkingBlock(threadId, userMessageId, 'thinking')

  // 自动刷新会话列表
  void loadThreads()
  scrollToBottom()

  try {
    for await (const event of streamSSE<StreamEvent>('/api/v1/research/stream', {
      query: userText,
      user_id: userId.value,
      thread_id: threadId,
      tenant_id: 'default_tenant',
      hitl_enabled: hitlEnabled.value,
    })) {
      // 所有过程事件 -> 思考气泡（绝不混入对话）
      if (event.type === 'status' || event.type === 'phase' || event.type === 'route') {
        const node = (event.type === 'phase' || event.type === 'status') && event.node ? event.node : ''
        appendThinkingLog(event.message || '', node)
        scrollToBottom()
        continue
      }

      if (event.type === 'custom') {
        const node = (event as Record<string, unknown>).node as string || ''
        const message = (event as Record<string, unknown>).message as string || ''
        if (message) {
          appendThinkingLog(message, node)
          scrollToBottom()
        }
        continue
      }

      if (event.type === 'cancelled') {
        updateThinkingBlock('cancelled')
        hasActiveTask.value = false
        void loadThreads()
        break
      }

      if (event.type === 'interrupt') {
        if (event.value) {
          activeInterrupt.value = event
          updateThinkingBlock('done')
        }
        continue
      }

      // 只有 final 事件才写入对话消息
      if (event.type === 'final') {
        clearThinkingBlock()
        messages.value.push({
          id: `a-${Date.now()}`,
          role: 'assistant',
          content: event.final || '已完成，但未返回正文。',
        })
        hasActiveTask.value = false
        void loadThreads()
        continue
      }

      if (event.type === 'error') {
        if ((event as Record<string, unknown>).code === 'TASK_NEEDS_RESUME') {
          activeInterrupt.value = event as StreamEvent
          updateThinkingBlock('done')
          hasActiveTask.value = true
          continue
        }
        updateThinkingBlock('cancelled')
        hasActiveTask.value = false
        throw new Error(event.message || '服务端执行异常')
      }
    }
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : '请求失败'
    updateThinkingBlock('cancelled')
    hasActiveTask.value = false
    messages.value.push({
      id: `e-${Date.now()}`,
      role: 'assistant',
      content: `❌ 请求失败：${errorMessage.value}`,
    })
  } finally {
    loading.value = false
    scrollToBottom()
  }
}

async function resumeTask(resumeValue: Record<string, unknown>) {
  const threadId = activeInterrupt.value?.thread_id || currentThreadId.value
  if (!threadId) return

  activeInterrupt.value = null
  loading.value = true
  hasActiveTask.value = true
  errorMessage.value = ''

  setThinkingBlock(threadId, `resume-${Date.now()}`, 'thinking')
  scrollToBottom()

  try {
    for await (const event of streamSSE<StreamEvent>('/api/v1/research/resume', {
      thread_id: threadId,
      resume_value: resumeValue,
    })) {
      if (event.type === 'status' || event.type === 'phase' || event.type === 'route') {
        const node = (event.type === 'phase' || event.type === 'status') && event.node ? event.node : ''
        appendThinkingLog(event.message || '', node)
        scrollToBottom()
        continue
      }
      if (event.type === 'custom') {
        const node = (event as Record<string, unknown>).node as string || ''
        const message = (event as Record<string, unknown>).message as string || ''
        if (message) {
          appendThinkingLog(message, node)
          scrollToBottom()
        }
        continue
      }
      if (event.type === 'interrupt' && event.value) {
        activeInterrupt.value = event
        updateThinkingBlock('done')
        continue
      }
      if (event.type === 'final') {
        clearThinkingBlock()
        messages.value.push({
          id: `a-${Date.now()}`,
          role: 'assistant',
          content: event.final || '已完成。',
        })
        hasActiveTask.value = false
        void loadThreads()
        continue
      }
      if (event.type === 'error') throw new Error(event.message || '服务端异常')
    }
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : '恢复失败'
    updateThinkingBlock('cancelled')
    hasActiveTask.value = false
    messages.value.push({
      id: `e-${Date.now()}`,
      role: 'assistant',
      content: `恢复失败：${errorMessage.value}`,
    })
  } finally {
    loading.value = false
    scrollToBottom()
  }
}

async function stopResearch() {
  const threadId = currentThreadId.value
  if (threadId) {
    try {
      await cancelResearch(threadId)
    } catch {
      // 即使取消接口失败，也要让用户能停掉前端等待
    }
  }
  loading.value = false
  hasActiveTask.value = false
  appendThinkingLog('用户手动中断了任务', 'system')
  updateThinkingBlock('cancelled')
  void loadThreads()
}

/**
 * 打开历史会话。
 *
 * 关键修复：
 * - 如果当前有活跃任务且目标会话就是当前会话，不重新加载（保留中间状态）
 * - 如果切到别的会话，正确加载该会话的历史消息
 */
async function openThread(threadId: string) {
  if (!threadId) return
  // 如果正在加载同一个会话，跳过
  if (loadingHistory.value && currentThreadId.value === threadId) return

  loading.value = false
  activeInterrupt.value = null
  errorMessage.value = ''
  loadingHistory.value = true
  hasActiveTask.value = false
  try {
    const data = await fetchThreadMessages(threadId)
    const loaded = toChatMessages(threadId, data.messages || [])
    messages.value =
      loaded.length > 0
        ? loaded
        : [{ id: `empty-${threadId}`, role: 'assistant', content: '此会话暂无消息，直接提问即可开始。' }]
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : '加载会话历史失败'
    messages.value = [
      { id: `err-${threadId}`, role: 'assistant', content: `加载会话历史失败：${errorMessage.value}` },
    ]
  } finally {
    loadingHistory.value = false
    scrollToBottom()
  }
}

function handleNewChat() {
  if (loading.value) {
    errorMessage.value = '当前任务还在执行中，可先点「停止」再新建会话。'
    return
  }
  suppressThreadLoad.value = true
  startNewThread()
  resetMessages('已开始新会话。历史会话仍在左侧列表中，随时可以点回去继续查看。')
  errorMessage.value = ''
  activeInterrupt.value = null
  hasActiveTask.value = false
  clearThinkingBlock()
}

function useStarter(prompt: string) {
  composer.value?.fill(prompt)
}

const displayableThinkingBlock = computed(() => {
  const tb = getThinkingBlockForCurrentThread()
  if (!tb) return null
  const userMsgExists = messages.value.some((m) => m.id === tb.userMessageId)
  if (!userMsgExists) return null
  return tb
})

// 会话切换 watcher
watch(
  () => currentThreadId.value,
  (id) => {
    if (!id) return
    if (suppressThreadLoad.value) {
      suppressThreadLoad.value = false
      return
    }
    void openThread(id)
  },
)

watch(
  () => newChatSignal.value,
  () => handleNewChat(),
)

watch(
  () => route.params.threadId,
  (id) => {
    const next = Array.isArray(id) ? id[0] : id
    if (next && next !== currentThreadId.value) selectThread(next)
  },
  { immediate: true },
)

// 定时自动刷新会话列表（参考 lobe-chat 的 SWR 轮询机制）
let refreshTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  // 启动时加载会话列表
  void loadThreads()

  const fromRoute = Array.isArray(route.params.threadId) ? route.params.threadId[0] : route.params.threadId
  if (fromRoute) {
    selectThread(fromRoute)
    return
  }
  if (!currentThreadId.value) {
    resetMessages('你好，我是 DeepResearch。你可以直接提问，我会根据意图自动走快速回答或完整研究链路。')
  } else {
    void openThread(currentThreadId.value)
  }

  // 每 10 秒自动刷新会话列表（任务进行中时更频繁）
  refreshTimer = setInterval(() => {
    void loadThreads()
  }, 10000)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  // 组件卸载时中止 SSE 连接
  if (currentAbortController.value) {
    currentAbortController.value.abort()
  }
})
</script>

<template>
  <main class="chat-view">
    <header class="view-header">
      <div>
        <h2>研究对话</h2>
        <p>多智能体研究工作台 · 快速回答与深度调研自动分流</p>
      </div>
      <label class="hitl-toggle">
        <input v-model="hitlEnabled" type="checkbox" />
        <span>人工干预模式</span>
      </label>
    </header>

    <div ref="messageList" class="message-list">
      <section v-if="isEmptyConversation && !loadingHistory" class="welcome-panel">
        <div class="welcome-hero">
          <h3>DeepResearch</h3>
          <p>多智能体研究助手 · 输入问题即可开始</p>
        </div>
        <div class="starter-grid">
          <button
            v-for="item in starterPrompts"
            :key="item.title"
            class="starter-card"
            @click="useStarter(item.prompt)"
          >
            <span class="starter-title">{{ item.title }}</span>
            <span class="starter-desc">{{ item.prompt.slice(0, 56) }}…</span>
          </button>
        </div>
      </section>

      <p v-if="loadingHistory" class="history-loading">正在加载会话历史…</p>

      <template v-for="message in messages" :key="message.id">
        <MessageBubble :message="message" />

        <!-- 思考气泡：挂在用户消息下方，仿 DeepSeek -->
        <ThinkingBlock
          v-if="displayableThinkingBlock && displayableThinkingBlock.userMessageId === message.id"
          :state="displayableThinkingBlock.state"
          :logs="displayableThinkingBlock.logs"
        />
      </template>
    </div>

    <!-- 中断恢复卡片 -->
    <InterruptCard
      v-if="activeInterrupt"
      :interrupt="activeInterrupt"
      @resume="resumeTask"
    />

    <Composer
      ref="composer"
      :disabled="loading"
      :loading="loading"
      @send="runResearch"
      @stop="stopResearch"
    />

    <p v-if="errorMessage" class="error-bar">{{ errorMessage }}</p>
  </main>
</template>
