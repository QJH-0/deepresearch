/**
 * 会话令牌存储。
 *
 * 单独成模块（而不是放在 Pinia store 里）的原因：api 层需要在非组件上下文
 * 读取令牌与清理会话，直接依赖 store 会形成循环引用。
 */

const TOKEN_KEY = 'dr.token'
const USER_KEY = 'dr.user'

export interface AuthUser {
  user_id: string
  role: string
}

export function getToken(): string {
  return (localStorage.getItem(TOKEN_KEY) || '').trim()
}

export function getUser(): AuthUser | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as AuthUser
    return parsed && parsed.user_id ? parsed : null
  } catch {
    return null
  }
}

export function setSession(token: string, user: AuthUser): void {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function clearSession(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

/** 请求头：有令牌时附带 Bearer。 */
export function authHeaders(): Record<string, string> {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/** 会话失效时统一跳登录页（保留原地址以便登录后跳回）。 */
export function redirectToLogin(): void {
  clearSession()
  const current = window.location.pathname + window.location.search
  if (current.startsWith('/login')) return
  window.location.assign(`/login?redirect=${encodeURIComponent(current)}`)
}
