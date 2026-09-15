<script setup lang="ts">
/**
 * LoginView — 登录页。
 *
 * 左侧用产品自己的主张作为主视觉：研究工具的卖点是「结论可追溯」，
 * 所以首屏直接画出证据链，而不是放一张无信息的装饰图。
 */
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const userId = ref('')
const password = ref('')
const showPassword = ref(false)

const canSubmit = computed(() => userId.value.trim().length > 0 && password.value.length > 0)

async function submit(): Promise<void> {
  if (!canSubmit.value || auth.loading) return
  const ok = await auth.login(userId.value.trim(), password.value)
  if (!ok) return
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/chat'
  await router.replace(redirect)
}

/** 证据链示意：与真实管线同序，末端落在一条带引用的结论上 */
const chain = [
  { label: '意图识别', note: '判断该直答还是检索' },
  { label: '规划', note: '拆解子问题与检索式' },
  { label: '网页 + 知识库', note: '双路并行召回' },
  { label: '证据裁判', note: '评分、去重、标记冲突' },
  { label: '成文', note: '正文逐句标注来源' },
]
</script>

<template>
  <div class="login-page">
    <section class="login-hero">
      <div class="hero-brand">
        <svg class="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
          <circle cx="16" cy="16" r="14" fill="none" stroke="currentColor" stroke-width="1.5" />
          <path d="M16 6v9l6 4" fill="none" stroke="currentColor" stroke-width="1.8"
                stroke-linecap="round" />
        </svg>
        <span class="brand-name">DeepResearch</span>
      </div>

      <h1 class="hero-title">每一条结论，都能追到它的来源</h1>
      <p class="hero-lede">
        多个智能体分头检索网页与本地知识库，交叉验证后成文，
        并在正文里标出每一处引用 —— 你可以顺着角标一路回溯到原文。
      </p>

      <ol class="hero-chain">
        <li v-for="(step, idx) in chain" :key="step.label">
          <span class="chain-node" aria-hidden="true">{{ idx + 1 }}</span>
          <span class="chain-body">
            <span class="chain-label">{{ step.label }}</span>
            <span class="chain-note">{{ step.note }}</span>
          </span>
        </li>
      </ol>

      <p class="hero-foot">
        示例引用：<span class="citation-ref">WEB1_2-3</span>
        <span class="hero-foot-note">指向第 1 轮检索、第 2 个查询、第 3 条结果</span>
      </p>
    </section>

    <section class="login-panel">
      <form class="login-card" @submit.prevent="submit">
        <h2 class="card-title">登录</h2>
        <p class="card-desc">使用部署方分配的账号进入工作台。</p>

        <div class="field">
          <label for="login-user">账号</label>
          <input
            id="login-user"
            v-model="userId"
            name="username"
            autocomplete="username"
            placeholder="例如 user01"
            :disabled="auth.loading"
          />
        </div>

        <div class="field">
          <label for="login-password">口令</label>
          <div class="password-wrap">
            <input
              id="login-password"
              v-model="password"
              :type="showPassword ? 'text' : 'password'"
              name="password"
              autocomplete="current-password"
              placeholder="请输入口令"
              :disabled="auth.loading"
            />
            <button
              type="button"
              class="password-toggle"
              :aria-label="showPassword ? '隐藏口令' : '显示口令'"
              @click="showPassword = !showPassword"
            >
              {{ showPassword ? '隐藏' : '显示' }}
            </button>
          </div>
        </div>

        <p v-if="auth.error" class="login-error" role="alert">{{ auth.error }}</p>

        <button type="submit" class="login-submit" :disabled="!canSubmit || auth.loading">
          {{ auth.loading ? '正在登录…' : '登录' }}
        </button>

        <p class="card-foot">
          账号与口令由部署方通过 <code>AUTH_USERS</code> 配置；登录后由服务端签发的令牌决定你的身份。
        </p>
      </form>
    </section>
  </div>
</template>

<style scoped>
.login-page {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
  min-height: 100dvh;
  background: var(--paper);
}

/* ── 左：主张 ── */
.login-hero {
  display: flex;
  flex-direction: column;
  gap: var(--sp-5);
  padding: var(--sp-10) var(--sp-8);
  background: var(--surface);
  border-right: 1px solid var(--line);
}

.hero-brand {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  color: var(--signal);
}

.brand-mark {
  width: 24px;
  height: 24px;
}

