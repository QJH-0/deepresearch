<script setup lang="ts">
/** ChatView（重构版）— 只做布局组装。事件→useEventStream，状态→Pinia stores */
import AppIcon from '../components/icons/AppIcon.vue'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import MessageItem from '../components/chat/MessageItem.vue'
import Composer from '../components/chat/Composer.vue'
import ProcessCard from '../components/chat/ProcessCard.vue'
import PlanApprovalCard from '../components/chat/PlanApprovalCard.vue'
import ClarifyCard from '../components/chat/ClarifyCard.vue'
import EvidenceGapCard from '../components/chat/EvidenceGapCard.vue'
import ReportReviewCard from '../components/chat/ReportReviewCard.vue'
import RollbackMenu from '../components/chat/RollbackMenu.vue'
import { useChatStore } from '../stores/chat'
import { groupTurns } from '../utils/turns'
import { useThreadsStore } from '../stores/threads'
import { useInterruptStore } from '../stores/interrupt'
import { useEventStream } from '../composables/useEventStream'
import { fetchThreadMessages, toChatMessages, cancelResearch, exportPdf, exportMarkdown, ApiError } from '../api/rest'
import type { ReportIssue } from '../api/rest'
import type { InterruptKind } from '../types/events.gen'
import { NAlert } from 'naive-ui'

const route = useRoute()
const chat = useChatStore()
const threads = useThreadsStore()
const intr = useInterruptStore()
const { resume, runOrResume, resumeWithCondition, startNewTopic } = useEventStream()

const messageList = ref<HTMLElement | null>(null)
const composer = ref<InstanceType<typeof Composer> | null>(null)
const loading = computed(() => chat.isRunning(threads.currentThreadId))
const isReconnecting = computed(() => chat.isReconnecting(threads.currentThreadId))
/** 停止态：用户手动停止过该会话。此时输入框给出两个显式动作入口 */
const isStopped = computed(() => chat.isUserStopped(threads.currentThreadId))
const hitlEnabled = ref(false)

const messages = computed(() => chat.getMessages(threads.currentThreadId))
const currentInterrupt = computed(() => intr.get(threads.currentThreadId))
const agentTimeline = computed(() => chat.getAgentTimeline(threads.currentThreadId))
const isEmpty = computed(() => messages.value.length === 0 || (messages.value.length === 1 && messages.value[0]?.role === 'assistant' && !messages.value[0]?.content))

/** 节点展示名（后端 NODE_LABELS 的镜像；时间线里的 label 优先） */
const NODE_LABELS: Record<string, string> = {
  intent: '意图识别',
  clarify: '澄清',
  plan: '规划',
  web_search: '网页检索',
  local_rag: '知识库检索',
  deep_dive: '深挖',
  analyze: '分析',
  reflect: '反思',
  write: '成文',
  direct_answer: '直接回答',
}

/**
 * 消息按「轮」分组：用户提问 → 中间过程 → 最终答案。
 * 分组规则与可测性说明见 utils/turns.ts。
 */
const turns = computed(() =>
  groupTurns(
    messages.value,
    {
      ...NODE_LABELS,
      ...Object.fromEntries(
        agentTimeline.value.filter((e) => e.node && e.label).map((e) => [e.node, e.label]),
      ),
    },
    agentTimeline.value,
  ),
)


const starterPrompts = [
  { title: '深度调研', prompt: '请调研当前 AI Agent 领域的前沿进展，包括主流框架、典型应用场景和落地挑战。' },
  { title: '方案对比', prompt: '请对比传统搜索引擎、RAG 检索增强生成和 Agent 自主调研三种信息获取方案，分析各自的适用场景与局限性。' },
  { title: '知识问答', prompt: '请解释大语言模型中的"幻觉"问题产生原因，以及目前主流的缓解方法。' },
  { title: '落地计划', prompt: '请帮我制定一个两周学习计划，目标是系统掌握 LangChain 或 LangGraph 的核心概念与基础用法。' },
]

function scrollToBottom() { void nextTick(() => { if (messageList.value) messageList.value.scrollTop = messageList.value.scrollHeight }) }

