/**
 * T2.2 断线重连与续流 测试
 *
 * 覆盖用例:
 *   T2.2-01 网络错误触发重连
 *   T2.2-02 AbortError 不重连
 *   T2.2-03 用户已取消不重连
 *   T2.2-04 awaiting_input 状态停止重连
 *   T2.2-05 重连次数上限
 *   T2.2-07 消息对齐 replaceThreadMessages
 *   T2.2-08 sources.found 幂等去重
 *   T2.2-09 message.start 替换同 node 旧 streaming 消息
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useChatStore } from '../src/stores/chat'
import { useInterruptStore } from '../src/stores/interrupt'
import { useThreadsStore } from '../src/stores/threads'
import type { SourceItem } from '../src/types/events.gen'

// ── mock fetch ──────────────────────────────────────────
const mockFetch = vi.fn()
vi.stubGlobal('fetch', mockFetch)

beforeEach(() => {
  setActivePinia(createPinia())
  mockFetch.mockReset()
})

/** 创建 JSON Response mock */
function jsonResp(data: unknown): Response {
  return { ok: true, json: async () => data } as unknown as Response
}

/** 创建 SSE Response mock（使用简单字符串 body） */
function sseResp(frames: string[]): Response {
  const body = frames.join('\n\n') + '\n\n'
  const encoded = new TextEncoder().encode(body)
  return {
    ok: true,
    body: {
      getReader: () => {
        let done = false
        return {
          read: async () => {
            if (done) return { done: true, value: undefined }
            done = true
            return { done: false, value: encoded }
          },
          releaseLock: () => {},
        }
      },
    },
  } as unknown as Response
}

// ── T2.2-07: 消息对齐 replaceThreadMessages ─────────────

describe('T2.2-07: replaceThreadMessages', () => {
  it('整体替换消息列表，清除 streaming 中间态', () => {
    const chat = useChatStore()
    const threadId = 'test-replace'

    chat.ensureThread(threadId)
    chat.addUserMessage(threadId, '问题')
    chat.startAssistantMessage(threadId, 'msg-streaming')
    chat.appendDelta(threadId, 'msg-streaming', '半截文本')

    expect(chat.isRunning(threadId)).toBe(true)

    chat.replaceThreadMessages(threadId, [
      { role: 'user', content: '问题' },
      { role: 'assistant', content: '完整回答' },
    ])

    const msgs = chat.getMessages(threadId)
    expect(msgs.length).toBe(2)
    expect(msgs[0].role).toBe('user')
    expect(msgs[0].content).toBe('问题')
    expect(msgs[1].role).toBe('assistant')
    expect(msgs[1].content).toBe('完整回答')
    expect(msgs[1].status).toBe('done')
    expect(chat.isRunning(threadId)).toBe(false)
  })
})

// ── T2.2-08: sources.found 幂等去重 ─────────────────────

describe('T2.2-08: sources.found 幂等去重', () => {
  it('同一 url 的 web source 不重复追加', () => {
    const chat = useChatStore()
    const threadId = 'test-dedup-r22'

    chat.ensureThread(threadId)
    chat.startAssistantMessage(threadId, 'msg-src')

    chat.addSources(threadId, [
      { url: 'https://example.com/a', title: '来源A', snippet: '片段A', source_type: 'web' },
      { url: 'https://example.com/b', title: '来源B', snippet: '片段B', source_type: 'web' },
    ])

    chat.addSources(threadId, [
      { url: 'https://example.com/a', title: '来源A重复', snippet: '片段A2', source_type: 'web' },
      { url: 'https://example.com/c', title: '来源C', snippet: '片段C', source_type: 'web' },
    ])

    const msg = chat.getMessages(threadId).find((m) => m.id === 'msg-src')
    expect(msg).toBeDefined()
    expect(msg!.sources!.length).toBe(3)
  })

  it('同 chunk_id 的 kb source 不重复追加', () => {
    const chat = useChatStore()
    const threadId = 'test-dedup-kb'

    chat.ensureThread(threadId)
    chat.startAssistantMessage(threadId, 'msg-kb')

    chat.addSources(threadId, [
      { url: null, title: '文档1', snippet: '片段', source_type: 'kb', chunk_id: 'chunk-001' },
    ])

    chat.addSources(threadId, [
      { url: null, title: '文档1重复', snippet: '片段2', source_type: 'kb', chunk_id: 'chunk-001' },
      { url: null, title: '文档2', snippet: '片段3', source_type: 'kb', chunk_id: 'chunk-002' },
    ])

    const msg = chat.getMessages(threadId).find((m) => m.id === 'msg-kb')
    expect(msg!.sources!.length).toBe(2)
  })
})

// ── T2.2-09: message.start 替换同 node 旧 streaming 消息 ──

