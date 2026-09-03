<script setup lang="ts">
/**
 * MarkdownRender — 使用 markdown-it + highlight.js + katex 渲染。
 *
 * 替代旧版手写 markdownToHtml，支持代码高亮、表格、数学公式。
 * 代码块附带复制按钮。
 */
import { computed, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import katex from '@vscode/markdown-it-katex'
import 'highlight.js/styles/github.css'
import 'katex/dist/katex.min.css'

const props = defineProps<{ content: string }>()

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

const html = computed(() => md.render(props.content || ''))

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
</script>

<template>
  <div class="markdown-render" @click="copyCode">
    <div class="markdown-body" v-html="html" />
    <transition name="fade">
      <span v-if="copiedId" class="copy-toast">已复制</span>
    </transition>
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
