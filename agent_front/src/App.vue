<script setup lang="ts">
/**
 * 应用外壳（重构版）— 左侧固定侧边栏 + 右侧路由视图。
 * 接 threads store（Pinia），不再用模块级单例。
 */
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import AppSidebar from './components/AppSidebar.vue'
import { useThreadsStore } from './stores/threads'

const router = useRouter()
const { load, requestNewChat } = useThreadsStore()

function handleNewChat() {
  if (router.currentRoute.value.name !== 'chat') {
    void router.push({ name: 'chat' })
  }
  requestNewChat()
}

function handleSelectThread(threadId: string) {
  void router.push({ name: 'chat', params: { threadId } })
}

onMounted(() => { void load() })
</script>

<template>
  <div class="app-shell">
    <AppSidebar @new-chat="handleNewChat" @select-thread="handleSelectThread" />
    <div class="app-main">
      <RouterView />
    </div>
  </div>
</template>
