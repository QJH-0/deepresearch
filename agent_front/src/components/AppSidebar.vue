<script setup lang="ts">
/**
 * 应用侧边栏：品牌 + 新建会话 + 页面导航 + 会话历史 + 设置。
 *
 * 知识库上传区已从侧边栏移除（旧版塞在侧栏底部，把会话历史挤没了），
 * 改为 /knowledge 独立页面，这里只保留导航入口。
 */
import { ref } from 'vue'
import ThreadHistory from './ThreadHistory.vue'
import { useSession } from '../stores/session'

const emit = defineEmits<{
  (e: 'new-chat'): void
  (e: 'select-thread', threadId: string): void
}>()

const { userId, changeUserId, loadThreads } = useSession()

const navItems = [
  { to: '/chat', label: '研究对话', icon: '💬' },
  { to: '/knowledge', label: '知识库', icon: '📚' },
]

const userIdDraft = ref(userId.value)

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
      @select="(id) => emit('select-thread', id)"
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
        <button class="refresh-btn" @click="loadThreads()">刷新会话列表</button>
      </details>
    </div>
  </aside>
</template>
