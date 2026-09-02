<script setup lang="ts">
/**
 * 文档管理表格。
 *
 * 相比旧版那几行「文件名 + status + 删除按钮」，这里补齐了判断
 * 「文档到底有没有进向量库」所必需的全部信息：
 *   - 切片数 与 已索引数（indexed / total）
 *   - 逐文档的向量化进度条
 *   - 状态徽章（处理中 / 已入向量库 / 部分失败 / 失败）
 *   - 文件大小、类型、上传时间
 *   - 勾选批量删除、失败单独重试
 */
import { computed, ref } from 'vue'
import type { DocumentItem, VectorState } from '../../types'
import { formatBytes, formatDateTime } from '../../utils/datetime'

const props = defineProps<{
  documents: DocumentItem[]
  loading?: boolean
}>()

const emit = defineEmits<{
  (e: 'delete', doc: DocumentItem): void
  (e: 'delete-selected', ids: string[]): void
  (e: 'retry', doc: DocumentItem): void
}>()

const selected = ref<Set<string>>(new Set())
const sortKey = ref<'created_at' | 'filename' | 'progress'>('created_at')
const sortDesc = ref(true)

const STATE_META: Record<VectorState, { label: string; hint: string }> = {
  empty: { label: '无切片', hint: '解析未产出切片' },
  processing: { label: '向量化中', hint: '切片正在 Embedding 并写入 Milvus' },
  indexed: { label: '已入向量库', hint: '全部切片可被检索' },
  partial: { label: '部分失败', hint: '有切片向量化失败，可点重试' },
  failed: { label: '向量化失败', hint: '全部切片失败，请检查 Embedding 服务' },
}

const sortedDocuments = computed(() => {
  const list = [...props.documents]
  const dir = sortDesc.value ? -1 : 1
  list.sort((a, b) => {
    if (sortKey.value === 'filename') return a.filename.localeCompare(b.filename, 'zh-CN') * dir
    if (sortKey.value === 'progress') return (a.progress - b.progress) * dir
    return (Date.parse(a.created_at) - Date.parse(b.created_at)) * dir
  })
  return list
})

const allSelected = computed(
  () => props.documents.length > 0 && selected.value.size === props.documents.length,
)
const selectedIds = computed(() => Array.from(selected.value))

function toggleAll() {
  if (allSelected.value) selected.value = new Set()
  else selected.value = new Set(props.documents.map((d) => d.doc_id))
}

function toggleOne(docId: string) {
  const next = new Set(selected.value)
  if (next.has(docId)) next.delete(docId)
  else next.add(docId)
  selected.value = next
}

function clearSelection() {
  selected.value = new Set()
}

function sortBy(key: 'created_at' | 'filename' | 'progress') {
  if (sortKey.value === key) sortDesc.value = !sortDesc.value
  else {
    sortKey.value = key
    sortDesc.value = true
  }
}

function sortIndicator(key: string): string {
  if (sortKey.value !== key) return ''
  return sortDesc.value ? '↓' : '↑'
}

function fileIcon(ext: string): string {
  const map: Record<string, string> = {
    '.pdf': '📕',
    '.doc': '📘',
    '.docx': '📘',
    '.md': '📝',
    '.markdown': '📝',
    '.txt': '📄',
    '.html': '🌐',
    '.htm': '🌐',
    '.csv': '📊',
    '.json': '🧩',
  }
  return map[ext.toLowerCase()] || '📄'
}

defineExpose({ selectedIds, clearSelection })
</script>

<template>
  <section class="doc-table-section">
    <div v-if="selectedIds.length" class="bulk-bar">
      <span>已选中 {{ selectedIds.length }} 个文档</span>
      <div class="bulk-actions">
        <button class="btn ghost" @click="clearSelection">取消选择</button>
        <button class="btn danger" @click="emit('delete-selected', selectedIds)">批量删除</button>
      </div>
    </div>

    <div class="table-wrap">
      <table class="doc-table">
        <thead>
          <tr>
            <th class="col-check">
              <input type="checkbox" :checked="allSelected" @change="toggleAll" />
            </th>
            <th class="col-name sortable" @click="sortBy('filename')">
              文件名 {{ sortIndicator('filename') }}
            </th>
            <th class="col-size">大小</th>
            <th class="col-chunks">切片</th>
            <th class="col-vector sortable" @click="sortBy('progress')">
              向量化进度 {{ sortIndicator('progress') }}
            </th>
            <th class="col-state">状态</th>
            <th class="col-time sortable" @click="sortBy('created_at')">
              上传时间 {{ sortIndicator('created_at') }}
            </th>
            <th class="col-ops">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading && !documents.length">
            <td colspan="8" class="empty-cell">加载中…</td>
          </tr>
          <tr v-else-if="!documents.length">
            <td colspan="8" class="empty-cell">
              知识库还是空的。上传文档后，切片会自动进入向量库，进度会在这里实时显示。
            </td>
          </tr>
          <tr v-for="doc in sortedDocuments" :key="doc.doc_id" :class="{ selected: selected.has(doc.doc_id) }">
            <td class="col-check">
              <input type="checkbox" :checked="selected.has(doc.doc_id)" @change="toggleOne(doc.doc_id)" />
            </td>

            <td class="col-name">
              <span class="file-icon">{{ fileIcon(doc.file_ext) }}</span>
              <span class="file-name" :title="doc.filename">{{ doc.filename }}</span>
              <span class="file-ext">{{ doc.file_ext }}</span>
            </td>

            <td class="col-size">{{ formatBytes(doc.file_size) }}</td>

            <td class="col-chunks">
              <span class="chunk-count">{{ doc.chunk_count }}</span>
            </td>

            <td class="col-vector">
              <div class="vector-cell">
                <div class="progress-track slim">
                  <div
                    class="progress-fill"
                    :class="`state-${doc.vector_state}`"
                    :style="{ width: `${doc.progress}%` }"
                  />
                </div>
                <span class="vector-text">
                  {{ doc.indexed_chunks }}/{{ doc.chunk_count }}
                  <span class="vector-pct">{{ doc.progress }}%</span>
                </span>
                <span v-if="doc.failed_chunks > 0" class="vector-failed">失败 {{ doc.failed_chunks }}</span>
              </div>
            </td>

            <td class="col-state">
              <span class="state-badge" :class="`state-${doc.vector_state}`" :title="STATE_META[doc.vector_state].hint">
                <span class="state-dot" />
                {{ STATE_META[doc.vector_state].label }}
              </span>
            </td>

            <td class="col-time">{{ formatDateTime(doc.created_at) }}</td>

            <td class="col-ops">
              <button
                v-if="doc.failed_chunks > 0"
                class="op-btn retry"
                title="重新向量化失败的切片"
                @click="emit('retry', doc)"
              >
                重试
              </button>
              <button class="op-btn danger" title="删除文档" @click="emit('delete', doc)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
