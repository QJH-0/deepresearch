<script setup lang="ts">
/** ChatView（重构版）— 只做布局组装。事件→useEventStream，状态→Pinia stores */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import MessageItem from '../components/chat/MessageItem.vue'
import Composer from '../components/chat/Composer.vue'
import AgentTimeline from '../components/chat/AgentTimeline.vue'
import PlanApprovalCard from '../components/chat/PlanApprovalCard.vue'
import ClarifyCard from '../components/chat/ClarifyCard.vue'
import ReportReviewCard from '../components/chat/ReportReviewCard.vue'
import RollbackMenu from '../components/chat/RollbackMenu.vue'
import { useChatStore } from '../stores/chat'
import { useThreadsStore } from '../stores/threads'
import { useInterruptStore } from '../stores/interrupt'
import { useEventStream } from '../composables/useEventStream'
import { fetchThreadMessages, toChatMessages, cancelResearch } from '../api/rest'
import type { InterruptKind } from '../types/events.gen'

const route = useRoute()
const chat = useChatStore()
const threads = useThreadsStore()
const intr = useInterruptStore()
const { run, resume } = useEventStream()

const messageList = ref<HTMLElement | null>(null)
const composer = ref<InstanceType<typeof Composer> | null>(null)
const loading = computed(() => chat.isRunning(threads.currentThreadId))
const hitlEnabled = ref(false)

const messages = computed(() => chat.getMessages(threads.currentThreadId))
const currentInterrupt = computed(() => intr.get(threads.currentThreadId))
const agentTimeline = computed(() => chat.getAgentTimeline(threads.currentThreadId))
const isEmpty = computed(() => messages.value.length === 0 || (messages.value.length === 1 && messages.value[0]?.role === 'assistant' && !messages.value[0]?.content))

const starterPrompts = [
  { title: '深度调研', prompt: '请调研"企业知识库 Agent 平台"市场，按市场规模、主要竞品、收费模式三部分输出。' },
  { title: '方案对比', prompt: '请对比"纯大模型直答""RAG 单 Agent""多 Agent 协作"三种方案，给出优缺点与推荐结论。' },
  { title: '知识问答', prompt: '请解释这个项目里"意图分流"的作用。' },
  { title: '落地计划', prompt: '请把"上线一个可用的 DeepResearch MVP"拆成两周计划。' },
]

function scrollToBottom() { void nextTick(() => { if (messageList.value) messageList.value.scrollTop = messageList.value.scrollHeight }) }

async function onSend(text: string) {
  const threadId = threads.currentThreadId
  if (!threadId) { threads.startNewThread() }
  const id = threads.currentThreadId
  await run(id, text, { hitl_enabled: hitlEnabled.value })
  scrollToBottom()
}

function onResume(payload: Record<string, unknown>) {
  void resume(threads.currentThreadId, payload)
}

async function onStop() {
  const threadId = threads.currentThreadId
  if (threadId) { try { await cancelResearch(threadId) } catch { /* 乐观更新 */ } }
  chat.markCancelled(threadId)
}

async function openThread(threadId: string) {
  try {
    const data = await fetchThreadMessages(threadId)
    const loaded = toChatMessages(threadId, data.messages || [])
    chat.setMessages(threadId, loaded as never)
  } catch { /* 静默失败 */ }
  scrollToBottom()
}

function handleNewChat() {
  if (loading.value) return
  threads.startNewThread()
  threads.requestNewChat()
}

function useStarter(prompt: string) { composer.value?.fill(prompt) }

// ── watchers ────────────────────────────────────────
watch(() => threads.currentThreadId, (id) => { if (id) void openThread(id) })
watch(() => threads.newChatSignal, () => handleNewChat())
watch(() => route.params.threadId, (id) => {
  const next = Array.isArray(id) ? id[0] : id
  if (next && next !== threads.currentThreadId) threads.selectThread(next)
}, { immediate: true })

// ── lifecycle ────────────────────────────────────────
onMounted(() => {
  void threads.load()
  const fromRoute = Array.isArray(route.params.threadId) ? route.params.threadId[0] : route.params.threadId
  if (fromRoute) { threads.selectThread(fromRoute); return }
  if (!threads.currentThreadId) {
    chat.ensureThread('welcome')
    chat.setMessages('welcome', [{ id: 'welcome', role: 'assistant', content: '你好，我是 DeepResearch。直接提问即可开始。' } as never])
    threads.selectThread('welcome')
  } else { void openThread(threads.currentThreadId) }
})

onUnmounted(() => { /* SSE 由 useEventStream 内部管理 */ })
</script>

<template>
  <main class="chat-view">
    <header class="view-header">
      <div>
        <h2>研究对话</h2>
        <p>多智能体研究工作台 · 快速回答与深度调研自动分流</p>
      </div>
      <div class="header-right">
        <RollbackMenu v-if="threads.currentThreadId" :thread-id="threads.currentThreadId" />
        <label class="hitl-toggle">
          <input v-model="hitlEnabled" type="checkbox" />
          <span>人工干预模式</span>
        </label>
      </div>
    </header>

    <div ref="messageList" class="message-list">
      <section v-if="isEmpty && !loading" class="welcome-panel">
        <div class="welcome-hero">
          <h3>DeepResearch</h3>
          <p>多智能体研究助手 · 输入问题即可开始</p>
        </div>
        <div class="starter-grid">
          <button v-for="item in starterPrompts" :key="item.title" class="starter-card" @click="useStarter(item.prompt)">
            <span class="starter-title">{{ item.title }}</span>
            <span class="starter-desc">{{ item.prompt.slice(0, 56) }}…</span>
          </button>
        </div>
      </section>

      <template v-for="message in messages" :key="message.id">
        <MessageItem :message="message" />
      </template>

      <AgentTimeline
        v-if="agentTimeline.length"
        :entries="agentTimeline"
        :running="loading"
      />
    </div>

    <!-- HITL 卡片（按 kind 分支渲染） -->
    <PlanApprovalCard
      v-if="currentInterrupt?.kind === 'plan_approval'"
      :payload="currentInterrupt.payload"
      @resume="onResume"
    />
    <ClarifyCard
      v-else-if="currentInterrupt?.kind === 'clarification'"
      :payload="currentInterrupt.payload"
      @resume="onResume"
    />
    <ReportReviewCard
      v-else-if="currentInterrupt?.kind === 'report_review'"
      :payload="currentInterrupt.payload"
      @resume="onResume"
    />

    <Composer
      ref="composer"
      :disabled="loading"
      :loading="loading"
      @send="onSend"
      @stop="onStop"
    />
  </main>
</template>
