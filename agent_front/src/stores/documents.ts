/**
 * Documents Store — 文档库管理。
 *
 * 从旧版 session.ts 中拆出，knowledge 组件族接此 store。
 * 上传进度、文档列表、统计信息统一管理。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import type {
  DocumentItem,
  DocumentListResult,
  DocumentStats,
  UploadLimits,
  UploadTask,
} from '../types'
import {
  fetchDocuments,
  fetchDocumentStats,
  fetchUploadLimits,
  deleteDocument,
  batchDeleteDocuments,
  retryDocument,
  uploadDocument,
} from '../api'

export const useDocumentsStore = defineStore('documents', () => {
  const documents = ref<DocumentItem[]>([])
  const stats = ref<DocumentStats | null>(null)
  const loading = ref(false)
  const error = ref('')
  const uploadTasks = ref<UploadTask[]>([])
  const uploadLimits = ref<UploadLimits | null>(null)

  async function load(keyword = ''): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      const result: DocumentListResult = await fetchDocuments(keyword)
      documents.value = result.documents || []
      stats.value = result.stats || null
    } catch (err) {
      error.value = err instanceof Error ? err.message : '加载文档列表失败'
    } finally {
      loading.value = false
    }
  }

  async function loadStats(): Promise<void> {
    try {
      stats.value = await fetchDocumentStats()
    } catch {
      // 静默失败
    }
  }

  async function loadLimits(): Promise<void> {
    try {
      uploadLimits.value = await fetchUploadLimits()
    } catch {
      // 静默失败
    }
  }

  async function remove(docId: string): Promise<boolean> {
    try {
      await deleteDocument(docId)
      documents.value = documents.value.filter((d) => d.doc_id !== docId)
      return true
    } catch {
      return false
    }
  }

  async function batchRemove(docIds: string[]): Promise<number> {
    try {
      const result = await batchDeleteDocuments(docIds)
      const removed = new Set(result.doc_ids)
      documents.value = documents.value.filter((d) => !removed.has(d.doc_id))
      return result.deleted
    } catch {
      return 0
    }
  }

  async function retry(docId: string): Promise<void> {
    try {
      await retryDocument(docId)
      void load()
    } catch {
      // 静默失败
    }
  }

  async function upload(file: File, onProgress?: (percent: number) => void): Promise<void> {
    const taskId = `upload-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`
    const task: UploadTask = {
      id: taskId,
      filename: file.name,
      size: file.size,
      stage: 'uploading',
      percent: 0,
      chunkCount: 0,
      indexedChunks: 0,
      error: '',
      docId: '',
    }
    uploadTasks.value.push(task)

    try {
      const result = await uploadDocument(file, (pct) => {
        task.percent = pct
        onProgress?.(pct)
      })
      task.stage = 'done'
      task.docId = result.doc_id
      task.chunkCount = result.chunks
      // 刷新列表
      void load()
    } catch (err) {
      task.stage = 'failed'
      task.error = err instanceof Error ? err.message : '上传失败'
    }
  }

  function removeUploadTask(taskId: string): void {
    uploadTasks.value = uploadTasks.value.filter((t) => t.id !== taskId)
  }

  return {
    documents,
    stats,
    loading,
    error,
    uploadTasks,
    uploadLimits,
    load,
    loadStats,
    loadLimits,
    remove,
    batchRemove,
    retry,
    upload,
    removeUploadTask,
  }
})
