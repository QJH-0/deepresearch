<script setup lang="ts">
/**
 * 应用外壳 — 左侧固定侧边栏 + 右侧路由视图。
 *
 * 登录页是全屏布局，不挂侧边栏；其余页面共享同一外壳，
 * 保证导航位置在所有页面一致（导航一致性）。
 */
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppSidebar from './components/AppSidebar.vue'
import { useAuthStore } from './stores/auth'
import { useThreadsStore } from './stores/threads'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const threads = useThreadsStore()

const showSidebar = computed(() => route.name !== 'login')

function handleNewChat(): void {
  if (router.currentRoute.value.name !== 'chat') {
    void router.push({ name: 'chat' })
  }
  threads.requestNewChat()
}

function handleSelectThread(threadId: string): void {
  void router.push({ name: 'chat', params: { threadId } })
}

onMounted(async () => {
  if (!auth.isAuthenticated) return
  // 本地令牌可能已过期：先校验再拉数据，避免刷出一屏 401
  await auth.verify()
  if (auth.isAuthenticated) void threads.load()
})
</script>

<template>
  <div class="app-shell">
    <AppSidebar
      v-if="showSidebar"
      @new-chat="handleNewChat"
      @select-thread="handleSelectThread"
    />
    <div class="app-main">
      <RouterView />
    </div>
  </div>
</template>
