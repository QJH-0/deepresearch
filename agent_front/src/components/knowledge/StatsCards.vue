<script setup lang="ts">
/**
 * 知识库总览卡片。
 *
 * 「已进向量库 / 切片总数」是用户最关心的一对数 —— 之前界面上完全看不到
 * 文档到底有没有完成向量化，这里把它做成最显眼的一张卡 + 全局进度条。
 */
import { computed } from 'vue'
import type { DocumentStats } from '../../types'
import { formatBytes } from '../../utils/datetime'

const props = defineProps<{ stats: DocumentStats | null }>()

const cards = computed(() => {
  const s = props.stats
  return [
    {
      key: 'docs',
      label: '文档总数',
      value: String(s?.document_count ?? 0),
      hint: `${s?.ready_documents ?? 0} 个可被检索`,
      tone: 'blue',
    },
    {
      key: 'chunks',
      label: '知识切片',
      value: String(s?.chunk_count ?? 0),
      hint: `合计 ${formatBytes(s?.size_bytes ?? 0)}`,
      tone: 'slate',
    },
    {
      key: 'indexed',
      label: '已进入向量库',
      value: String(s?.indexed_chunks ?? 0),
      hint: `${s?.progress ?? 0}% 完成`,
      tone: 'green',
    },
    {
      key: 'pending',
      label: '待处理',
      value: String(s?.pending_chunks ?? 0),
      hint: '正在排队等待 Embedding',
      tone: 'amber',
    },
    {
      key: 'failed',
      label: '失败',
      value: String(s?.failed_chunks ?? 0),
      hint: '可点文档行上的「重试」',
      tone: 'red',
    },
  ]
})

const hasPending = computed(() => (props.stats?.pending_chunks ?? 0) > 0)
</script>

<template>
  <section class="stats-section">
    <div class="stats-grid">
      <article v-for="card in cards" :key="card.key" class="stat-card" :class="`tone-${card.tone}`">
        <p class="stat-label">{{ card.label }}</p>
        <p class="stat-value">{{ card.value }}</p>
        <p class="stat-hint">{{ card.hint }}</p>
      </article>
    </div>

    <div class="global-progress">
      <div class="progress-head">
        <span>全库向量化进度</span>
        <span class="progress-value">{{ stats?.progress ?? 0 }}%</span>
      </div>
      <div class="progress-track" :class="{ active: hasPending }">
        <div class="progress-fill" :style="{ width: `${stats?.progress ?? 0}%` }" />
      </div>
      <p class="progress-desc">
        {{ stats?.indexed_chunks ?? 0 }} / {{ stats?.chunk_count ?? 0 }} 个切片已完成 Embedding 并写入 Milvus
        <template v-if="hasPending"> · 队列中还有 {{ stats?.pending_chunks }} 个</template>
        <template v-else-if="(stats?.chunk_count ?? 0) > 0"> · 全部完成，可被检索</template>
      </p>
    </div>
  </section>
</template>