describe('T2.2-09: message.start 替换同 node 旧 streaming 消息', () => {
  it('同 node 旧 streaming 消息被移除，新消息插入', () => {
    const chat = useChatStore()
    const threadId = 'test-replace-msg'

    chat.ensureThread(threadId)
    chat.addUserMessage(threadId, '问题')
    chat.startAssistantMessage(threadId, 'msg-old', 'write')
    chat.appendDelta(threadId, 'msg-old', '半截文本')

    chat.startAssistantMessage(threadId, 'msg-new', 'write')

    const msgs = chat.getMessages(threadId)
    expect(msgs.find((m) => m.id === 'msg-old')).toBeUndefined()
    expect(msgs.find((m) => m.id === 'msg-new')).toBeDefined()
    expect(msgs.find((m) => m.id === 'msg-new')!.status).toBe('streaming')

    chat.appendDelta(threadId, 'msg-new', '新文本')
    expect(chat.getMessages(threadId).find((m) => m.id === 'msg-new')!.content).toBe('新文本')
  })

  it('不同 node 的 streaming 消息不被移除', () => {
    const chat = useChatStore()
    const threadId = 'test-keep-different-node'

    chat.ensureThread(threadId)
    chat.startAssistantMessage(threadId, 'msg-search', 'search')
    chat.startAssistantMessage(threadId, 'msg-write', 'write')

    const msgs = chat.getMessages(threadId)
    expect(msgs.find((m) => m.id === 'msg-search')).toBeDefined()
    expect(msgs.find((m) => m.id === 'msg-write')).toBeDefined()
  })
})

// ── T2.2-01: 网络错误触发重连 ──────────────────────────

describe('T2.2-01: 网络错误触发重连', () => {
  it('首次失败后第二次 resume 成功', async () => {
    const chat = useChatStore()
    const threads = useThreadsStore()
    const threadId = 'test-reconnect-01'

    chat.ensureThread(threadId)
    chat.addUserMessage(threadId, '测试')
    threads.currentThreadId = threadId
    vi.spyOn(threads, 'refresh').mockResolvedValue(undefined)

    let streamFailed = false

    mockFetch.mockImplementation(async (url: string, init?: RequestInit) => {
      const urlStr = String(url)

      // 首次 stream 请求失败
      if (urlStr.includes('/stream') && init?.method === 'POST' && !streamFailed) {
        streamFailed = true
        throw new TypeError('fetch failed')
      }

      // resume 请求成功返回 SSE
      if (urlStr.includes('/resume') && init?.method === 'POST') {
        return sseResp([
          'data: {"type":"run.completed","ts":1,"data":{"message_id":"msg-1","final_state":"done"}}',
        ])
      }

      // state 请求
      if (urlStr.includes('/state')) {
        return jsonResp({ thread_id: threadId, status: 'idle', resumable: true, next_nodes: ['write'] })
      }

      // messages 请求
      if (urlStr.includes('/messages')) {
        return jsonResp({ thread_id: threadId, messages: [{ role: 'user', content: '测试' }] })
      }

      return jsonResp({})
    })

    // mock setTimeout 使退避延迟为 0
    const originalSetTimeout = globalThis.setTimeout
    globalThis.setTimeout = ((fn: () => void) => { fn(); return 0 as never }) as typeof setTimeout

    const { useEventStream } = await import('../src/composables/useEventStream')
    const { run } = useEventStream()

    await run(threadId, '测试')

    globalThis.setTimeout = originalSetTimeout

    // 验证 resume 被调用
    const resumeCall = mockFetch.mock.calls.find(
      (c) => String(c[0]).includes('/resume'),
    )
    expect(resumeCall).toBeDefined()
    expect(chat.isReconnecting(threadId)).toBe(false)
  })
})

// ── T2.2-02: AbortError 不重连 ──────────────────────────

describe('T2.2-02: AbortError 不重连', () => {
  it('AbortError 不触发重连', async () => {
    const chat = useChatStore()
    const threads = useThreadsStore()
    const threadId = 'test-abort'

    chat.ensureThread(threadId)
    chat.addUserMessage(threadId, '测试')
    threads.currentThreadId = threadId
    vi.spyOn(threads, 'refresh').mockResolvedValue(undefined)

    let callCount = 0
    mockFetch.mockImplementation(async () => {
      callCount++
      const err = new Error('Aborted')
      err.name = 'AbortError'
      throw err
    })

    const originalSetTimeout = globalThis.setTimeout
    globalThis.setTimeout = ((fn: () => void) => { fn(); return 0 as never }) as typeof setTimeout

    const { useEventStream } = await import('../src/composables/useEventStream')
    const { run } = useEventStream()
    await run(threadId, '测试')

    globalThis.setTimeout = originalSetTimeout
    expect(callCount).toBe(1)
  })
})

