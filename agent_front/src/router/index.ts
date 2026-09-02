/**
 * 路由表。
 *
 * 之前「对话」和「知识库上传/管理」挤在同一个侧边栏里 —— 侧栏被上传区
 * 和文档列表占满，会话历史反而被挤到看不见。拆成两个一级页面后：
 *   /chat      只做对话
 *   /knowledge 只做知识库（上传 + 管理 + 向量化状态）
 */
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/chat' },
  {
    path: '/chat/:threadId?',
    name: 'chat',
    component: () => import('../views/ChatView.vue'),
    props: true,
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('../views/KnowledgeView.vue'),
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
