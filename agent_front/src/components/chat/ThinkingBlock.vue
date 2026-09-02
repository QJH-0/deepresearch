<script setup lang="ts">
/**
 * 思考块（Thinking Block）
 *
 * 类似 DeepSeek / 豆包的思考模式：
 * - 执行过程中显示一个独立的思考区域，默认折叠
 * - 点击可展开查看完整过程日志
 * - 思考块与用户消息关联，只在当前轮次有效
 * - 任务结束后切走再回来不显示思考块，只显示最终结果
 */
import { computed, ref } from 'vue'
import { markdownToHtml } from '../../utils/markdown'

export interface ThinkingLog {
  node: string
  message: string
  time: string
}

export type ThinkingState = 'thinking' | 'done' | 'cancelled'

const props = defineProps<{
  state: ThinkingState
  logs: ThinkingLog[]
}>()

const expanded = ref(false)

const hasLogs = computed(() => props.logs.length > 0)

const previewLogs = computed(() => {
  if (!hasLogs.value || expanded.value) return []
  // 折叠态只显示最后 2 条
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

const logsHtml = computed(() => {
  const content = props.logs
    .map((log) => `- ${log.time} ${log.node ? `[${log.node}] ` : ''}${log.message}`)
    .join('\n')
  return markdownToHtml(content)
})

const previewHtml = computed(() => {
  const content = previewLogs.value
    .map((log) => `- ${log.time} ${log.node ? `[${log.node}] ` : ''}${log.message}`)
    .join('\n')
  return markdownToHtml(content)
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

    <div v-if="hasLogs && !expanded" class="thinking-preview markdown-body" v-html="previewHtml" />

    <div v-if="hasLogs && expanded" class="thinking-body">
      <div class="thinking-logs markdown-body" v-html="logsHtml" />
      <button class="thinking-collapse-btn" @click="expanded = false">
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

.thinking-icon {
  font-size: 14px;
}

.thinking-label {
  font-size: 13px;
  font-weight: 500;
  color: #5c6f98;
}

.state-thinking .thinking-label {
  color: #3f67d4;
}

.state-cancelled .thinking-label {
  color: #c0392b;
}

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
  font-size: 12px;
  color: #6d7fa8;
  line-height: 1.6;
  position: relative;
  max-height: 60px;
  overflow: hidden;
}

.thinking-preview::after {
  content: '';
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 20px;
  background: linear-gradient(180deg, transparent 0%, #f8faff 100%);
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

/* 思考中的动画 */
.state-thinking .thinking-icon {
  animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.5;
  }
}
</style>
