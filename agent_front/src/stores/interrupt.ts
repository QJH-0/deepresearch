/**
 * Interrupt Store — HITL 审批/澄清状态管理。
 *
 * 核心设计：
 * - Map<thread_id, InterruptState> 持久化：切会话回来还在
 * - 数据源：interrupt.raised 事件（实时）+ GET /threads/{id}/interrupt（重连重建）
 * - resume 成功后清除该 thread 的 interrupt 状态
 */
import { defineStore } from 'pinia'
import { reactive } from 'vue'
import type { InterruptKind } from '../types/events.gen'

export interface InterruptState {
  interrupt_id: string
  kind: InterruptKind
  payload: Record<string, unknown>
  /** 收到时间 */
  ts: number
}

export const useInterruptStore = defineStore('interrupt', () => {
  const interrupts = reactive<Map<string, InterruptState>>(new Map())

  function raise(threadId: string, data: {
    interrupt_id: string
    kind: InterruptKind
    payload: Record<string, unknown>
  }): void {
    interrupts.set(threadId, {
      interrupt_id: data.interrupt_id,
      kind: data.kind,
      payload: data.payload,
      ts: Date.now(),
    })
  }

  function clear(threadId: string): void {
    interrupts.delete(threadId)
  }

  function get(threadId: string): InterruptState | null {
    return interrupts.get(threadId) || null
  }

  function has(threadId: string): boolean {
    return interrupts.has(threadId)
  }

  return { interrupts, raise, clear, get, has }
})
