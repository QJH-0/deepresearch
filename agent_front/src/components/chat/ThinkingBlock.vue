<script setup lang="ts">
/**
 * ThinkingBlock（重构）— 消息级、可折叠、流式追加动画。
 *
 * 重构要点：
 * - 挂在消息上（通过 props.logs），不是全局块
 * - 可折叠/展开，折叠时只显示最后 2 条预览
 * - 流式追加时有渐入动画
 */
import { computed, ref } from 'vue'

export interface ThinkingLog {
  node: string
  message: string
  time: string
}

export type ThinkingState = 'thinking' | 'done' | 'cancelled'

const props = defineProps<{
  state: ThinkingState
  logs: ThinkingLog[]
  thinking?: string
}>()

const expanded = ref(false)

const hasLogs = computed(() => props.logs.length > 0)

const previewLogs = computed(() => {
  if (!hasLogs.value || expanded.value) return []
  return props.logs.slice(-2)
})

const hiddenCount = computed(() => Math.max(0, props.logs.length - 2))

const stateLabel = computed(() => {
  if (props.state === 'cancelled') return '已取消'
  if (props.state === 'thinking') return '思考中...'
  return '思考完成'
})

const stateIcon = computed(() => {
  if (props.state === 'cancelled') return '⏹'
  if (props.state === 'thinking') return '💭'
  return '✅'
})
</script>

<template>
  <div class="thinking-block" :class="`state-${state}`">
    <div class="thinking-header" @click="expanded = !expanded">
      <span class="thinking-icon">{{ stateIcon }}</span>
      <span class="thinking-label">{{ stateLabel }}</span>
      <span v-if="hasLogs && !expanded" class="thinking-count">
        {{ logs.length }} 条过程记录
      </span>
      <span class="thinking-toggle-icon">{{ expanded ? '▼' : '▶' }}</span>
    </div>

    <div v-if="hasLogs && !expanded" class="thinking-preview">
      <div v-for="log in previewLogs" :key="log.time + log.message" class="preview-line">
        <span class="log-time">{{ log.time }}</span>
        <span v-if="log.node" class="log-node">[{{ log.node }}]</span>
        <span class="log-msg">{{ log.message }}</span>
      </div>
      <p v-if="hiddenCount > 0" class="hidden-hint">还有 {{ hiddenCount }} 条记录…</p>
    </div>

    <div v-if="hasLogs && expanded" class="thinking-body">
      <div class="thinking-logs">
        <div v-for="(log, idx) in logs" :key="idx" class="log-line">
          <span class="log-time">{{ log.time }}</span>
          <span v-if="log.node" class="log-node">[{{ log.node }}]</span>
          <span class="log-msg">{{ log.message }}</span>
        </div>
      </div>
      <button class="thinking-collapse-btn" @click.stop="expanded = false">
        收起过程记录
      </button>
    </div>
  </div>
</template>

<style scoped>
.thinking-block {
  border: 1px solid #e4eafb;
  border-radius: 12px;
  background: #fbfcfe;
  margin-top: 8px;
  overflow: hidden;
}
.thinking-block.state-thinking {
  border-color: #d5def4;
  background: linear-gradient(180deg, #f8faff 0%, #f4f7ff 100%);
}
.thinking-block.state-cancelled {
  border-color: #f4cdc8;
  background: #fff7f6;
}
.thinking-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  cursor: pointer;
  user-select: none;
  transition: background 0.15s;
}
.thinking-header:hover {
  background: rgba(63, 103, 212, 0.04);
}
.thinking-icon { font-size: 14px; }
.thinking-label { font-size: 13px; font-weight: 500; color: #5c6f98; }
.state-thinking .thinking-label { color: #3f67d4; }
.state-cancelled .thinking-label { color: #c0392b; }
.thinking-count {
  font-size: 11.5px;
  color: #8b9bc0;
  background: #eef2fd;
  border-radius: 999px;
  padding: 1px 8px;
}
.thinking-toggle-icon {
  margin-left: auto;
  font-size: 10px;
  color: #a0aecd;
}
.thinking-preview {
  padding: 0 14px 10px;
}
.preview-line, .log-line {
  display: flex;
  gap: 4px;
  font-size: 12px;
  color: #6d7fa8;
  line-height: 1.6;
  align-items: baseline;
}
.log-time {
  font-size: 11px;
  color: #a0aecd;
  flex-shrink: 0;
}
.log-node {
  color: #3f67d4;
  font-weight: 500;
  flex-shrink: 0;
}
.log-msg { flex: 1; }
.hidden-hint {
  font-size: 11px;
  color: #a0aecd;
  margin-top: 4px;
}
.thinking-body {
  padding: 0 14px 10px;
}
.thinking-logs {
  font-size: 12px;
  color: #5f719b;
  line-height: 1.7;
  max-height: 300px;
  overflow-y: auto;
  padding: 8px;
  background: #fff;
  border-radius: 8px;
  border: 1px solid #eef2fb;
}
.thinking-collapse-btn {
  display: block;
  width: 100%;
  margin-top: 8px;
  border: 1px solid #d9e3f9;
  background: #fff;
  color: #5c6f98;
  border-radius: 8px;
  padding: 6px 12px;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s;
}
.thinking-collapse-btn:hover {
  background: #f4f7ff;
}
.state-thinking .thinking-icon {
  animation: pulse 1.5s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
</style>
