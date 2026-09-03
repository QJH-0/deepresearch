/**
 * Interrupt Store — HITL 审批/澄清状态管理。
 *
 * 核心设计：
 * - Map<thread_id, InterruptState> 持久化：切会话回来还在
 * - 数据源：interrupt.raised 事件（实时）+ GET /threads/{id}/interrupt（重连重建）
 * - resume 成功后清除该 thread 的 interrupt 状态
 *
 * P1-3 修复：新增 rebuild(threadId) 方法，切会话时调用后端 API 重建审批卡片。
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

  /**
   * P1-3 修复：从后端 GET /threads/{id}/interrupt 重建 interrupt 状态。
   *
   * 切会话时调用，确保审批卡片在页面刷新或切会话后仍能重建。
   * 后端返回 {active: true, interrupt_id, kind, payload} 或 {active: false}。
   */
  async function rebuild(threadId: string): Promise<boolean> {
    // 如果内存中已有该 thread 的 interrupt，不重复请求
    if (interrupts.has(threadId)) {
      return true
    }
    try {
      const resp = await fetch(`/api/v1/research/threads/${encodeURIComponent(threadId)}/interrupt`)
      if (!resp.ok) {
        return false
      }
      const data = await resp.json() as {
        active: boolean
        interrupt_id?: string
        kind?: InterruptKind
        payload?: Record<string, unknown>
      }
      if (data.active && data.interrupt_id && data.kind) {
        interrupts.set(threadId, {
          interrupt_id: data.interrupt_id,
          kind: data.kind,
          payload: data.payload || {},
          ts: Date.now(),
        })
        return true
      }
      return false
    } catch {
      return false
    }
  }

  return { interrupts, raise, clear, get, has, rebuild }
})
