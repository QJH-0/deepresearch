<script setup lang="ts">
/**
 * 应用外壳：左侧固定侧边栏 + 右侧路由视图。
 *
 * 侧边栏负责「会话历史 + 页面导航」，右侧按路由切到
 * 对话页 / 知识库页 —— 两者不再挤在同一屏。
 */
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import AppSidebar from './components/AppSidebar.vue'
import { useSession } from './stores/session'

const router = useRouter()
const { loadThreads, requestNewChat } = useSession()

function handleNewChat() {
  // 不在对话页就先切过去，用信号通知对话页开新会话
  // （不能拿子组件实例：路由切换完成前拿到的还是旧页面组件）
  if (router.currentRoute.value.name !== 'chat') {
    void router.push({ name: 'chat' })
  }
  requestNewChat()
}

function handleSelectThread(threadId: string) {
  void router.push({ name: 'chat', params: { threadId } })
}

onMounted(() => {
  void loadThreads()
})
</script>

<template>
  <div class="app-shell">
    <AppSidebar @new-chat="handleNewChat" @select-thread="handleSelectThread" />
    <div class="app-main">
      <RouterView />
    </div>
  </div>
</template>
