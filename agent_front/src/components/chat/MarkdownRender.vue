<script setup lang="ts">
/**
 * MarkdownRender — 使用 markdown-it + highlight.js + katex 渲染。
 *
 * P7 增强：
 * - [source_id] 角标渲染为上标可交互元素（hover tooltip）
 * - 代码块附带复制按钮
 * - 导出 Markdown 按钮
 */
import { computed, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import katex from '@vscode/markdown-it-katex'
import 'highlight.js/styles/github.css'
import 'katex/dist/katex.min.css'
import type { SourceItem } from '../../types/events.gen'

const props = defineProps<{
  content: string
  sources?: SourceItem[]
  showExport?: boolean
}>()

const emit = defineEmits<{
  (e: 'export-markdown'): void
  (e: 'source-click', sourceId: string): void
}>()

const md = new MarkdownIt({
  html: false,
  linkify: true,
  typographer: true,
  highlight(str: string, lang: string): string {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return `<pre class="hljs"><code>${hljs.highlight(str, { language: lang }).value}</code></pre>`
      } catch {
        // fallthrough
      }
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(str)}</code></pre>`
  },
})
md.use(katex)

// P7-3: 在渲染前把 [source_id] 替换为占位符，避免被 markdown-it 当作链接处理
const CITATION_PATTERN = /\[([A-Z]+\d+_\d+-\d+)\]/g
const PLACEHOLDER_PREFIX = '@@CITE_'

function preProcessContent(content: string): string {
  return content.replace(CITATION_PATTERN, (match, id) => {
    return `${PLACEHOLDER_PREFIX}${id}@@`
  })
}

function postProcessHtml(html: string): string {
  // 把占位符替换回可交互的上标元素
  const placeholderPattern = new RegExp(`${PLACEHOLDER_PREFIX}([A-Z]+\\d+_\\d+-\\d+)@@`, 'g')
  return html.replace(placeholderPattern, (_, id) => {
    return `<sup class="citation-ref" data-source-id="${id}">[${id}]</sup>`
  })
}

const html = computed(() => {
  const processed = preProcessContent(props.content || '')
  const rendered = md.render(processed)
  return postProcessHtml(rendered)
})

// ── 代码块复制 ──────────────────────────────────────
const copiedId = ref('')
function copyCode(e: MouseEvent): void {
  const target = e.target as HTMLElement
  const pre = target.closest('pre')
  if (!pre) return
  const code = pre.querySelector('code')
  if (!code) return
  navigator.clipboard.writeText(code.textContent || '').then(() => {
    copiedId.value = Math.random().toString(36).slice(2)
    setTimeout(() => { copiedId.value = '' }, 2000)
  })
}

// ── P7-3: 角标交互 ──────────────────────────────────
const tooltipContent = ref('')
const tooltipX = ref(0)
const tooltipY = ref(0)
const tooltipVisible = ref(false)

function findSource(sourceId: string): SourceItem | undefined {
  return props.sources?.find(s => {
    // 尝试匹配 url 或 title 中的 source_id
    return (s.url || '').includes(sourceId) || (s.title || '').includes(sourceId)
  })
}

function onCitationClick(e: MouseEvent): void {
  const target = e.target as HTMLElement
  if (target.classList.contains('citation-ref')) {
    const sourceId = target.getAttribute('data-source-id') || ''
    emit('source-click', sourceId)
  }
}

function onCitationHover(e: MouseEvent): void {
  const target = e.target as HTMLElement
  if (!target.classList.contains('citation-ref')) {
    tooltipVisible.value = false
    return
  }
  const sourceId = target.getAttribute('data-source-id') || ''
  const source = findSource(sourceId)
  if (source) {
    const lines = [
      source.title || '未知来源',
      source.url ? `🔗 ${source.url}` : '',
      source.snippet ? source.snippet.slice(0, 100) : '',
    ].filter(Boolean)
    tooltipContent.value = lines.join('\n')
  } else {
    tooltipContent.value = `[${sourceId}]`
  }
  tooltipX.value = e.clientX + 10
  tooltipY.value = e.clientY + 10
  tooltipVisible.value = true
}

function hideTooltip(): void {
  tooltipVisible.value = false
}
</script>

<template>
  <div class="markdown-render" @click="copyCode" @mousemove="onCitationHover" @mouseleave="hideTooltip">
    <div class="markdown-body" v-html="html" @click="onCitationClick" />
    <button v-if="showExport" class="export-btn" @click.stop="emit('export-markdown')">
      📥 导出 MD
    </button>
    <transition name="fade">
      <span v-if="copiedId" class="copy-toast">已复制</span>
    </transition>
    <!-- P7-3: 角标 tooltip -->
    <div
      v-if="tooltipVisible"
      class="citation-tooltip"
      :style="{ left: tooltipX + 'px', top: tooltipY + 'px' }"
    >
      <pre class="tooltip-text">{{ tooltipContent }}</pre>
    </div>
  </div>
</template>

<style scoped>
.markdown-render {
  position: relative;
}
.markdown-render :deep(pre.hljs) {
  position: relative;
  border-radius: 8px;
  overflow-x: auto;
}
.markdown-render :deep(pre.hljs::after) {
  content: '📋';
  position: absolute;
  top: 4px;
  right: 8px;
  font-size: 14px;
  cursor: pointer;
  opacity: 0.4;
  transition: opacity 0.2s;
}
.markdown-render :deep(pre.hljs:hover::after) {
  opacity: 1;
}
.markdown-render :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 8px 0;
}
.markdown-render :deep(th),
.markdown-render :deep(td) {
  border: 1px solid #e0e0e0;
  padding: 6px 12px;
  text-align: left;
}
.markdown-render :deep(th) {
  background: #f5f5f5;
  font-weight: 600;
}
.markdown-render :deep(blockquote) {
  border-left: 3px solid #d0d0d0;
  margin: 8px 0;
  padding: 4px 16px;
  color: #666;
}
.markdown-render :deep(a) {
  color: #3366cc;
  text-decoration: none;
}
.markdown-render :deep(a:hover) {
  text-decoration: underline;
}
/* P7-3: 角标样式 */
.markdown-render :deep(.citation-ref) {
  color: #3f67d4;
  font-size: 0.75em;
  cursor: pointer;
  font-weight: 600;
  vertical-align: super;
  margin: 0 1px;
  transition: color 0.15s;
}
.markdown-render :deep(.citation-ref:hover) {
  color: #1a3a8a;
  text-decoration: underline;
}
.export-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border: 1px solid #d9e3f9;
  border-radius: 6px;
  background: #f8faff;
  color: #3f67d4;
  font-size: 12px;
  cursor: pointer;
  margin-top: 8px;
  transition: background 0.15s;
}
.export-btn:hover {
  background: #eef2fd;
}
.citation-tooltip {
  position: fixed;
  background: rgba(30, 30, 30, 0.95);
  color: #fff;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 12px;
  z-index: 10000;
  max-width: 360px;
  pointer-events: none;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
}
.tooltip-text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.5;
}
.copy-toast {
  position: fixed;
  bottom: 20px;
  right: 20px;
  background: #333;
  color: #fff;
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 13px;
  z-index: 9999;
}
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.3s;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}
</style>
