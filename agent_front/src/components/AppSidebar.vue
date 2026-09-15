<script setup lang="ts">
/**
 * 应用侧边栏 — 品牌 + 新建会话 + 页面导航 + 会话历史 + 当前账号。
 *
 * 图标统一用内联 SVG（同一套描边风格），不使用 emoji：
 * emoji 在不同平台字形差异大，且无法与文字基线对齐。
 * 身份展示与登出放在底部，与导航项分离（危险操作不与导航混排）。
 */
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import ThreadHistory from './ThreadHistory.vue'
import { useAuthStore } from '../stores/auth'
import { useThreadsStore } from '../stores/threads'

const emit = defineEmits<{
  (e: 'new-chat'): void
  (e: 'select-thread', threadId: string): void
}>()

const router = useRouter()
const auth = useAuthStore()
const threadsStore = useThreadsStore()
const { threads, loading } = storeToRefs(threadsStore)

const navItems = [
  { to: '/chat', label: '研究对话', icon: 'chat' },
  { to: '/knowledge', label: '知识库', icon: 'library' },
] as const

const accountLabel = computed(() => auth.user?.user_id || '未登录')
const roleLabel = computed(() => (auth.isAdmin ? '管理员' : '成员'))

function logout(): void {
  auth.logout()
  threadsStore.reset()
  void router.replace({ name: 'login' })
}
</script>

<template>
  <aside class="app-sidebar">
    <div class="sidebar-brand">
      <span class="brand-badge">多智能体研究工作台</span>
      <h1>DeepResearch</h1>
      <p class="brand-desc">检索、交叉验证、成文，全程可溯源</p>
    </div>

    <button class="new-chat-btn" type="button" @click="emit('new-chat')">
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
        <path d="M8 3v10M3 8h10" fill="none" stroke="currentColor" stroke-width="1.6"
              stroke-linecap="round" />
      </svg>
      新建会话
    </button>

    <nav class="sidebar-nav" aria-label="主导航">
      <RouterLink
        v-for="item in navItems"
        :key="item.to"
        :to="item.to"
        class="nav-item"
        active-class="active"
      >
        <span class="nav-icon" aria-hidden="true">
          <svg v-if="item.icon === 'chat'" viewBox="0 0 20 20" width="18" height="18">
            <path d="M4 5h12v8H8l-4 3V5z" fill="none" stroke="currentColor" stroke-width="1.4"
                  stroke-linejoin="round" />
          </svg>
          <svg v-else viewBox="0 0 20 20" width="18" height="18">
            <path d="M4 4h5v12H4zM11 4h5v12h-5z" fill="none" stroke="currentColor"
                  stroke-width="1.4" stroke-linejoin="round" />
          </svg>
        </span>
        <span>{{ item.label }}</span>
      </RouterLink>
    </nav>

    <ThreadHistory
      class="sidebar-history"
      @select="(id: string) => emit('select-thread', id)"
    />

    <div class="sidebar-footer">
      <div class="account">
        <span class="account-avatar" aria-hidden="true">
          {{ accountLabel.slice(0, 1).toUpperCase() }}
        </span>
        <span class="account-body">
          <span class="account-name">{{ accountLabel }}</span>
          <span class="account-role">{{ roleLabel }}</span>
        </span>
        <button class="account-logout" type="button" @click="logout">退出</button>
      </div>

      <div class="sidebar-meta">
        <span>{{ threads.length }} 个会话</span>
        <button class="link-btn" type="button" :disabled="loading" @click="threadsStore.load()">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.account {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.account-avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  border-radius: var(--r-sm);
  background: var(--signal-wash);
  color: var(--signal-ink);
  font-size: var(--fs-sm);
  font-weight: 500;
}

.account-body {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}

.account-name {
  font-size: var(--fs-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-role {
  font-size: var(--fs-xs);
  color: var(--ink-3);
}

.account-logout {
  border: 1px solid var(--line);
  border-radius: var(--r-sm);
  background: var(--surface);
  color: var(--ink-2);
  padding: 3px var(--sp-2);
  font-size: var(--fs-xs);
}

.account-logout:hover {
  border-color: var(--line-strong);
  color: var(--ink);
}

.sidebar-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: var(--sp-3);
  font-size: var(--fs-xs);
  color: var(--ink-3);
}

.link-btn {
  border: none;
  background: none;
  padding: 0;
  color: var(--signal);
  font-size: var(--fs-xs);
}

.link-btn:disabled {
  color: var(--ink-3);
  cursor: default;
}
</style>
