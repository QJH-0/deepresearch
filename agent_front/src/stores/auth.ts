/**
 * Auth Store — 登录态管理。
 *
 * 令牌与身份的实际存储在 api/token.ts（api 层需要在非组件上下文读取），
 * 这里只做响应式包装与动作编排。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { fetchMe, login as apiLogin } from '../api'
import { clearSession, getToken, getUser, type AuthUser } from '../api/token'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(getUser())
  const token = ref(getToken())
  const loading = ref(false)
  const error = ref('')

  const isAuthenticated = computed(() => Boolean(token.value && user.value))
  const isAdmin = computed(() => user.value?.role === 'admin')

  async function login(userId: string, password: string): Promise<boolean> {
    loading.value = true
    error.value = ''
    try {
      user.value = await apiLogin(userId, password)
      token.value = getToken()
      return true
    } catch (err) {
      error.value = err instanceof Error ? err.message : '登录失败'
      user.value = null
      token.value = ''
      return false
    } finally {
      loading.value = false
    }
  }

  /** 校验本地令牌是否仍有效（刷新页面时调用）。 */
  async function verify(): Promise<boolean> {
    if (!token.value) return false
    try {
      user.value = await fetchMe()
      return true
    } catch {
      logout()
      return false
    }
  }

  function logout(): void {
    clearSession()
    user.value = null
    token.value = ''
    error.value = ''
  }

  return { user, token, loading, error, isAuthenticated, isAdmin, login, verify, logout }
})
