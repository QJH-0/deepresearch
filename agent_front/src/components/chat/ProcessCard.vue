<script setup lang="ts">
/**
 * ProcessCard — 研究过程折叠卡片。
 *
 * 把「中间过程」从消息流里独立出来：各节点的进度、工具调用与推理过程
 * 收进一张可折叠卡片，正文区只留最终答案。运行中默认展开，
 * 出结果后自动收起（用户手动展开/收起过则以用户操作为准）。
 */
import { computed, ref, watch } from 'vue'
import AppIcon from '../icons/AppIcon.vue'

export interface ProcessStep {
  /** 节点标识（intent / plan / web_search …） */
  node: string
  /** 展示名 */
  label: string
  /** 该节点的过程要点（按时间顺序） */
  details: string[]
  /** 进行状态 */
  state: 'running' | 'done' | 'error' | 'pending'
}

const props = withDefaults(
  defineProps<{
    steps: ProcessStep[]
    /** 推理文本（模型 reasoning，可能为空） */
    thinking?: string
    running?: boolean
  }>(),
  { thinking: '', running: false },
)

const userToggled = ref(false)
const expanded = ref(false)

// 运行中展开；结束自动收起。用户手动操作过就不再自动干预
watch(
  () => props.running,
  (isRunning) => {
    if (userToggled.value) return
    expanded.value = isRunning
  },
  { immediate: true },
)

function toggle(): void {
  userToggled.value = true
  expanded.value = !expanded.value
}

const doneCount = computed(() => props.steps.filter((s) => s.state === 'done').length)
const hasError = computed(() => props.steps.some((s) => s.state === 'error'))

const summary = computed(() => {
  if (props.running) return `进行中 · 已完成 ${doneCount.value}/${props.steps.length} 步`
  if (hasError.value) return `已完成 · ${doneCount.value}/${props.steps.length} 步（有失败）`
  return `已完成 · 共 ${props.steps.length} 步`
})

/** 同一节点重复上报时只保留最近若干条要点，避免卡片被刷屏 */
const visibleDetails = (step: ProcessStep): string[] => step.details.slice(-4)
</script>

<template>
  <section v-if="steps.length || thinking" class="process-card" :class="{ running }">
    <button class="process-head" type="button" :aria-expanded="expanded" @click="toggle">
      <span class="head-icon" aria-hidden="true">
        <svg viewBox="0 0 16 16" width="15" height="15">
          <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.5" />
          <path d="M8 5v3.4l2.2 1.4" fill="none" stroke="currentColor" stroke-width="1.5"
                stroke-linecap="round" />
        </svg>
      </span>
      <span class="head-title">研究过程</span>
      <span class="head-summary">{{ summary }}</span>
      <span class="head-toggle">
        <AppIcon :name="expanded ? 'cross' : 'list'" :size="13" />
        {{ expanded ? '收起' : '展开' }}
      </span>
    </button>

    <div v-show="expanded" class="process-body">
      <ol class="step-list">
        <li v-for="step in steps" :key="step.node" class="step" :class="step.state">
          <span class="step-dot" aria-hidden="true" />
          <div class="step-main">
            <div class="step-head">
              <span class="step-label">{{ step.label }}</span>
              <span class="step-state">{{ step.state === 'running' ? '进行中'
                : step.state === 'error' ? '失败'
                : step.state === 'pending' ? '待开始' : '完成' }}</span>
            </div>
            <ul v-if="visibleDetails(step).length" class="step-details">
              <li v-for="(detail, idx) in visibleDetails(step)" :key="idx">{{ detail }}</li>
            </ul>
          </div>
        </li>
      </ol>

      <details v-if="thinking" class="thinking-detail">
        <summary>模型推理</summary>
        <pre class="thinking-text">{{ thinking }}</pre>
      </details>
    </div>
  </section>
</template>

<style scoped>
.process-card {
  max-width: var(--content-max);
  margin: 0 auto var(--sp-3);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  background: var(--surface);
  overflow: hidden;
}

.process-card.running {
  border-color: var(--signal);
}

.process-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  padding: var(--sp-3) var(--sp-4);
  border: none;
  background: var(--surface-2);
  text-align: left;
}

.process-head:hover {
  background: var(--surface-sunken);
}

.head-icon {
  display: inline-flex;
  color: var(--signal);
}

.running .head-icon {
  animation: pulse 1.4s var(--ease) infinite;
}

.head-title {
  font-size: var(--fs-sm);
  font-weight: 500;
}

.head-summary {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--ink-3);
}

.head-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  margin-left: auto;
  color: var(--ink-3);
  font-size: var(--fs-xs);
}

.process-body {
  padding: var(--sp-3) var(--sp-4) var(--sp-4);
  border-top: 1px solid var(--line);
}

.step-list {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
}

.step {
  position: relative;
  display: flex;
  gap: var(--sp-3);
  padding-bottom: var(--sp-3);
}

.step::before {
  content: '';
  position: absolute;
  left: 4px;
  top: 12px;
  bottom: 0;
  width: 1px;
  background: var(--line);
}

.step:last-child {
  padding-bottom: 0;
}

.step:last-child::before {
  display: none;
}

.step-dot {
  position: relative;
  z-index: 1;
  width: 9px;
  height: 9px;
  flex: 0 0 9px;
  margin-top: 4px;
  border: 1px solid var(--line-strong);
  border-radius: 50%;
  background: var(--surface);
}

.step.done .step-dot {
  border-color: var(--success);
  background: var(--success);
}

.step.running .step-dot {
  border-color: var(--signal);
  background: var(--signal);
  animation: pulse 1.4s var(--ease) infinite;
}

.step.error .step-dot {
  border-color: var(--danger);
  background: var(--danger);
}

.step-main {
  flex: 1;
  min-width: 0;
}

.step-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
}

.step-label {
  font-size: var(--fs-sm);
  color: var(--ink);
}

.step-state {
  font-size: var(--fs-xs);
  color: var(--ink-3);
}

.step.running .step-state {
  color: var(--signal);
}

.step.error .step-state {
  color: var(--danger);
}

.step-details {
  margin: var(--sp-1) 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.step-details li {
  font-size: var(--fs-xs);
  color: var(--ink-2);
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.thinking-detail {
  margin-top: var(--sp-3);
  border-top: 1px dashed var(--line);
  padding-top: var(--sp-3);
}

.thinking-detail summary {
  cursor: pointer;
  font-size: var(--fs-xs);
  color: var(--ink-3);
  list-style: none;
}

.thinking-detail summary::-webkit-details-marker {
  display: none;
}

.thinking-detail summary::before {
  content: '›';
  display: inline-block;
  margin-right: var(--sp-1);
  transition: transform var(--dur) var(--ease);
}

.thinking-detail[open] summary::before {
  transform: rotate(90deg);
}

.thinking-text {
  margin: var(--sp-2) 0 0;
  padding: var(--sp-3);
  max-height: 240px;
  overflow: auto;
  border-radius: var(--r-sm);
  background: var(--surface-sunken);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--ink-2);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
