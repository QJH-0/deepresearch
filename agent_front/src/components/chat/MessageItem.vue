<script setup lang="ts">
/**
 * MessageItem — 消息气泡。
 *
 * 只负责「用户提问」与「最终答案」两种气泡；中间过程由 ProcessCard 承载，
 * 不再内嵌思考块。样式全部由设计系统（assets/theme.css）提供，
 * 组件内不再写硬编码颜色 —— 否则会覆盖设计令牌，造成新旧样式混杂。
 */
import { computed } from 'vue'
import MarkdownRender from './MarkdownRender.vue'
import SourceList from './SourceList.vue'
import type { ChatMessage } from '../../stores/chat'

const props = defineProps<{ message: ChatMessage }>()

const avatarText = computed(() => (props.message.role === 'user' ? '你' : 'AI'))

const hasSources = computed(() => (props.message.sources?.length || 0) > 0)

const isReport = computed(
  () =>
    props.message.role === 'assistant' &&
    props.message.status === 'done' &&
    (props.message.content || '').length > 200,
)

function exportMarkdown(): void {
  if (!props.message.content) return
  const sources = props.message.sources || []
  const refs =
    sources.length > 0
      ? sources
          .map((s, i) => {
            const locator = s.source_type === 'kb' ? `知识库 ${s.chunk_id || ''}` : s.url || ''
            return `[${i + 1}] ${s.title || '未知来源'} — ${locator}`
          })
          .join('\n')
      : ''
  const content = props.message.content + (refs ? `\n\n## 参考文献\n${refs}` : '')
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = '研究报告.md'
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="message-row" :class="message.role">
    <div class="avatar">{{ avatarText }}</div>
    <div class="bubble-wrap">
      <div class="bubble">
        <MarkdownRender
          :content="message.content"
          :sources="message.sources"
          :show-export="isReport"
          :streaming="message.status === 'streaming'"
          @export-markdown="exportMarkdown"
        />
      </div>
      <SourceList v-if="hasSources" :sources="message.sources || []" />
    </div>
  </div>
</template>
