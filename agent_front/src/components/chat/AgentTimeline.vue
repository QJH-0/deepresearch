<script setup lang="ts">
/**
 * AgentTimeline — 消费 agent.status，展示节点进度时间线。
 *
 * P7/H3 增强：
 * - 已完成✓ / 进行中 spinner / 待开始
 * - 每节点完成后可展开查看该步的中间结论（thinkingLogs）与来源增量
 * - 数据全部来自既有事件流，无新增后端改动
 */
import { computed, ref } from 'vue'

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
const expandedNodes = ref<Set<string>>(new Set())

const visibleEntries = computed(() =>
  expanded.value ? props.entries : props.entries.slice(-5)
)
const hiddenCount = computed(() => Math.max(0, props.entries.length - 5))

/** 合并同一节点的连续条目（取最后状态） */
const mergedEntries = computed(() => {
  const result: TimelineEntry[] = []
  for (const entry of visibleEntries.value) {
    const last = result[result.length - 1]
    if (last && last.node === entry.node) {
      result[result.length - 1] = entry
    } else {
      result.push(entry)
    }
  }
  return result
})

/** 按节点分组统计步骤数 */
const nodeStepCount = computed(() => {
  const counts: Record<string, number> = {}
  for (const entry of props.entries) {
    counts[entry.node] = (counts[entry.node] || 0) + 1
  }
  return counts
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

function toggleNode(node: string): void {
  if (expandedNodes.value.has(node)) {
    expandedNodes.value.delete(node)
  } else {
    expandedNodes.value.add(node)
  }
}

function isNodeExpanded(node: string): boolean {
  return expandedNodes.value.has(node)
}

/** 获取某节点的所有日志条目（用于展开时展示中间结论） */
function nodeEntries(node: string): TimelineEntry[] {
  return props.entries.filter(e => e.node === node)
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
        :class="[phaseClass(entry.phase, idx === mergedEntries.length - 1), { expandable: entry.phase === 'done' || entry.phase === 'completed' }]"
      >
        <div class="timeline-row" @click="entry.phase === 'done' || entry.phase === 'completed' ? toggleNode(entry.node) : null">
          <span class="timeline-icon">{{ phaseIcon(entry.phase, idx === mergedEntries.length - 1) }}</span>
          <div class="timeline-content">
            <span class="timeline-label">{{ entry.label }}</span>
            <span v-if="entry.node" class="timeline-node">{{ entry.node }}</span>
            <span class="timeline-time">{{ formatTime(entry.ts) }}</span>
          </div>
          <span
            v-if="(entry.phase === 'done' || entry.phase === 'completed') && (nodeStepCount[entry.node] ?? 0) > 1"
            class="timeline-expand"
          >
            {{ isNodeExpanded(entry.node) ? '▼' : '▶' }}
          </span>
        </div>
        <!-- H3: 展开节点详情 -->
        <div v-if="isNodeExpanded(entry.node)" class="timeline-detail">
          <div v-for="(sub, subIdx) in nodeEntries(entry.node)" :key="subIdx" class="detail-line">
            <span class="detail-time">{{ formatTime(sub.ts) }}</span>
            <span class="detail-phase" :class="phaseClass(sub.phase, false)">{{ sub.phase }}</span>
            <span class="detail-label">{{ sub.label }}</span>
          </div>
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
  padding: 2px 0;
  font-size: 12px;
}
.timeline-row {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 4px 0;
}
.timeline-item.expandable .timeline-row {
  cursor: pointer;
}
.timeline-item.expandable .timeline-row:hover {
  background: rgba(63, 103, 212, 0.04);
  border-radius: 4px;
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
.timeline-expand {
  font-size: 10px;
  color: #a0aecd;
  flex-shrink: 0;
}
.timeline-item.done .timeline-icon { color: #52c41a; }
.timeline-item.running .timeline-icon {
  color: #3f67d4;
  animation: spin 1.5s linear infinite;
}
.timeline-item.error .timeline-icon { color: #f5222d; }
.timeline-item.pending .timeline-icon { color: #c0c0c0; }
.timeline-detail {
  padding: 4px 0 4px 26px;
  border-left: 2px solid #eef2fb;
  margin-left: 9px;
}
.detail-line {
  display: flex;
  gap: 6px;
  font-size: 11px;
  color: #8b9bc0;
  padding: 2px 0;
}
.detail-time {
  color: #a0aecd;
  flex-shrink: 0;
}
.detail-phase {
  font-size: 10px;
  padding: 0 4px;
  border-radius: 3px;
  background: #eef2fb;
}
.detail-phase.done { color: #52c41a; }
.detail-phase.running { color: #3f67d4; }
.detail-phase.error { color: #f5222d; }
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
