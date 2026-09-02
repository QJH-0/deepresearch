<script setup lang="ts">
/**
 * 知识库页面（独立于对话）。
 *
 * 职责：上传文档 → 观察「是否进入向量库」→ 管理（搜索 / 重试 / 删除）。
 *
 * 上传后的向量化是后端异步流水线，前端用轮询跟进：
 * 有 pending 切片时每 3s 拉一次列表，全部 indexed 后停止，
 * 避免无意义的常驻轮询。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import StatsCards from '../components/knowledge/StatsCards.vue'
import UploadDropzone from '../components/knowledge/UploadDropzone.vue'
import UploadTaskList from '../components/knowledge/UploadTaskList.vue'
import DocumentTable from '../components/knowledge/DocumentTable.vue'
import type { DocumentItem, DocumentStats, UploadLimits, UploadTask } from '../types'
import {
  batchDeleteDocuments,
  deleteDocument,
  fetchDocuments,
  fetchUploadLimits,
  retryDocument,
  uploadDocument,
} from '../api'
import { isSupportedFile } from '../utils/upload'

const documents = ref<DocumentItem[]>([])
const stats = ref<DocumentStats | null>(null)
const limits = ref<UploadLimits | null>(null)
const tasks = ref<UploadTask[]>([])

const listLoading = ref(false)
const listError = ref('')
const keyword = ref('')
const notice = ref('')
const autoRefresh = ref(true)
const pendingDelete = ref<{ ids: string[]; label: string } | null>(null)

const tableRef = ref<InstanceType<typeof DocumentTable> | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | null = null
let searchTimer: ReturnType<typeof setTimeout> | null = null

const hasPendingWork = computed(() => (stats.value?.pending_chunks ?? 0) > 0)
const uploadingCount = computed(
  () => tasks.value.filter((t) => t.stage !== 'done' && t.stage !== 'failed').length,
)

function flash(message: string) {
  notice.value = message
  setTimeout(() => {
    if (notice.value === message) notice.value = ''
  }, 4000)
}

async function loadDocuments() {
  listLoading.value = true
  listError.value = ''
  try {
    const data = await fetchDocuments(keyword.value)
    documents.value = data.documents || []
    stats.value = data.stats
  } catch (err) {
    listError.value = err instanceof Error ? err.message : '加载文档列表失败'
  } finally {
    listLoading.value = false
  }
}

function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => void loadDocuments(), 250)
}

// ── 轮询：只在有切片待处理时才跑 ──────────────────────────────────────
function schedulePolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
  if (!autoRefresh.value || !hasPendingWork.value) return
  pollTimer = setTimeout(async () => {
    await loadDocuments()
    // 上传中的任务进度也跟着刷新
    for (const task of tasks.value) {
      if (task.stage === 'embedding' && task.docId) syncTaskProgress(task)
    }
    schedulePolling()
  }, 3000)
}

function syncTaskProgress(task: UploadTask) {
  const doc = documents.value.find((d) => d.doc_id === task.docId)
  if (!doc) return
  task.chunkCount = doc.chunk_count
  task.indexedChunks = doc.indexed_chunks
  if (doc.vector_state === 'indexed') {
    task.stage = 'done'
    task.percent = 100
  } else if (doc.failed_chunks > 0 && doc.pending_chunks === 0) {
    task.stage = 'failed'
    task.error = `${doc.failed_chunks}/${doc.chunk_count} 个切片向量化失败，可在列表中点「重试」`
  }
}

// ── 上传 ─────────────────────────────────────────────────────────────
function makeTask(file: File): UploadTask {
  return {
    id: `task-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    filename: file.name,
    size: file.size,
    stage: 'queued',
    percent: 0,
    chunkCount: 0,
    indexedChunks: 0,
    error: '',
    docId: '',
  }
}

async function handleUpload(files: File[]) {
  const exts = limits.value?.extensions || []
  const maxBytes = (limits.value?.max_file_size_mb ?? 50) * 1024 * 1024
  const accepted = files.slice(0, limits.value?.max_files_per_batch ?? 20)

  if (files.length > accepted.length) {
    flash(`一次最多上传 ${limits.value?.max_files_per_batch ?? 20} 个文件，多余的已忽略`)
  }

  for (const file of accepted) {
    const task = makeTask(file)
    tasks.value = [task, ...tasks.value]

    // 上传前就拦截：格式 / 大小。比上传后拿到 400/413 更省事
    if (!isSupportedFile(file.name, exts)) {
      task.stage = 'failed'
      task.error = `不支持的文件格式，支持：${exts.join('、')}`
      continue
    }
    if (file.size > maxBytes) {
      task.stage = 'failed'
      task.error = `文件 ${(file.size / 1024 / 1024).toFixed(1)} MB 超过上限 ${limits.value?.max_file_size_mb ?? 50} MB`
      continue
    }

    void runSingleUpload(file, task)
  }
}

async function runSingleUpload(file: File, task: UploadTask) {
  task.stage = 'uploading'
  try {
    const result = await uploadDocument(file, (percent) => {
      task.percent = percent
    })
    // 上传接口同步返回时，PG 里已经有切片了，接下来是异步 Embedding
    task.stage = 'parsing'
    task.docId = result.doc_id || ''
    task.chunkCount = result.chunks || 0

    await loadDocuments()
    task.stage = task.chunkCount > 0 ? 'embedding' : 'done'
    syncTaskProgress(task)
    schedulePolling()
  } catch (err) {
    task.stage = 'failed'
    task.error = err instanceof Error ? err.message : '上传失败'
  }
}

function dismissTask(id: string) {
  tasks.value = tasks.value.filter((t) => t.id !== id)
}

// ── 管理操作 ─────────────────────────────────────────────────────────
function askDelete(doc: DocumentItem) {
  pendingDelete.value = { ids: [doc.doc_id], label: doc.filename }
}

function askDeleteSelected(ids: string[]) {
  if (!ids.length) return
  pendingDelete.value = { ids, label: `${ids.length} 个文档` }
}

async function confirmDelete() {
  const target = pendingDelete.value
  pendingDelete.value = null
  if (!target) return
  const singleId = target.ids.length === 1 ? target.ids[0] : undefined
  try {
    const result = singleId
      ? await deleteDocument(singleId)
      : await batchDeleteDocuments(target.ids)
    flash(result.message || '删除完成')
    tableRef.value?.clearSelection()
    await loadDocuments()
  } catch (err) {
    flash(err instanceof Error ? err.message : '删除失败')
  }
}

async function handleRetry(doc: DocumentItem) {
  try {
    const result = await retryDocument(doc.doc_id)
    flash(result.message || '已重新入队')
    await loadDocuments()
    schedulePolling()
  } catch (err) {
    flash(err instanceof Error ? err.message : '重试失败')
  }
}

onMounted(async () => {
  await loadDocuments()
  try {
    limits.value = await fetchUploadLimits()
  } catch {
    // 拿不到限制就用前端默认值，不阻塞页面
  }
  schedulePolling()
})

onBeforeUnmount(() => {
  if (pollTimer) clearTimeout(pollTimer)
  if (searchTimer) clearTimeout(searchTimer)
})
</script>

<template>
  <main class="knowledge-view">
    <header class="view-header">
      <div>
        <h2>知识库</h2>
        <p>上传文档 → 自动切片 → 写入 Milvus 向量库 → 供研究链路检索</p>
      </div>
      <div class="header-actions">
        <label class="auto-refresh">
          <input v-model="autoRefresh" type="checkbox" @change="schedulePolling" />
          <span>自动刷新进度</span>
        </label>
        <button class="btn ghost" :disabled="listLoading" @click="loadDocuments">
          {{ listLoading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </header>

    <div class="knowledge-body">
      <p v-if="notice" class="notice-bar">{{ notice }}</p>
      <p v-if="listError" class="error-bar">{{ listError }}</p>

      <StatsCards :stats="stats" />

      <section class="upload-panel">
        <div class="panel-head">
          <h3>上传文档</h3>
          <span v-if="uploadingCount" class="panel-badge">{{ uploadingCount }} 个进行中</span>
        </div>
        <UploadDropzone :limits="limits" @upload="handleUpload" />
        <UploadTaskList :tasks="tasks" @dismiss="dismissTask" />
      </section>

      <section class="manage-panel">
        <div class="panel-head">
          <h3>文档管理</h3>
          <div class="doc-search">
            <span class="search-icon">🔍</span>
            <input
              v-model="keyword"
              class="search-input"
              type="search"
              placeholder="按文件名搜索"
              @input="onSearchInput"
            />
          </div>
        </div>

        <DocumentTable
          ref="tableRef"
          :documents="documents"
          :loading="listLoading"
          @delete="askDelete"
          @delete-selected="askDeleteSelected"
          @retry="handleRetry"
        />
      </section>
    </div>

    <!-- 删除确认：批量删除是破坏性操作，必须二次确认 -->
    <div v-if="pendingDelete" class="confirm-mask" @click="pendingDelete = null">
      <div class="confirm-card" @click.stop>
        <p class="confirm-title">确认删除？</p>
        <p class="confirm-desc">
          将删除「{{ pendingDelete.label }}」及其在本地数据库与对象存储中的记录，向量索引会异步清理。
        </p>
        <div class="confirm-actions">
          <button class="btn ghost" @click="pendingDelete = null">取消</button>
          <button class="btn danger" @click="confirmDelete">确认删除</button>
        </div>
      </div>
    </div>
  </main>
</template>
