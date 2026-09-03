<script setup lang="ts">
/**
 * AgentTimeline — 消费 agent.status，展示节点进度时间线。
 *
 * 已完成 ✓ / 进行中 spinner / 待开始
 */
import { computed, ref, watch } from 'vue'

interface TimelineEntry {
  node: string
  label: string
  phase: string
  ts: number
}

const props = defineProps<{
  entries: TimelineEntry[]
  running?: boolean
}>()

const expanded = ref(false)
const visibleEntries = computed(() =>
  expanded.value ? props.entries : props.entries.slice(-5)
)
const hiddenCount = computed(() => Math.max(0, props.entries.length - 5))

/** 合并同一节点的连续条目 */
const mergedEntries = computed(() => {
  const result: TimelineEntry[] = []
  for (const entry of visibleEntries.value) {
    const last = result[result.length - 1]
    if (last && last.node === entry.node) {
      // 更新最后一条
      result[result.length - 1] = entry
    } else {
      result.push(entry)
    }
  }
  return result
})

function phaseIcon(phase: string, isLast: boolean): string {
  if (phase === 'done' || phase === 'completed') return '✓'
  if (phase === 'error') return '❌'
  if (phase === 'running' || (isLast && props.running)) return '⋯'
  return '○'
}

function phaseClass(phase: string, isLast: boolean): string {
  if (phase === 'done' || phase === 'completed') return 'done'
  if (phase === 'error') return 'error'
  if (phase === 'running' || (isLast && props.running)) return 'running'
  return 'pending'
}

function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString('zh-CN', { hour12: false })
}
</script>

<template>
  <div v-if="mergedEntries.length" class="agent-timeline">
    <div class="timeline-header" @click="expanded = !expanded">
      <span class="timeline-title">执行进度</span>
      <span class="timeline-count">{{ entries.length }} 步</span>
      <span class="timeline-toggle">{{ expanded ? '▼' : '▶' }}</span>
    </div>
    <div class="timeline-list">
      <div
        v-for="(entry, idx) in mergedEntries"
        :key="idx"
        class="timeline-item"
        :class="phaseClass(entry.phase, idx === mergedEntries.length - 1)"
      >
        <span class="timeline-icon">{{ phaseIcon(entry.phase, idx === mergedEntries.length - 1) }}</span>
        <div class="timeline-content">
          <span class="timeline-label">{{ entry.label }}</span>
          <span v-if="entry.node" class="timeline-node">{{ entry.node }}</span>
          <span class="timeline-time">{{ formatTime(entry.ts) }}</span>
        </div>
      </div>
    </div>
    <p v-if="hiddenCount > 0 && !expanded" class="timeline-more">
      还有 {{ hiddenCount }} 步…
    </p>
  </div>
</template>

<style scoped>
.agent-timeline {
  border: 1px solid #eef2fb;
  border-radius: 8px;
  background: #fafbfd;
  margin-top: 8px;
  overflow: hidden;
}
.timeline-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  cursor: pointer;
  user-select: none;
  font-size: 13px;
  color: #5c6f98;
}
.timeline-title { font-weight: 500; }
.timeline-count {
  font-size: 11px;
  background: #eef2fd;
  color: #8b9bc0;
  border-radius: 999px;
  padding: 1px 6px;
}
.timeline-toggle {
  margin-left: auto;
  font-size: 10px;
  color: #a0aecd;
}
.timeline-list {
  padding: 4px 12px 8px;
}
.timeline-item {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 4px 0;
  font-size: 12px;
}
.timeline-icon {
  width: 18px;
  text-align: center;
  flex-shrink: 0;
}
.timeline-content {
  display: flex;
  gap: 6px;
  align-items: baseline;
  flex: 1;
}
.timeline-label { color: #5f719b; }
.timeline-node {
  color: #3f67d4;
  font-size: 11px;
  background: #eef2fd;
  border-radius: 4px;
  padding: 0 4px;
}
.timeline-time {
  font-size: 11px;
  color: #a0aecd;
  margin-left: auto;
}
.timeline-item.done .timeline-icon { color: #52c41a; }
.timeline-item.running .timeline-icon {
  color: #3f67d4;
  animation: spin 1.5s linear infinite;
}
.timeline-item.error .timeline-icon { color: #f5222d; }
.timeline-item.pending .timeline-icon { color: #c0c0c0; }
.timeline-more {
  text-align: center;
  font-size: 11px;
  color: #a0aecd;
  padding: 4px;
}
@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
