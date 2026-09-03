<script setup lang="ts">
/**
 * 应用侧边栏（重构版）— 品牌 + 新建会话 + 页面导航 + 会话历史 + 设置。
 * 接 threads store（Pinia），不再用模块级单例。
 */
import { ref } from 'vue'
import ThreadHistory from './ThreadHistory.vue'
import { useThreadsStore } from '../stores/threads'

const emit = defineEmits<{
  (e: 'new-chat'): void
  (e: 'select-thread', threadId: string): void
}>()

const { userId, changeUserId, load } = useThreadsStore()

const navItems = [
  { to: '/chat', label: '研究对话', icon: '💬' },
  { to: '/knowledge', label: '知识库', icon: '📚' },
]

const userIdDraft = ref(userId)

function commitUserId() {
  changeUserId(userIdDraft.value)
}
</script>

<template>
  <aside class="app-sidebar">
    <div class="sidebar-brand">
      <p class="brand-badge">AI Copilot</p>
      <h1>DeepResearch</h1>
      <p class="brand-desc">多智能体研究工作台</p>
    </div>

    <button class="new-chat-btn" @click="emit('new-chat')">
      <span class="plus">＋</span> 新建会话
    </button>

    <nav class="sidebar-nav">
      <RouterLink
        v-for="item in navItems"
        :key="item.to"
        :to="item.to"
        class="nav-item"
        active-class="active"
      >
        <span class="nav-icon">{{ item.icon }}</span>
        <span>{{ item.label }}</span>
      </RouterLink>
    </nav>

    <ThreadHistory
      class="sidebar-history"
      @select="(id: string) => emit('select-thread', id)"
    />

    <div class="sidebar-footer">
      <details class="settings-details">
        <summary class="settings-summary">设置</summary>
        <div class="settings-group">
          <label>User ID</label>
          <input
            v-model="userIdDraft"
            class="sidebar-input"
            placeholder="default_user"
            @change="commitUserId"
            @keyup.enter="commitUserId"
          />
          <p class="hint-text">切换用户会重新加载其会话与知识库</p>
        </div>
        <button class="refresh-btn" @click="load()">刷新会话列表</button>
      </details>
    </div>
  </aside>
</template>
