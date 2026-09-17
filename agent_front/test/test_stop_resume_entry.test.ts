/**
 * 停止态的两个显式动作入口。
 *
 * 停止后的下一步有两种截然不同的归宿，靠关键词猜意图存在已知误判
 * （「继续调研AI」既像续流又像新任务）。因此在停止态输入框给出两个
 * 明确入口：
 *   - 补充条件继续研究 → POST /resume { mode: 'modify', resume_value: 条件 }
 *   - 换个主题重新研究 → 开新 thread 走 POST /stream
 *
 * 关键词分流（方案 A）保留，两者互补；本文件同时覆盖 A 的回归。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { useChatStore } from '../src/stores/chat'
import { useThreadsStore } from '../src/stores/threads'
import { useEventStream } from '../src/composables/useEventStream'
import Composer from '../src/components/chat/Composer.vue'

// ── mock fetch ──────────────────────────────────────────
const mockFetch = vi.fn()
vi.stubGlobal('fetch', mockFetch)

const THREAD_ID = 'thread_original'

function jsonResp(data: unknown): Response {
  return { ok: true, status: 200, json: async () => data } as unknown as Response
}

function sseResp(): Response {
  const encoded = new TextEncoder().encode('')
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () => ({ done: true, value: undefined }),
        releaseLock: () => {},
      }),
    },
  } as unknown as Response
}

/** 状态响应：resumable 决定 checkpoint 能不能续 */
function stateResp(resumable: boolean, status = 'idle'): Response {
  return jsonResp({
    thread_id: THREAD_ID,
    status,
    resumable,
    next_nodes: [],
    values: {},
    interrupts: [],
    has_checkpoint: resumable,
    interrupted_by_restart: false,
    current_node: '',
    created_at: null,
    parent_config: null,
  })
}

function installFetchRouter(resumable: boolean, status = 'idle') {
  mockFetch.mockImplementation(async (url: string) => {
    const target = String(url)
    if (target.includes('/state')) return stateResp(resumable, status)
    if (target.includes('/messages')) return jsonResp({ thread_id: THREAD_ID, messages: [] })
    if (target.includes('/resume') || target.includes('/stream')) return sseResp()
    return jsonResp({})
  })
}

/** 取出某次请求的 body（JSON 已解析） */
function bodyOf(urlFragment: string, method: string): Record<string, unknown> | undefined {
  const call = mockFetch.mock.calls.find(([url, init]) => {
    const target = String(url)
    return target.includes(urlFragment) && ((init as RequestInit)?.method || 'GET') === method
  })
  if (!call) return undefined
  return JSON.parse(String((call[1] as RequestInit).body))
}

beforeEach(() => {
  setActivePinia(createPinia())
  mockFetch.mockReset()
})

// ── 入口一：补充条件继续研究 ────────────────────────────

describe('停止态入口：补充条件继续研究', () => {
  it('以 mode=modify 提交补充条件', async () => {
    installFetchRouter(true)
    const chat = useChatStore()
    const threads = useThreadsStore()
    threads.selectThread(THREAD_ID)
    chat.setUserStopped(THREAD_ID, true)

    const { resumeWithCondition } = useEventStream()
    await resumeWithCondition(THREAD_ID, '再加上 2025 年的对比数据')

    const body = bodyOf('/resume', 'POST')
    expect(body?.mode).toBe('modify')
    expect(body?.resume_value).toBe('再加上 2025 年的对比数据')
    expect(body?.thread_id).toBe(THREAD_ID)
  })

  it('提交后清除停止态标记', async () => {
    installFetchRouter(true)
    const chat = useChatStore()
    const threads = useThreadsStore()
    threads.selectThread(THREAD_ID)
    chat.setUserStopped(THREAD_ID, true)

    const { resumeWithCondition } = useEventStream()
    await resumeWithCondition(THREAD_ID, '补充条件')

    expect(chat.isUserStopped(THREAD_ID)).toBe(false)
  })

  it('checkpoint 不可续时退回新研究，不丢用户输入', async () => {
    installFetchRouter(false)
    const chat = useChatStore()
    const threads = useThreadsStore()
    threads.selectThread(THREAD_ID)
    chat.setUserStopped(THREAD_ID, true)

    const { resumeWithCondition } = useEventStream()
    await resumeWithCondition(THREAD_ID, '用户输入不能丢')

    expect(bodyOf('/resume', 'POST')).toBeUndefined()
    expect(bodyOf('/stream', 'POST')?.query).toBe('用户输入不能丢')
  })
})

// ── 入口二：换个主题重新研究 ────────────────────────────

