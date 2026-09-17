/**
 * 导出被质量门禁拦下时，用户必须看到具体问题。
 *
 * 后端返回 422 + 问题清单，前端若只走 chat.markError 显示一句「导出失败」，
 * 用户就无从知道报告哪里有问题、该怎么改 —— 门禁的提示价值全部丢失。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import ChatView from '../src/views/ChatView.vue'

const mockFetch = vi.fn()
vi.stubGlobal('fetch', mockFetch)

const ISSUES = [
  {
    severity: 'error',
    code: 'missing_reference_section',
    message: '正文含引用标记但缺少参考资料段落，引用无法核对',
  },
]

function jsonResponse(body: unknown, status: number) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

async function mountChatView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: { template: '<div />' } }],
  })
  await router.push('/')
  await router.isReady()
  return mount(ChatView, { global: { plugins: [router] } })
}

beforeEach(() => {
  setActivePinia(createPinia())
  mockFetch.mockReset()
  localStorage.setItem('dr.token', 'test-token')
})

describe('导出质量门禁的用户可见提示', () => {
  it('422 时列出具体问题，而不是只显示「导出失败」', async () => {
    mockFetch.mockImplementation(async (url: string) => {
      const target = String(url)
      if (target.includes('/export/pdf')) {
        return jsonResponse({ detail: { message: '报告未通过导出前检查', issues: ISSUES } }, 422)
      }
      return jsonResponse({ thread_id: 't', messages: [] }, 200)
    })

    const wrapper = await mountChatView()
    await wrapper.vm.$nextTick()

    // 触发导出
    await (wrapper.vm as unknown as { exportPdfReport: () => Promise<void> }).exportPdfReport()
    await wrapper.vm.$nextTick()

    const text = wrapper.text()
    expect(text).toContain('报告未通过导出前检查')
    expect(text).toContain(ISSUES[0]!.message)
  })

  it('普通失败（无问题清单）不展示门禁提示，仍走原有错误态', async () => {
    mockFetch.mockImplementation(async (url: string) => {
      const target = String(url)
      if (target.includes('/export/pdf')) {
        return new Response('boom', { status: 500 })
      }
      return jsonResponse({ thread_id: 't', messages: [] }, 200)
    })

    const wrapper = await mountChatView()
    await wrapper.vm.$nextTick()

    await (wrapper.vm as unknown as { exportPdfReport: () => Promise<void> }).exportPdfReport()
    await wrapper.vm.$nextTick()

    // 没有 issues 时不该出现门禁标题，否则这条断言无法区分两条分支
    expect(wrapper.text()).not.toContain('报告未通过导出前检查')
  })
})