async function onSend(text: string) {
  const threadId = threads.currentThreadId
  if (!threadId) { threads.startNewThread() }
  const id = threads.currentThreadId

  // 如果当前会话有未处理的 interrupt，走 resume 而非新 run
  if (intr.has(id)) {
    void resume(id, { kind: 'clarification', answers: [text] })
    return
  }

  // 先检查后端是否存在可恢复的 checkpoint（用户停止后续流），
  // 不可恢复则走新 run
  await runOrResume(id, text, { hitl_enabled: hitlEnabled.value })
  scrollToBottom()
}

function onResume(payload: Record<string, unknown>) {
  void resume(threads.currentThreadId, payload)
}

/**
 * 停止态显式入口：「补充条件继续研究」。
 * 与直接发送的区别是不再靠关键词猜意图 —— 点这个按钮就是要在原研究上补条件。
 */
async function onContinueWithCondition(text: string) {
  await resumeWithCondition(threads.currentThreadId, text, { hitl_enabled: hitlEnabled.value })
  scrollToBottom()
}

/**
 * 停止态显式入口：「换个主题重新研究」。
 * 开新 thread，旧会话与其 checkpoint 保持不动，可随时切回续研。
 */
async function onRestartTopic(text: string) {
  await startNewTopic(text, { hitl_enabled: hitlEnabled.value })
  scrollToBottom()
}

async function onStop() {
  const threadId = threads.currentThreadId
  if (threadId) { try { await cancelResearch(threadId) } catch { /* 乐观更新 */ } }
  chat.markCancelled(threadId)
  chat.setUserStopped(threadId, true)
}

async function openThread(threadId: string) {
  try {
    const data = await fetchThreadMessages(threadId)
    const loaded = toChatMessages(threadId, data.messages || [])
    chat.setMessages(threadId, loaded as never)
  } catch { /* 临时会话 ID 无后端记录，静默处理 */ }
  scrollToBottom()
}

function handleNewChat() {
  if (loading.value) return
  // P0-2：不再主动调用 requestNewChat() — startNewThread() 已通过 currentThreadId 变更触发下方 watcher 加载空消息；
  // 手动 requestNewChat() 会让 watcher(newChatSignal) 再次触发自身 → 递归爆栈。
  threads.startNewThread()
}

function useStarter(prompt: string) { composer.value?.fill(prompt) }

// P7-4: 导出报告为 PDF（或降级 Markdown）
// 导出接口要求认证，不能用 <a href>/window.open 直链，必须走 fetch 再落盘
//
// 后端导出前跑质量门禁：error 阻断（422 + 问题清单），仅 warning 时放行并经
// X-Report-Warnings 头回传。两者都要展示给用户 —— 只报一句「导出失败」等于
// 让用户无从知道报告哪里有问题。
const exportNotice = ref<{ level: 'error' | 'warning'; title: string; lines: string[] } | null>(null)

function describeIssue(issue: ReportIssue): string {
  return `${issue.severity === 'error' ? '阻断' : '提醒'}：${issue.message}`
}

function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

async function exportPdfReport(): Promise<void> {
  const threadId = threads.currentThreadId
  if (!threadId) return
  exportNotice.value = null
  try {
    const { blob, warnings } = await exportPdf(threadId)
    saveBlob(blob, `report_${threadId.slice(0, 12)}.pdf`)
    if (warnings.length) {
      exportNotice.value = {
        level: 'warning',
        title: '已导出，但报告有以下问题建议核对',
        lines: warnings.map(describeIssue),
      }
    }
  } catch (err) {
    const issues = err instanceof ApiError ? err.issues : []
    if (issues.length) {
      exportNotice.value = {
        level: 'error',
        title: err instanceof Error ? err.message : '报告未通过导出前检查',
        lines: issues.map(describeIssue),
      }
      return
    }
    chat.markError(threadId, {
      code: 'EXPORT_FAILED',
      message: err instanceof Error ? err.message : '导出失败',
    })
  }
}

/**
 * 逃生通道：PDF 被质量门禁拦下时导出 Markdown 原文。
 *
 * 门禁拦的是「排版后的成品」，不该把用户锁死 —— Markdown 保留完整内容，
 * 只是没有排版保证。没有这条退路，被拦的用户只能先改报告再导出。
 */
