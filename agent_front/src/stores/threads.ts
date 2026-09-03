/**
 * Threads Store — 会话列表管理。
 *
 * 核心设计：
 * - 事件驱动刷新：run.completed/error 后调 refresh()，替代旧版 10s 轮询
 * - 标题自动生成：run.completed 后后端 LLM 生成标题，前端 refresh 单条
 * - 乐观更新（重命名/置顶/删除），失败回滚
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { ThreadItem } from '../types'
import {
  deleteThread as apiDeleteThread,
  fetchThreads,
  getUserId,
  pinThread as apiPinThread,
  renameThread as apiRenameThread,
  setUserId as persistUserId,
} from '../api'

export const useThreadsStore = defineStore('threads', () => {
  const userId = ref(getUserId())
  const threads = ref<ThreadItem[]>([])
  const currentThreadId = ref('')
  const loading = ref(false)
  const error = ref('')
  /** 新建会话信号（侧边栏触发，ChatView 响应） */
  const newChatSignal = ref(0)

  async function load(keyword = ''): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      const data = await fetchThreads(keyword)
      threads.value = data.threads || []
    } catch (err) {
      error.value = err instanceof Error ? err.message : '加载会话列表失败'
      threads.value = []
    } finally {
      loading.value = false
    }
  }

  /** 事件驱动刷新（run.completed/error 后调用） */
  async function refresh(threadId?: string): Promise<void> {
    if (threadId) {
      // 只刷新单条（标题可能已更新）
      try {
        await load()
      } catch {
        // 静默失败，不打断用户
      }
    } else {
      await load()
    }
  }

  function startNewThread(): string {
    const id = `thread_${Date.now()}`
    currentThreadId.value = id
    return id
  }

  function requestNewChat(): void {
    newChatSignal.value += 1
  }

  function selectThread(threadId: string): void {
    currentThreadId.value = threadId
  }

  async function renameThread(threadId: string, title: string): Promise<boolean> {
    const trimmed = title.trim()
    if (!trimmed) return false
    const target = threads.value.find((t) => t.thread_id === threadId)
    const previous = target?.title
    if (target) target.title = trimmed
    try {
      const updated = await apiRenameThread(threadId, trimmed)
      if (updated && target) Object.assign(target, updated)
      return true
    } catch {
      if (target && previous !== undefined) target.title = previous
      return false
    }
  }

  async function togglePin(threadId: string, pinned: boolean): Promise<void> {
    const target = threads.value.find((t) => t.thread_id === threadId)
    if (target) target.pinned = pinned
    try {
      const updated = await apiPinThread(threadId, pinned)
      if (updated && target) Object.assign(target, updated)
    } catch {
      // 置顶失败只影响排序，刷新后会自愈
    }
  }

  async function removeThread(threadId: string): Promise<boolean> {
    const removed = threads.value.find((t) => t.thread_id === threadId)
    threads.value = threads.value.filter((t) => t.thread_id !== threadId)
    try {
      const result = await apiDeleteThread(threadId)
      if (!result.deleted && removed) threads.value = [removed, ...threads.value]
      if (result.deleted && currentThreadId.value === threadId) currentThreadId.value = ''
      return result.deleted
    } catch {
      if (removed) threads.value = [removed, ...threads.value]
      return false
    }
  }

  function changeUserId(next: string): void {
    userId.value = next.trim() || 'default_user'
    persistUserId(userId.value)
    currentThreadId.value = ''
    void load()
  }

  return {
    userId,
    threads,
    currentThreadId,
    loading,
    error,
    newChatSignal,
    load,
    refresh,
    startNewThread,
    requestNewChat,
    selectThread,
    renameThread,
    togglePin,
    removeThread,
    changeUserId,
  }
})
