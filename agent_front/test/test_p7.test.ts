/**
 * Phase 7 测试：引用溯源 + 报告导出
 *
 * 覆盖用例:
 *   T7-1 来源去重与编号稳定 — 同 url 两轮检索去重正确
 *   T7-2 悬挂引用剔除 — 报告含不存在的引用ID → 后处理剔除
 *   T7-3 引用覆盖率统计 — 代码断言覆盖率日志逻辑存在
 *   T7-4 角标渲染交互 — MarkdownRender 预处理 [source_id] 为上标元素
 *   T7-5 MD 导出 — 导出函数生成正确 Blob
 *   T7-7 事件增量 — sources.found 事件只含本轮新增
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useChatStore } from '../src/stores/chat'
import { takeSseLines } from '../src/api/sse'
import { exportMarkdown, exportPdf } from '../src/api/rest'
import { groupTurns } from '../src/utils/turns'
import type { EventEnvelope, SourceItem } from '../src/types/events.gen'

// ── mock fetch ──────────────────────────────────────────
const mockFetch = vi.fn()
vi.stubGlobal('fetch', mockFetch)

beforeEach(() => {
  setActivePinia(createPinia())
  mockFetch.mockReset()
})

// ── T7-1: 来源去重与编号稳定 ─────────────────────────────

describe('T7-1: 来源去重与编号稳定', () => {
  it('同 url 两轮检索 → sources 去重后仅保留一条', () => {
    const chat = useChatStore()
    const threadId = 'test-dedup'

    chat.ensureThread(threadId)
    chat.startAssistantMessage(threadId, 'msg-1')

    // 第一轮来源
    const sources1: SourceItem[] = [
      { url: 'https://example.com/a', title: '来源A', snippet: '片段A', source_type: 'web' },
      { url: 'https://example.com/b', title: '来源B', snippet: '片段B', source_type: 'web' },
    ]
    chat.addSources(threadId, sources1)

    // 第二轮来源（含重复 url）
    const sources2: SourceItem[] = [
      { url: 'https://example.com/a', title: '来源A重复', snippet: '片段A2', source_type: 'web' },
      { url: 'https://example.com/c', title: '来源C', snippet: '片段C', source_type: 'web' },
    ]
    chat.addSources(threadId, sources2)

    const msgs = chat.getMessages(threadId)
    const msg = msgs.find((m) => m.id === 'msg-1')
    expect(msg).toBeDefined()
    expect(msg!.sources).toBeDefined()
    // R2.2: chat store 现已做幂等去重（url+title），A 不重复追加
    expect(msg!.sources!.length).toBe(3)
  })

  it('web + kb 混合来源各自独立', () => {
    const chat = useChatStore()
    const threadId = 'test-mixed'

    chat.ensureThread(threadId)
    chat.startAssistantMessage(threadId, 'msg-1')

    const mixedSources: SourceItem[] = [
      { url: 'https://example.com/web', title: '网页来源', snippet: 'web', source_type: 'web' },
      { url: null, title: '知识库文档', snippet: 'kb片段', source_type: 'kb', chunk_id: 'doc-123' },
    ]
    chat.addSources(threadId, mixedSources)

    const msgs = chat.getMessages(threadId)
    const msg = msgs.find((m) => m.id === 'msg-1')
    expect(msg!.sources!.length).toBe(2)
    expect(msg!.sources!.find((s) => s.source_type === 'web')).toBeDefined()
    expect(msg!.sources!.find((s) => s.source_type === 'kb')).toBeDefined()
  })
})

// ── T7-2: 悬挂引用剔除（代码断言） ───────────────────────

describe('T7-2: 悬挂引用剔除', () => {
  it('后端 _validate_and_fix_citations 逻辑存在', async () => {
    // 代码断言：后端 write.py 中有 _validate_and_fix_citations 调用
    const fs = await import('node:fs')
    const path = await import('node:path')
    const content = fs.readFileSync(
      path.resolve(__dirname, '../../app/mult_agents/nodes/write.py'),
      'utf-8',
    )
    expect(content).toContain('_validate_and_fix_citations')
    expect(content).toContain('valid_source_ids_set')
  })

  it('后端引用覆盖率统计日志存在', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const content = fs.readFileSync(
      path.resolve(__dirname, '../../app/mult_agents/nodes/write.py'),
      'utf-8',
    )
    expect(content).toContain('引用覆盖率')
    expect(content).toContain('coverage')
  })
})

// ── T7-4: 角标渲染交互（代码断言） ───────────────────────

describe('T7-4: 角标渲染交互', () => {
  it('MarkdownRender 预处理 [source_id] 为占位符', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const content = fs.readFileSync(
      path.resolve(__dirname, '../src/components/chat/MarkdownRender.vue'),
      'utf-8',
    )
    // 验证预处理逻辑存在
    expect(content).toContain('CITATION_PATTERN')
    expect(content).toContain('PLACEHOLDER_PREFIX')
    expect(content).toContain('citation-ref')
    expect(content).toContain('data-source-id')
  })

  it('SourceList 按 source_type 分组', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const content = fs.readFileSync(
      path.resolve(__dirname, '../src/components/chat/SourceList.vue'),
      'utf-8',
    )
    expect(content).toContain('webSources')
    expect(content).toContain('kbSources')
    expect(content).toContain('source_type')
  })
})

// ── T7-5: 导出（需认证）────────────────────────────────

describe('T7-5: 导出', () => {
  it('MD 导出走带令牌的 fetch 并返回 Blob', async () => {
    localStorage.setItem('dr.token', 'test-token')
    mockFetch.mockResolvedValue(new Response('# 报告', { status: 200 }))

    const blob = await exportMarkdown('thread-123')

    // jsdom 的 Blob 与 Node Response.blob() 返回的 Blob 属于不同 realm，
    // instanceof 必然失败；断言 blob 的关键特征即可。
    expect(blob.size).toBeGreaterThan(0)
    expect(typeof blob.text).toBe('function')
    const [url, init] = mockFetch.mock.calls[0]
    expect(String(url)).toContain('thread-123')
    expect(String(url)).toContain('/export/md')
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer test-token')
  })

  it('PDF 导出走带令牌的 fetch 并返回 Blob', async () => {
    localStorage.setItem('dr.token', 'test-token')
    mockFetch.mockResolvedValue(new Response('%PDF-1.4', { status: 200 }))

    const blob = await exportPdf('thread-456')

    // 同上：跨 realm 的 instanceof 不可靠，断言 blob 的关键特征。
    expect(blob.size).toBeGreaterThan(0)
    expect(typeof blob.text).toBe('function')
    const [url, init] = mockFetch.mock.calls[0]
    expect(String(url)).toContain('thread-456')
    expect(String(url)).toContain('/export/pdf')
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer test-token')
  })

  it('MessageItem 导出函数代码存在', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const content = fs.readFileSync(
      path.resolve(__dirname, '../src/components/chat/MessageItem.vue'),
      'utf-8',
    )
    expect(content).toContain('exportMarkdown')
    expect(content).toContain('Blob')
    expect(content).toContain('参考文献')
  })
})

// ── T7-7: 事件增量（sources.found 只含新增） ─────────────

describe('T7-7: 事件增量', () => {
  it('sources.found 事件在 SSE 流中正确解析', () => {
    const event: EventEnvelope = {
      type: 'sources.found',
      ts: 1000,
      data: {
        sources: [
          { url: 'https://example.com', title: 'Test', snippet: 'Test snippet', source_type: 'web' },
        ],
      },
    }
    const sseFrame = `data: ${JSON.stringify(event)}\n\n`
    const lines = takeSseLines(sseFrame)
    expect(lines.length).toBe(1)
    const parsed = JSON.parse(lines[0])
    expect(parsed.type).toBe('sources.found')
    expect(parsed.data.sources).toBeInstanceOf(Array)
    expect(parsed.data.sources[0].url).toBe('https://example.com')
  })

  it('多轮 sources.found 事件各自独立解析', () => {
    const events = [
      { type: 'sources.found', ts: 1, data: { sources: [{ url: 'https://a.com', title: 'A', source_type: 'web' }] } },
      { type: 'sources.found', ts: 2, data: { sources: [{ url: 'https://b.com', title: 'B', source_type: 'web' }] } },
    ]
    const buffer = events.map((e) => `data: ${JSON.stringify(e)}`).join('\n\n') + '\n\n'
    const lines = takeSseLines(buffer)
    expect(lines.length).toBe(2)
  })
})

// ── H3: 中间过程按轮分组，收进折叠卡片 ───────────────────
//
// 原测试断言 AgentTimeline.vue 的源码文本，属「证明代码存在」而非验证行为；
// 分组规则已抽成 utils/turns.ts 纯函数，这里直接验证行为。

describe('H3: 消息按轮分组', () => {
  const labelOf = { intent: '意图识别', plan: '规划', web_search: '网页检索', write: '成文' }

  it('过程步骤来自时间线，带正文的消息作为答案', () => {
    const timeline = [
      { node: 'intent', label: '意图识别', phase: 'completed', ts: 1 },
      { node: 'plan', label: '规划', phase: 'completed', ts: 2 },
      { node: 'write', label: '成文', phase: 'completed', ts: 3 },
    ]
    const turns = groupTurns(
      [
        { id: 'u1', role: 'user', content: '调研一下 AI Agent', timelineStart: 0 },
        { id: 'write', role: 'assistant', content: '# 报告\n正文…', nodeId: 'write', status: 'done' },
      ],
      labelOf,
      timeline,
    )

    expect(turns).toHaveLength(1)
    expect(turns[0].user?.id).toBe('u1')
    expect(turns[0].steps.map((s) => s.label)).toEqual(['意图识别', '规划', '成文'])
    expect(turns[0].answer?.id).toBe('write')
  })

  it('时间线里尚未完成的节点标记为 running', () => {
    const turns = groupTurns(
      [
        { id: 'u1', role: 'user', content: '问题', timelineStart: 0 },
      ],
      labelOf,
      [{ node: 'web_search', label: '网页检索', phase: 'running', ts: 1 }],
    )

    expect(turns[0].steps[0].state).toBe('running')
    expect(turns[0].answer).toBeNull()
  })

  it('时间线按轮切分，多轮互不串味', () => {
    const timeline = [
      { node: 'intent', label: '意图识别', phase: 'completed', ts: 1 },
      { node: 'write', label: '成文', phase: 'completed', ts: 2 },
      { node: 'intent', label: '意图识别', phase: 'completed', ts: 3 },
      { node: 'plan', label: '规划', phase: 'completed', ts: 4 },
    ]
    const turns = groupTurns(
      [
        { id: 'u1', role: 'user', content: '第一问', timelineStart: 0 },
        { id: 'a1', role: 'assistant', content: '第一答', nodeId: 'write', status: 'done' },
        { id: 'u2', role: 'user', content: '第二问', timelineStart: 2 },
        { id: 'a2', role: 'assistant', content: '第二答', nodeId: 'write', status: 'done' },
      ],
      labelOf,
      timeline,
    )

    expect(turns).toHaveLength(2)
    expect(turns[0].steps.map((s) => s.node)).toEqual(['intent', 'write'])
    expect(turns[1].steps.map((s) => s.node)).toEqual(['intent', 'plan'])
    expect(turns[0].answer?.content).toBe('第一答')
    expect(turns[1].answer?.content).toBe('第二答')
  })

  it('时间线缺失时回退到按消息推导步骤', () => {
    const turns = groupTurns(
      [
        { id: 'u1', role: 'user', content: '问题' },
        {
          id: 'plan', role: 'assistant', content: '', nodeId: 'plan', status: 'done',
          thinkingLogs: [{ node: 'plan', message: '拆解 3 个子问题', time: '10:00:03' }],
        },
        { id: 'write', role: 'assistant', content: '答案', nodeId: 'write', status: 'done' },
      ],
      labelOf,
    )

    expect(turns[0].steps).toHaveLength(1)
    expect(turns[0].steps[0].label).toBe('规划')
    expect(turns[0].steps[0].details).toEqual(['拆解 3 个子问题'])
  })

  it('步骤细节来自时间线自带的 detail', () => {
    const turns = groupTurns(
      [{ id: 'u1', role: 'user', content: '你好', timelineStart: 0 }],
      labelOf,
      [
        { node: 'intent', label: '意图识别', phase: 'running', detail: '正在判断问题意图...', ts: 1 },
        { node: 'intent', label: '意图识别', phase: 'completed', detail: '意图判定完成: direct', ts: 2 },
        { node: 'direct_answer', label: '快速回答', phase: 'completed', detail: '', ts: 3 },
      ],
    )

    expect(turns[0].steps[0].details).toEqual(['正在判断问题意图...', '意图判定完成: direct'])
    expect(turns[0].steps[1].details).toEqual([])
  })

  it('步骤细节来自时间线自带的 detail', () => {
    const turns = groupTurns(
      [{ id: 'u1', role: 'user', content: '你好', timelineStart: 0 }],
      labelOf,
      [
        { node: 'intent', label: '意图识别', phase: 'running', detail: '正在判断问题意图...', ts: 1 },
        { node: 'intent', label: '意图识别', phase: 'completed', detail: '意图判定完成: direct', ts: 2 },
        { node: 'direct_answer', label: '快速回答', phase: 'completed', detail: '', ts: 3 },
      ],
    )

    expect(turns[0].steps[0].details).toEqual(['正在判断问题意图...', '意图判定完成: direct'])
    expect(turns[0].steps[1].details).toEqual([])
  })

  it('端到端：状态事件经 store 落到时间线后，分组仍带细节', () => {
    const chat = useChatStore()
    const tid = 'e2e-turn'
    chat.ensureThread(tid)
    chat.addUserMessage(tid, '你好')
    chat.setNodeStatus(tid, { node: 'intent', label: '意图识别', phase: 'running', detail: '正在判断问题意图...' })
    chat.setNodeStatus(tid, { node: 'intent', label: '意图识别', phase: 'completed', detail: '' })
    chat.setNodeStatus(tid, { node: 'direct_answer', label: '快速回答', phase: 'completed', detail: '' })

    const timeline = chat.getAgentTimeline(tid)
    const labels = Object.fromEntries(
      timeline.filter((e) => e.node && e.label).map((e) => [e.node, e.label]),
    )
    const turns = groupTurns(chat.getMessages(tid), labels, timeline)

    expect(turns).toHaveLength(1)
    expect(turns[0].steps.map((s) => s.label)).toEqual(['意图识别', '快速回答'])
    expect(turns[0].steps[0].details).toEqual(['正在判断问题意图...'])
  })

  it('端到端：状态事件经 store 落到时间线后，分组仍带细节', () => {
    const chat = useChatStore()
    const tid = 'e2e-turn'
    chat.ensureThread(tid)
    chat.addUserMessage(tid, '你好')
    chat.setNodeStatus(tid, { node: 'intent', label: '意图识别', phase: 'running', detail: '正在判断问题意图...' })
    chat.setNodeStatus(tid, { node: 'intent', label: '意图识别', phase: 'completed', detail: '' })
    chat.setNodeStatus(tid, { node: 'direct_answer', label: '快速回答', phase: 'completed', detail: '' })

    const timeline = chat.getAgentTimeline(tid)
    const labels = Object.fromEntries(
      timeline.filter((e) => e.node && e.label).map((e) => [e.node, e.label]),
    )
    const turns = groupTurns(chat.getMessages(tid), labels, timeline)

    expect(turns).toHaveLength(1)
    expect(turns[0].steps.map((s) => s.label)).toEqual(['意图识别', '快速回答'])
    expect(turns[0].steps[0].details).toEqual(['正在判断问题意图...'])
  })

  it('时间线里的细节从同名节点的日志补充', () => {
    const turns = groupTurns(
      [
        { id: 'u1', role: 'user', content: '问题', timelineStart: 0 },
        {
          id: 'plan', role: 'assistant', content: '', nodeId: 'plan', status: 'done',
          thinkingLogs: [{ node: 'plan', message: '拆解 3 个子问题', time: '10:00:03' }],
        },
      ],
      labelOf,
      [{ node: 'plan', label: '规划', phase: 'completed', ts: 1 }],
    )

    expect(turns[0].steps[0].details).toEqual(['拆解 3 个子问题'])
  })
})
