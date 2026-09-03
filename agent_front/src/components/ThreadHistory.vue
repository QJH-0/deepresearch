<script setup lang="ts">
/**
 * 会话历史列表（重构版）— 接 threads store（Pinia）。
 * 按 置顶 / 今天 / 昨天 / 近 7 天 / 更早 分组。
 */
import { computed, nextTick, ref, type ComponentPublicInstance } from 'vue'
import type { ThreadItem } from '../types'
import { groupThreadsByDate, formatThreadTime } from '../utils/datetime'
import { useThreadsStore } from '../stores/threads'

const emit = defineEmits<{ (e: 'select', threadId: string): void }>()

const {
  threads,
  currentThreadId,
  loading: threadsLoading,
  error: threadsError,
  load,
  renameThread,
  togglePin,
  removeThread,
} = useThreadsStore()

const keyword = ref('')
const editingId = ref('')
const editingTitle = ref('')
const menuOpenId = ref('')
const pendingDeleteId = ref('')
const editInput = ref<HTMLInputElement | null>(null)

function setEditInput(el: Element | ComponentPublicInstance | null): void {
  editInput.value = el as HTMLInputElement | null
}

const groups = computed(() => groupThreadsByDate(threads))

const visibleGroups = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return groups.value
  return groups.value
    .map((g) => ({
      ...g,
      threads: g.threads.filter((t) => t.title.toLowerCase().includes(kw)),
    }))
    .filter((g) => g.threads.length > 0)
})

let searchTimer: ReturnType<typeof setTimeout> | null = null
function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => void load(keyword.value), 250)
}

async function onSelect(thread: ThreadItem) {
  if (editingId.value) return
  emit('select', thread.thread_id)
}

function openMenu(threadId: string) {
  menuOpenId.value = menuOpenId.value === threadId ? '' : threadId
}
function closeMenu() { menuOpenId.value = '' }

async function startRename(thread: ThreadItem) {
  closeMenu()
  editingId.value = thread.thread_id
  editingTitle.value = thread.title
  await nextTick()
  editInput.value?.focus()
  editInput.value?.select()
}

async function commitRename(thread: ThreadItem) {
  const id = editingId.value
  const title = editingTitle.value.trim()
  editingId.value = ''
  if (!id || !title || title === thread.title) return
  await renameThread(id, title)
}

function cancelRename() { editingId.value = '' }

function askDelete(threadId: string) {
  closeMenu()
  pendingDeleteId.value = threadId
}

async function confirmDelete() {
  const id = pendingDeleteId.value
  pendingDeleteId.value = ''
  if (!id) return
  await removeThread(id)
}

async function onTogglePin(thread: ThreadItem) {
  closeMenu()
  await togglePin(thread.thread_id, !thread.pinned)
}

function statusIcon(thread: ThreadItem): string {
  return thread.completed ? '✓' : '⋯'
}
</script>

<template>
  <section class="thread-history">
    <div class="history-head">
      <p class="section-title">会话历史</p>
      <span v-if="threads.length" class="history-count">{{ threads.length }}</span>
    </div>

    <div class="history-search">
      <span class="search-icon">🔍</span>
      <input
        v-model="keyword"
        class="search-input"
        type="search"
        placeholder="搜索会话标题"
        @input="onSearchInput"
      />
      <button v-if="keyword" class="search-clear" title="清空" @click="keyword = ''; load('')">✕</button>
    </div>

    <p v-if="threadsError" class="history-error">{{ threadsError }}</p>
    <p v-else-if="threadsLoading && !threads.length" class="history-empty">加载中…</p>
    <p v-else-if="!threads.length" class="history-empty">
      {{ keyword ? '没有匹配的会话' : '还没有会话，发送第一条消息后会自动出现在这里' }}
    </p>

    <div v-else class="thread-groups" @click="closeMenu">
      <div v-for="group in visibleGroups" :key="group.key" class="thread-group">
        <p class="group-label">{{ group.label }}</p>
        <div
          v-for="thread in group.threads"
          :key="thread.thread_id"
          class="thread-item"
          :class="{ active: thread.thread_id === currentThreadId, pinned: thread.pinned }"
          @click="onSelect(thread)"
        >
          <span class="thread-status" :class="thread.completed ? 'done' : 'pending'">
            {{ statusIcon(thread) }}
          </span>

          <input
            v-if="editingId === thread.thread_id"
            :ref="setEditInput"
            v-model="editingTitle"
            class="thread-rename-input"
            @click.stop
            @keyup.enter.prevent="commitRename(thread)"
            @keyup.esc.prevent="cancelRename"
            @blur="commitRename(thread)"
          />
          <template v-else>
            <span class="thread-title" :title="thread.title">{{ thread.title }}</span>
            <span class="thread-meta">
              <span v-if="thread.message_count" class="thread-count">{{ thread.message_count }}</span>
              <span class="thread-time">{{ formatThreadTime(thread.updated_at || thread.created_at) }}</span>
            </span>
            <button class="thread-menu-btn" title="更多操作" @click.stop="openMenu(thread.thread_id)">⋮</button>
            <div v-if="menuOpenId === thread.thread_id" class="thread-menu" @click.stop>
              <button @click="onTogglePin(thread)">{{ thread.pinned ? '取消置顶' : '置顶会话' }}</button>
              <button @click="startRename(thread)">重命名</button>
              <button class="danger" @click="askDelete(thread.thread_id)">删除会话</button>
            </div>
          </template>
        </div>
      </div>
    </div>

    <div v-if="pendingDeleteId" class="confirm-mask" @click="pendingDeleteId = ''">
      <div class="confirm-card" @click.stop>
        <p class="confirm-title">删除这个会话？</p>
        <p class="confirm-desc">只会从历史列表中移除，可以随时重新开始一段对话。</p>
        <div class="confirm-actions">
          <button class="btn ghost" @click="pendingDeleteId = ''">取消</button>
          <button class="btn danger" @click="confirmDelete">删除</button>
        </div>
      </div>
    </div>
  </section>
</template>
