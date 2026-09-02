<script setup lang="ts">
/**
 * 会话历史列表。
 *
 * 相比旧版做了这些修正：
 *  1. 会话列表不再是「有内容才显示」，空态也给出明确提示
 *  2. 按 置顶 / 今天 / 昨天 / 近 7 天 / 更早 分组，而不是按 thread_id 字符串排序
 *  3. 支持搜索、重命名（内联编辑）、置顶、删除（二次确认）
 *  4. 当前会话高亮，新建会话不会把历史顶掉
 */
import { computed, nextTick, ref, type ComponentPublicInstance } from 'vue'
import type { ThreadItem } from '../types'
import { groupThreadsByDate, formatThreadTime } from '../utils/datetime'
import { useSession } from '../stores/session'

const emit = defineEmits<{ (e: 'select', threadId: string): void }>()

const {
  threads,
  currentThreadId,
  threadsLoading,
  threadsError,
  loadThreads,
  renameThread,
  togglePin,
  removeThread,
} = useSession()

const keyword = ref('')
const editingId = ref('')
const editingTitle = ref('')
const menuOpenId = ref('')
const pendingDeleteId = ref('')
const editInput = ref<HTMLInputElement | null>(null)

/**
 * 用函数 ref 而不是字符串 ref。
 *
 * 这个 input 在 v-for 内部，字符串 ref 会被 Vue 收集成数组，
 * 直接 .focus() 会炸；函数 ref 拿到的就是当前那一个元素。
 */
function setEditInput(el: Element | ComponentPublicInstance | null): void {
  editInput.value = el as HTMLInputElement | null
}

const groups = computed(() => groupThreadsByDate(threads.value))

// 前端二次过滤：后端已支持 keyword，但输入框是即时的，本地过滤更跟手
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
  searchTimer = setTimeout(() => void loadThreads(keyword.value), 250)
}

async function onSelect(thread: ThreadItem) {
  if (editingId.value) return
  emit('select', thread.thread_id)
}

function openMenu(threadId: string) {
  menuOpenId.value = menuOpenId.value === threadId ? '' : threadId
}

function closeMenu() {
  menuOpenId.value = ''
}

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

function cancelRename() {
  editingId.value = ''
}

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
  if (!thread.completed) return '⋯'
  return '✓'
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
      <button v-if="keyword" class="search-clear" title="清空" @click="keyword = ''; loadThreads('')">✕</button>
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
          :class="{
            active: thread.thread_id === currentThreadId,
            pinned: thread.pinned,
          }"
          @click="onSelect(thread)"
        >
          <span class="thread-status" :class="thread.completed ? 'done' : 'pending'" :title="thread.completed ? '已产出结论' : '进行中'">
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

    <!-- 删除二次确认：破坏性操作必须有确认，避免误删历史 -->
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
