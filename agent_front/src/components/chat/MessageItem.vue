<script setup lang="ts">
/**
 * MessageItem — 消息气泡（重构版）。
 *
 * thinking 和 sources 挂在消息上（消息级），不是全局块。
 * 使用 MarkdownRender 替代旧版手写 markdownToHtml。
 */
import { computed } from 'vue'
import MarkdownRender from './MarkdownRender.vue'
import ThinkingBlock from './ThinkingBlock.vue'
import SourceList from './SourceList.vue'
import type { ChatMessage } from '../../stores/chat'

const props = defineProps<{ message: ChatMessage }>()

const avatarText = computed(() => (props.message.role === 'user' ? '你' : 'AI'))

const thinkingState = computed<'thinking' | 'done' | 'cancelled'>(() => {
  if (props.message.status === 'cancelled') return 'cancelled'
  if (props.message.status === 'streaming') return 'thinking'
  return 'done'
})

const hasThinking = computed(() => {
  return (props.message.thinkingLogs && props.message.thinkingLogs.length > 0) ||
    (props.message.thinking && props.message.thinking.length > 0)
})
</script>

<template>
  <div class="message-row" :class="`role-${message.role}`">
    <div class="avatar">{{ avatarText }}</div>
    <div class="bubble-wrap">
      <ThinkingBlock
        v-if="hasThinking"
        :state="thinkingState"
        :logs="message.thinkingLogs || []"
        :thinking="message.thinking"
      />
      <div v-if="message.content" class="bubble">
        <MarkdownRender :content="message.content" />
      </div>
      <SourceList v-if="message.sources" :sources="message.sources" />
    </div>
  </div>
</template>

<style scoped>
.message-row {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}
.message-row.role-user {
  flex-direction: row-reverse;
}
.avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  flex-shrink: 0;
  background: #eef2fd;
  color: #3f67d4;
}
.message-row.role-user .avatar {
  background: #e6f7ff;
  color: #1890ff;
}
.bubble-wrap {
  max-width: 75%;
  min-width: 0;
}
.message-row.role-user .bubble-wrap {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}
.bubble {
  padding: 10px 14px;
  border-radius: 12px;
  background: #f8faff;
  border: 1px solid #eef2fb;
  font-size: 14px;
  line-height: 1.7;
  word-break: break-word;
}
.message-row.role-user .bubble {
  background: #e6f7ff;
  border-color: #bae7ff;
}
</style>