async function exportMarkdownReport(): Promise<void> {
  const threadId = threads.currentThreadId
  if (!threadId) return
  try {
    const { blob } = await exportMarkdown(threadId)
    saveBlob(blob, `report_${threadId.slice(0, 12)}.md`)
  } catch (err) {
    chat.markError(threadId, {
      code: 'EXPORT_FAILED',
      message: err instanceof Error ? err.message : '导出失败',
    })
  }
}

// ── watchers ────────────────────────────────────────
watch(() => threads.currentThreadId, (id) => { if (id) void openThread(id) })
watch(() => threads.newChatSignal, () => handleNewChat())
watch(() => route.params.threadId, (id) => {
  const next = Array.isArray(id) ? id[0] : id
  if (next && next !== threads.currentThreadId) {
    threads.selectThread(next)
    // 切换会话时检查是否有未处理的 interrupt
    void intr.rebuild(next).then((hasInterrupt) => {
      if (!hasInterrupt) void openThread(next)
    })
  }
}, { immediate: true })

// ── lifecycle ────────────────────────────────────────
onMounted(() => {
  const fromRoute = Array.isArray(route.params.threadId) ? route.params.threadId[0] : route.params.threadId
  if (fromRoute) {
    threads.selectThread(fromRoute)
    // 切换到旧会话时检查是否有未处理的 interrupt
    void intr.rebuild(fromRoute).then((hasInterrupt) => {
      if (!hasInterrupt) void openThread(fromRoute)
    })
    return
  }
  // 路由无 threadId → 创建新会话，不加载任何旧会话
  threads.startNewThread()
  chat.ensureThread(threads.currentThreadId)
  chat.setMessages(threads.currentThreadId, [])
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
        <button v-if="threads.currentThreadId && !isEmpty" class="export-pdf-btn" @click="exportPdfReport"><AppIcon name="download" :size="14" /> 导出</button>
        <label class="hitl-toggle">
          <input v-model="hitlEnabled" type="checkbox" />
          <span>人工干预模式</span>
        </label>
      </div>
    </header>

    <NAlert
      v-if="isReconnecting"
      type="warning"
      :bordered="false"
      class="reconnecting-banner"
    >
      网络连接中断，正在自动恢复…
    </NAlert>

    <NAlert
      v-if="exportNotice"
      :type="exportNotice.level"
      :bordered="false"
      closable
      class="reconnecting-banner"
      @close="exportNotice = null"
    >
      <div>{{ exportNotice.title }}</div>
      <ul class="export-notice-list">
        <li v-for="(line, index) in exportNotice.lines" :key="index">{{ line }}</li>
      </ul>
      <div v-if="exportNotice.level === 'error'" class="export-notice-actions">
        <button class="export-pdf-btn" @click="exportMarkdownReport">导出 Markdown 原文</button>
        <span class="export-notice-hint">内容完整，但无排版保证</span>
      </div>
    </NAlert>

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

      <template v-for="turn in turns" :key="turn.key">
        <MessageItem v-if="turn.user" :message="turn.user" />
        <ProcessCard
          v-if="turn.steps.length || turn.thinking"
          :steps="turn.steps"
          :thinking="turn.thinking"
          :running="loading && !turn.answer"
        />
        <MessageItem v-if="turn.answer" :message="turn.answer" />
      </template>
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
    <EvidenceGapCard
      v-else-if="currentInterrupt?.kind === 'evidence_gap'"
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
      :stopped="isStopped"
      @send="onSend"
      @stop="onStop"
      @continue-with-condition="onContinueWithCondition"
      @restart-topic="onRestartTopic"
    />
  </main>
</template>

<style scoped>
.reconnecting-banner {
  margin: 0 0 8px;
  border-radius: 6px;
}
.export-notice-list {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.6;
}
.export-notice-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}
.export-notice-hint {
  color: #6b7a99;
  font-size: 12px;
}
.export-pdf-btn {
  padding: 4px 12px;
  border: 1px solid #d9e3f9;
  border-radius: 6px;
  background: #f8faff;
  color: #3f67d4;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s;
}
.export-pdf-btn:hover {
  background: #eef2fd;
}
</style>
