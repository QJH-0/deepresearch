<script setup lang="ts">
/**
 * 单条消息气泡。
 *
 * 注意：status 类型的消息已不再使用（改为独立的 ThinkingBlock 组件），
 * 这里只处理 user 和 assistant 两种角色的消息。
 */
import { computed } from 'vue'
import type { ChatMessage } from '../../types'
import { markdownToHtml } from '../../utils/markdown'

const props = defineProps<{ message: ChatMessage }>()

const html = computed(() => markdownToHtml(props.message.content || ''))
const avatarText = computed(() => (props.message.role === 'user' ? '你' : 'AI'))
</script>

<template>
  <div class="message-row" :class="`role-${message.role}`">
    <div class="avatar">{{ avatarText }}</div>
    <div class="bubble-wrap">
      <div class="bubble markdown-body" v-html="html" />
    </div>
  </div>
</template>
