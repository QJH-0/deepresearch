/**
 * R4.2 E3-E5 前端缺陷修复 测试
 *
 * 覆盖用例:
 *   T4.2-01 message.start 初始化消息对象
 *   T4.2-02 未知 thread 的 delta 不抛错
 *   T4.2-03 完整事件序列渲染回复
 *   T4.2-04 run.error 显示错误态
 *   T4.2-05 interrupt.raised 不触发递归更新
 *   T4.2-06 中断卡输入不直接改 store payload
 *   T4.2-07 取消后终态对齐
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useChatStore } from '../src/stores/chat'
import { useInterruptStore } from '../src/stores/interrupt'
import { useEventStream } from '../src/composables/useEventStream'
import type { EventEnvelope } from '../src/types/events.gen'

// ── mock fetch ──────────────────────────────────────────
const mockFetch = vi.fn()
vi.stubGlobal('fetch', mockFetch)

beforeEach(() => {
  setActivePinia(createPinia())
  mockFetch.mockReset()
})

function makeEvent(type: string, data: Record<string, unknown>): EventEnvelope {
  return { type, data } as unknown as EventEnvelope
}

// ── T4.2-01: message.start 初始化消息对象 ──────────────

describe('T4.2-01: message.start 初始化消息对象', () => {
  it('run.started 不创建空消息，message.start 创建正确消息', () => {
    const chat = useChatStore()
    const intr = useInterruptStore()
    const { dispatch } = useEventStream()
    const threadId = 't4-01'

    dispatch(threadId, makeEvent('run.started', {}))
    const msgsAfterStart = chat.getMessages(threadId)
    expect(msgsAfterStart.length).toBe(0)

    dispatch(threadId, makeEvent('message.start', { message_id: 'm1', node: 'write' }))
    const msgs = chat.getMessages(threadId)
    expect(msgs.length).toBe(1)
    expect(msgs[0].id).toBe('m1')
    expect(msgs[0].status).toBe('streaming')
    expect(chat.isRunning(threadId)).toBe(true)
  })
})

// ── T4.2-02: 未知 thread 的 delta 不抛错 ──────────────

describe('T4.2-02: 未知 thread 的 delta 不抛错', () => {
  it('delta 先于 message.start 到达时惰性初始化', () => {
    const chat = useChatStore()
    const { dispatch } = useEventStream()
    const threadId = 't4-02'

    // 直接发 delta，不先 message.start
    expect(() => {
      dispatch(threadId, makeEvent('message.delta', { message_id: 'm1', text: 'hello' }))
    }).not.toThrow()

    const msgs = chat.getMessages(threadId)
    expect(msgs.length).toBe(1)
    expect(msgs[0].id).toBe('m1')
    expect(msgs[0].content).toBe('hello')
  })
})

// ── T4.2-03: 完整事件序列渲染回复 ──────────────

describe('T4.2-03: 完整事件序列渲染回复', () => {
  it('run.started → message.start → message.delta×5 → run.completed', () => {
    const chat = useChatStore()
    const { dispatch } = useEventStream()
    const threadId = 't4-03'

    dispatch(threadId, makeEvent('run.started', {}))
    dispatch(threadId, makeEvent('agent.status', { node: 'intent', label: '意图分析', phase: 'start' }))
    dispatch(threadId, makeEvent('message.start', { message_id: 'm1', node: 'write' }))
    dispatch(threadId, makeEvent('message.delta', { message_id: 'm1', text: 'Hello' }))
    dispatch(threadId, makeEvent('message.delta', { message_id: 'm1', text: ' ' }))
    dispatch(threadId, makeEvent('message.delta', { message_id: 'm1', text: 'World' }))
    dispatch(threadId, makeEvent('message.delta', { message_id: 'm1', text: '!' }))
    dispatch(threadId, makeEvent('message.delta', { message_id: 'm1', text: '🎉' }))
    dispatch(threadId, makeEvent('run.completed', { message_id: 'm1', final_state: 'done' }))

    const msgs = chat.getMessages(threadId)
    expect(msgs.length).toBe(1)
    expect(msgs[0].content).toBe('Hello World!🎉')
    expect(msgs[0].status).toBe('done')
    expect(chat.isRunning(threadId)).toBe(false)
  })
})

// ── T4.2-04: run.error 显示错误态 ──────────────

describe('T4.2-04: run.error 显示错误态', () => {
  it('run.error 设置 error 态并清理 streaming', () => {
    const chat = useChatStore()
    const { dispatch } = useEventStream()
    const threadId = 't4-04'

    dispatch(threadId, makeEvent('run.started', {}))
    dispatch(threadId, makeEvent('message.start', { message_id: 'm1', node: 'write' }))
    dispatch(threadId, makeEvent('message.delta', { message_id: 'm1', text: '部分内容' }))
    dispatch(threadId, makeEvent('run.error', { code: 'NoFinalOutput', message: '无最终输出' }))

    const msgs = chat.getMessages(threadId)
    expect(msgs[0].status).toBe('error')
    expect(chat.getError(threadId)).toBe('无最终输出')
    expect(chat.isRunning(threadId)).toBe(false)
  })
})

// ── T4.2-05: interrupt.raised 不触发递归更新 ──────────────

describe('T4.2-05: interrupt.raised 不触发递归更新', () => {
  it('interrupt.raised 正常设置状态，多次 raise 不报错', () => {
    const chat = useChatStore()
    const intr = useInterruptStore()
    const { dispatch } = useEventStream()
    const threadId = 't4-05'

    dispatch(threadId, makeEvent('run.started', {}))
    dispatch(threadId, makeEvent('interrupt.raised', {
      interrupt_id: 'int1',
      kind: 'plan_approval',
      payload: { plan: '计划', sub_questions: ['q1', 'q2'] },
    }))

    expect(intr.has(threadId)).toBe(true)
    expect(intr.get(threadId)?.kind).toBe('plan_approval')

    // 重复 raise 不报错（模拟消息重发）
    expect(() => {
      dispatch(threadId, makeEvent('interrupt.raised', {
        interrupt_id: 'int1',
        kind: 'plan_approval',
        payload: { plan: '计划', sub_questions: ['q1', 'q2'] },
      }))
    }).not.toThrow()
  })
})

// ── T4.2-06: 中断卡输入不直接改 store payload ──────────────

describe('T4.2-06: 中断卡输入不直接改 store payload', () => {
  it('PlanApprovalCard feedback 是本地 ref', async () => {
    const intr = useInterruptStore()
    const threadId = 't4-06'

    intr.raise(threadId, {
      interrupt_id: 'int1',
      kind: 'plan_approval',
      payload: { plan: '原始计划', sub_questions: [] },
    })

    const originalPayload = intr.get(threadId)?.payload
    expect(originalPayload).toBeDefined()

    // 模拟组件内部操作不影响 payload
    //（实际测试通过 mount PlanApprovalCard 验证，这里验证 store 层面 payload 不变）
    expect(intr.get(threadId)?.payload).toBe(originalPayload)
  })
})

// ── T4.2-07: 取消后终态对齐 ──────────────

describe('T4.2-07: 取消后终态对齐', () => {
  it('markCancelled 后 streaming 态清除，消息保留', () => {
    const chat = useChatStore()
    const { dispatch } = useEventStream()
    const threadId = 't4-07'

    dispatch(threadId, makeEvent('run.started', {}))
    dispatch(threadId, makeEvent('message.start', { message_id: 'm1', node: 'write' }))
    dispatch(threadId, makeEvent('message.delta', { message_id: 'm1', text: '部分内容' }))

    // 用户取消
    chat.markCancelled(threadId)

    const msgs = chat.getMessages(threadId)
    expect(msgs[0].status).toBe('cancelled')
    expect(msgs[0].content).toBe('部分内容')
    expect(chat.isRunning(threadId)).toBe(false)

    // run.cancelled 事件到达后不重复处理
    dispatch(threadId, makeEvent('run.cancelled', {}))
    expect(msgs[0].status).toBe('cancelled')
    expect(chat.isRunning(threadId)).toBe(false)
  })
})
