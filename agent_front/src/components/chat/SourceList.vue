<script setup lang="ts">
/**
 * SourceList — 来源列表（P7 完整版）。
 *
 * P7 增强：
 * - 按 source_type 分组（网络来源 / 知识库来源）
 * - 条目 = 编号 + title + 类型徽标
 * - web 点击新开 url，kb 点击打开知识库文档定位
 * - 会话级汇总视图（当前报告全部来源）
 */
import { computed, ref } from 'vue'
import type { SourceItem } from '../../types/events.gen'

const props = defineProps<{
  sources: SourceItem[]
  /** 是否侧栏模式（全部来源汇总），默认消息内嵌 */
  sidebar?: boolean
}>()

const emit = defineEmits<{
  (e: 'source-click', source: SourceItem): void
}>()

const expanded = ref(false)

const visibleSources = computed(() =>
  expanded.value ? props.sources : props.sources.slice(0, 6)
)

const hiddenCount = computed(() => Math.max(0, props.sources.length - 6))

const webSources = computed(() =>
  visibleSources.value.filter(s => s.source_type !== 'kb')
)

const kbSources = computed(() =>
  visibleSources.value.filter(s => s.source_type === 'kb')
)

function handleClick(src: SourceItem): void {
  if (src.source_type === 'kb') {
    // 知识库来源 — 跳转知识库页面
    emit('source-click', src)
  } else if (src.url) {
    // 网络来源 — 新开页
    window.open(src.url, '_blank', 'noopener,noreferrer')
  }
}
</script>

<template>
  <div v-if="sources.length" class="source-list" :class="{ sidebar: sidebar }">
    <div class="source-header" @click="expanded = !expanded">
      <span class="source-title">📎 来源（{{ sources.length }}）</span>
      <span class="source-toggle">{{ expanded ? '收起' : '展开' }}</span>
    </div>

    <div v-if="webSources.length" class="source-group">
      <p class="group-label">🌐 网络来源</p>
      <div class="source-items">
        <a
          v-for="(src, idx) in webSources"
          :key="'web-' + idx"
          :href="src.url || '#'"
          target="_blank"
          rel="noreferrer noopener"
          class="source-item web"
          :title="src.snippet"
          @click.prevent="handleClick(src)"
        >
          <span class="source-text">{{ src.title || src.url || '未知来源' }}</span>
          <span v-if="src.snippet" class="source-snippet">{{ src.snippet.slice(0, 80) }}…</span>
        </a>
      </div>
    </div>

    <div v-if="kbSources.length" class="source-group">
      <p class="group-label">📚 知识库来源</p>
      <div class="source-items">
        <button
          v-for="(src, idx) in kbSources"
          :key="'kb-' + idx"
          class="source-item kb"
          :title="src.snippet"
          @click="handleClick(src)"
        >
          <span class="source-text">{{ src.title || src.chunk_id || '未知文档' }}</span>
          <span v-if="src.snippet" class="source-snippet">{{ src.snippet.slice(0, 80) }}…</span>
        </button>
      </div>
    </div>

    <button v-if="hiddenCount > 0 && !expanded" class="source-more" @click="expanded = true">
      还有 {{ hiddenCount }} 个来源…
    </button>
  </div>
</template>

<style scoped>
.source-list {
  margin-top: 8px;
  padding: 8px 12px;
  border: 1px solid #f0f0f0;
  border-radius: 8px;
  background: #fafbfd;
}
.source-list.sidebar {
  border: none;
  background: transparent;
  padding: 0;
}
.source-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
  user-select: none;
  margin-bottom: 6px;
}
.source-title {
  font-size: 13px;
  font-weight: 500;
  color: #5c6f98;
}
.source-toggle {
  font-size: 11px;
  color: #3f67d4;
}
.source-group {
  margin-bottom: 8px;
}
.group-label {
  font-size: 11px;
  color: #8b9bc0;
  margin: 6px 0 4px;
}
.source-items {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.source-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 4px 8px;
  border-radius: 6px;
  font-size: 12px;
  text-decoration: none;
  border: none;
  background: transparent;
  cursor: pointer;
  text-align: left;
  width: 100%;
  transition: background 0.15s;
}
.source-item:hover {
  background: #eef2fd;
}
.source-item.web {
  color: #3366cc;
}
.source-item.kb {
  color: #1890ff;
}
.source-text {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-snippet {
  font-size: 11px;
  color: #8b9bc0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-more {
  display: block;
  width: 100%;
  text-align: center;
  font-size: 11px;
  color: #3f67d4;
  background: none;
  border: none;
  cursor: pointer;
  padding: 4px;
  border-radius: 4px;
}
.source-more:hover {
  background: #eef2fd;
}
</style>