describe('停止态入口：换个主题重新研究', () => {
  it('开新 thread 走 /stream，而不是复用当前 thread', async () => {
    installFetchRouter(true)
    const chat = useChatStore()
    const threads = useThreadsStore()
    threads.selectThread(THREAD_ID)
    chat.setUserStopped(THREAD_ID, true)

    const { startNewTopic } = useEventStream()
    await startNewTopic('换一个完全不同的主题')

    const body = bodyOf('/stream', 'POST')
    expect(body?.query).toBe('换一个完全不同的主题')
    // 关键：必须换 thread —— 复用会把上一次研究的证据混进新主题
    expect(body?.thread_id).not.toBe(THREAD_ID)
    expect(String(body?.thread_id)).toMatch(/^thread_/)
  })

  it('旧 thread 的消息与 checkpoint 保持不动', async () => {
    installFetchRouter(true)
    const chat = useChatStore()
    const threads = useThreadsStore()
    threads.selectThread(THREAD_ID)
    chat.addUserMessage(THREAD_ID, '上一次研究的问题')
    chat.setUserStopped(THREAD_ID, true)

    const { startNewTopic } = useEventStream()
    await startNewTopic('新主题')

    const oldMessages = chat.getMessages(THREAD_ID)
    expect(oldMessages.some((m) => m.content === '上一次研究的问题')).toBe(true)
    expect(chat.isUserStopped(THREAD_ID)).toBe(true)
  })
})

// ── 方案 A 回归：关键词分流 ─────────────────────────────

describe('关键词分流回归（方案 A 仍在）', () => {
  it('停止后输入「继续」→ mode=continue', async () => {
    installFetchRouter(true)
    const chat = useChatStore()
    const threads = useThreadsStore()
    threads.selectThread(THREAD_ID)
    chat.setUserStopped(THREAD_ID, true)

    const { runOrResume } = useEventStream()
    await runOrResume(THREAD_ID, '继续')

    expect(bodyOf('/resume', 'POST')?.mode).toBe('continue')
  })

  it('停止后输入新条件 → mode=modify', async () => {
    installFetchRouter(true)
    const chat = useChatStore()
    const threads = useThreadsStore()
    threads.selectThread(THREAD_ID)
    chat.setUserStopped(THREAD_ID, true)

    const { runOrResume } = useEventStream()
    await runOrResume(THREAD_ID, '换成 2025 年的数据')

    expect(bodyOf('/resume', 'POST')?.mode).toBe('modify')
  })

  it('非停止态 → 走 /stream 新任务', async () => {
    installFetchRouter(true)
    const chat = useChatStore()
    const threads = useThreadsStore()
    threads.selectThread(THREAD_ID)

    const { runOrResume } = useEventStream()
    await runOrResume(THREAD_ID, '一个全新的问题')

    expect(bodyOf('/resume', 'POST')).toBeUndefined()
    expect(bodyOf('/stream', 'POST')?.query).toBe('一个全新的问题')
  })
})

// ── 输入框渲染 ──────────────────────────────────────────

describe('Composer 停止态渲染', () => {
  function mountComposer(props: Record<string, unknown> = {}) {
    return mount(Composer, {
      props: { disabled: false, loading: false, ...props },
    })
  }

  it('停止态给出两个动作入口', () => {
    const wrapper = mountComposer({ stopped: true })
    const labels = wrapper.findAll('.stop-action-btn').map((b) => b.text())

    expect(labels).toEqual(['补充条件继续研究', '换个主题重新研究'])
  })

  it('非停止态不出现动作入口', () => {
    const wrapper = mountComposer({ stopped: false })

    expect(wrapper.findAll('.stop-action-btn')).toHaveLength(0)
  })

  it('研究进行中不出现动作入口', () => {
    const wrapper = mountComposer({ stopped: true, loading: true })

    expect(wrapper.findAll('.stop-action-btn')).toHaveLength(0)
  })

  it('空输入时两个入口都禁用', () => {
    const wrapper = mountComposer({ stopped: true })

    for (const button of wrapper.findAll('.stop-action-btn')) {
      expect(button.attributes('disabled')).toBeDefined()
    }
  })

  it('填入内容后「补充条件继续研究」发出对应事件与文本', async () => {
    const wrapper = mountComposer({ stopped: true })
    await wrapper.find('textarea').setValue('  再加上政策部分  ')

    await wrapper.findAll('.stop-action-btn')[0]!.trigger('click')

    expect(wrapper.emitted('continue-with-condition')).toEqual([['再加上政策部分']])
  })

  it('填入内容后「换个主题重新研究」发出对应事件与文本', async () => {
    const wrapper = mountComposer({ stopped: true })
    await wrapper.find('textarea').setValue('换个主题')

    await wrapper.findAll('.stop-action-btn')[1]!.trigger('click')

    expect(wrapper.emitted('restart-topic')).toEqual([['换个主题']])
  })

  it('点击动作入口后清空输入框', async () => {
    const wrapper = mountComposer({ stopped: true })
    await wrapper.find('textarea').setValue('换个主题')

    await wrapper.findAll('.stop-action-btn')[1]!.trigger('click')

    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('')
  })
})
