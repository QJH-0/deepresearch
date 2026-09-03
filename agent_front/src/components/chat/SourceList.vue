<script setup lang="ts">
/**
 * SourceList — 来源列表（P7 完善角标 tooltip + 侧栏，先占位）。
 *
 * 显示当前消息的来源列表，支持点击跳转。
 */
import { computed } from 'vue'
import type { SourceItem } from '../../types/events.gen'

const props = defineProps<{
  sources: SourceItem[]
}>()

const visibleSources = computed(() => props.sources.slice(0, 20))
</script>

<template>
  <div v-if="sources.length" class="source-list">
    <p class="source-title">来源（{{ sources.length }}）</p>
    <div class="source-items">
      <a
        v-for="(src, idx) in visibleSources"
        :key="idx"
        :href="src.url || '#'"
        target="_blank"
        rel="noreferrer noopener"
        class="source-item"
        :title="src.snippet"
      >
        <span class="source-index">[{{ idx + 1 }}]</span>
        <span class="source-text">{{ src.title || src.url || '未知来源' }}</span>
        <span v-if="src.source_type === 'kb'" class="source-badge">KB</span>
      </a>
    </div>
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
.source-title {
  font-size: 12px;
  color: #8b9bc0;
  margin-bottom: 6px;
}
.source-items {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.source-item {
  display: flex;
  align-items: baseline;
  gap: 4px;
  font-size: 12px;
  color: #3366cc;
  text-decoration: none;
  line-height: 1.5;
}
.source-item:hover {
  text-decoration: underline;
}
.source-index {
  color: #8b9bc0;
  flex-shrink: 0;
  font-size: 11px;
}
.source-text {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-badge {
  font-size: 10px;
  background: #e6f7ff;
  color: #1890ff;
  border-radius: 3px;
  padding: 0 4px;
  flex-shrink: 0;
}
</style>
