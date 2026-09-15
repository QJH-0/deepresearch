/**
 * 路由表 + 登录守卫。
 *
 * 页面划分：
 *   /login     登录（唯一公开路由）
 *   /chat      研究对话
 *   /knowledge 知识库（上传 + 管理 + 向量化状态）
 *
 * 守卫只检查本地是否持有令牌（快速、无网络）；令牌失效由 api 层的 401 处理统一兜底。
 */
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { getToken } from '../api/token'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/chat' },
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/chat/:threadId?',
    name: 'chat',
    component: () => import('../views/ChatView.vue'),
    props: true,
    meta: { title: '研究对话' },
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('../views/KnowledgeView.vue'),
    meta: { title: '知识库' },
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const authed = Boolean(getToken())

  if (to.meta.public) {
    // 已登录时不再展示登录页
    return authed && to.name === 'login' ? { name: 'chat' } : true
  }

  if (!authed) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  return true
})

router.afterEach((to) => {
  const title = typeof to.meta.title === 'string' ? to.meta.title : ''
  document.title = title ? `${title} · DeepResearch` : 'DeepResearch'
})

export default router