.brand-name {
  font-size: var(--fs-md);
  font-weight: 500;
  color: var(--ink);
  letter-spacing: -0.01em;
}

.hero-title {
  /* 中文标题用 em 限宽（ch 以拉丁字宽为基准，对中文会过窄），
     再用 text-wrap: balance 让折行两侧长度接近 */
  max-width: 14em;
  font-size: var(--fs-2xl);
  font-weight: 700;
  line-height: 1.3;
  letter-spacing: -0.02em;
  text-wrap: balance;
}

.hero-lede {
  max-width: 46ch;
  color: var(--ink-2);
  line-height: 1.75;
}

.hero-chain {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  margin: var(--sp-2) 0 0;
  padding: 0;
  list-style: none;
}

.hero-chain li {
  position: relative;
  display: flex;
  gap: var(--sp-3);
  padding-bottom: var(--sp-3);
}

.hero-chain li::before {
  content: '';
  position: absolute;
  left: 11px;
  top: 26px;
  bottom: 0;
  width: 1px;
  background: var(--line);
}

.hero-chain li:last-child {
  padding-bottom: 0;
}

.hero-chain li:last-child::before {
  display: none;
}

.chain-node {
  position: relative;
  z-index: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 23px;
  height: 23px;
  flex: 0 0 23px;
  border: 1px solid var(--line-strong);
  border-radius: 50%;
  background: var(--surface);
  color: var(--ink-3);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
}

.hero-chain li:last-child .chain-node {
  border-color: var(--evidence);
  background: var(--evidence-wash);
  color: var(--evidence-ink);
}

.chain-body {
  display: flex;
  flex-direction: column;
}

.chain-label {
  font-size: var(--fs-sm);
  font-weight: 500;
}

.chain-note {
  font-size: var(--fs-xs);
  color: var(--ink-3);
}

.hero-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  margin-top: auto;
  padding-top: var(--sp-5);
  border-top: 1px solid var(--line);
  font-size: var(--fs-sm);
  color: var(--ink-2);
}

.hero-foot-note {
  font-size: var(--fs-xs);
  color: var(--ink-3);
}

/* ── 右：表单 ── */
.login-panel {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-8) var(--sp-6);
}

.login-card {
  width: min(380px, 100%);
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}

.card-title {
  font-size: var(--fs-xl);
  font-weight: 700;
  letter-spacing: -0.01em;
}

.card-desc {
  font-size: var(--fs-sm);
  color: var(--ink-2);
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.field label {
  font-size: var(--fs-sm);
  font-weight: 500;
}

.field input {
  min-height: 42px;
  padding: 0 var(--sp-3);
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  background: var(--surface);
  transition: border-color var(--dur) var(--ease);
}

.field input:focus {
  border-color: var(--signal);
  outline: none;
}

.field input:disabled {
  opacity: 0.5;
}

.password-wrap {
  position: relative;
  display: flex;
  align-items: center;
}

.password-wrap input {
  width: 100%;
  padding-right: 56px;
}

.password-toggle {
  position: absolute;
  right: var(--sp-2);
  border: none;
  background: none;
  color: var(--ink-3);
  font-size: var(--fs-xs);
}

.login-error {
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm);
  background: var(--danger-wash);
  color: var(--danger);
  font-size: var(--fs-sm);
}

.login-submit {
  min-height: 44px;
  border: 1px solid var(--signal);
  border-radius: var(--r-md);
  background: var(--signal);
  color: #fff;
  font-size: var(--fs-md);
  font-weight: 500;
  transition: background var(--dur) var(--ease);
}

.login-submit:hover:not(:disabled) {
  background: var(--signal-ink);
}

.login-submit:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.card-foot {
  font-size: var(--fs-xs);
  color: var(--ink-3);
  line-height: 1.6;
}

.card-foot code {
  font-family: var(--font-mono);
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--surface-sunken);
}

@media (max-width: 900px) {
  .login-page {
    grid-template-columns: minmax(0, 1fr);
  }

  .login-hero {
    gap: var(--sp-4);
    padding: var(--sp-6) var(--sp-5);
    border-right: none;
    border-bottom: 1px solid var(--line);
  }

  .hero-title {
    font-size: var(--fs-xl);
  }

  .hero-chain,
  .hero-foot {
    display: none;
  }

  .login-panel {
    padding: var(--sp-6) var(--sp-5) var(--sp-10);
  }
}
</style>