// ── T2.2-03: 用户已取消不重连 ──────────────────────────

describe('T2.2-03: 用户已取消不重连', () => {
  it('chat store 已置 cancelled 状态时不重连', async () => {
    const chat = useChatStore()
    const threads = useThreadsStore()
    const threadId = 'test-cancelled'

    chat.ensureThread(threadId)
    chat.addUserMessage(threadId, '测试')
    chat.startAssistantMessage(threadId, 'msg-1')
    chat.markCancelled(threadId)
    threads.currentThreadId = threadId
    vi.spyOn(threads, 'refresh').mockResolvedValue(undefined)

    let callCount = 0
    mockFetch.mockImplementation(async () => {
      callCount++
      throw new TypeError('fetch failed')
    })

    const originalSetTimeout = globalThis.setTimeout
    globalThis.setTimeout = ((fn: () => void) => { fn(); return 0 as never }) as typeof setTimeout

    const { useEventStream } = await import('../src/composables/useEventStream')
    const { run } = useEventStream()
    await run(threadId, '测试')

    globalThis.setTimeout = originalSetTimeout
    expect(callCount).toBe(1)
  })
})

// ── T2.2-04: awaiting_input 状态停止重连 ─────────────────

describe('T2.2-04: awaiting_input 状态停止重连', () => {
  it('state 返回 awaiting_input 时停止重连', async () => {
    const chat = useChatStore()
    const threads = useThreadsStore()
    const threadId = 'test-awaiting'

    chat.ensureThread(threadId)
    chat.addUserMessage(threadId, '测试')
    threads.currentThreadId = threadId
    vi.spyOn(threads, 'refresh').mockResolvedValue(undefined)

    mockFetch.mockImplementation(async (url: string) => {
      const urlStr = String(url)
      if (urlStr.includes('/stream')) throw new TypeError('fetch failed')
      if (urlStr.includes('/state')) return jsonResp({ thread_id: threadId, status: 'awaiting_input', resumable: true, next_nodes: [] })
      return jsonResp({})
    })

    const originalSetTimeout = globalThis.setTimeout
    globalThis.setTimeout = ((fn: () => void) => { fn(); return 0 as never }) as typeof setTimeout

    const { useEventStream } = await import('../src/composables/useEventStream')
    const { run } = useEventStream()
    await run(threadId, '测试')

    globalThis.setTimeout = originalSetTimeout

    const resumeCall = mockFetch.mock.calls.find((c) => String(c[0]).includes('/resume'))
    expect(resumeCall).toBeUndefined()
    expect(chat.isReconnecting(threadId)).toBe(false)
  })
})

// ── T2.2-05: 重连次数上限 ───────────────────────────────

describe('T2.2-05: 重连次数上限', () => {
  it('连续失败最终调用 markError 并复位 reconnecting', async () => {
    const chat = useChatStore()
    const threads = useThreadsStore()
    const threadId = 'test-max-attempts'

    chat.ensureThread(threadId)
    chat.addUserMessage(threadId, '测试')
    chat.startAssistantMessage(threadId, 'msg-stream')
    threads.currentThreadId = threadId
    vi.spyOn(threads, 'refresh').mockResolvedValue(undefined)

    mockFetch.mockImplementation(async (url: string) => {
      const urlStr = String(url)

      if (urlStr.includes('/stream') || urlStr.includes('/resume')) {
        throw new TypeError('fetch failed')
      }
      if (urlStr.includes('/state')) {
        return jsonResp({ thread_id: threadId, status: 'idle', resumable: true, next_nodes: ['write'] })
      }
      if (urlStr.includes('/messages')) {
        return jsonResp({ thread_id: threadId, messages: [] })
      }
      return jsonResp({})
    })

    // mock setTimeout 使退避延迟为 0
    const originalSetTimeout = globalThis.setTimeout
    globalThis.setTimeout = ((fn: () => void) => { fn(); return 0 as never }) as typeof setTimeout

    const { useEventStream } = await import('../src/composables/useEventStream')
    const { run } = useEventStream()

    await run(threadId, '测试')

    globalThis.setTimeout = originalSetTimeout

    expect(chat.getError(threadId)).toBeTruthy()
    expect(chat.isReconnecting(threadId)).toBe(false)
  })
})

// ── 辅助：setReconnecting / isReconnecting ───────────────

describe('setReconnecting / isReconnecting', () => {
  it('设置和读取重连状态', () => {
    const chat = useChatStore()
    const threadId = 'test-rc-state'

    chat.ensureThread(threadId)
    expect(chat.isReconnecting(threadId)).toBe(false)

    chat.setReconnecting(threadId, true)
    expect(chat.isReconnecting(threadId)).toBe(true)

    chat.setReconnecting(threadId, false)
    expect(chat.isReconnecting(threadId)).toBe(false)
  })
})
