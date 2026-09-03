/**
 * SSE 流式消费 — fetch + ReadableStream 手动解析。
 *
 * POST body 需 fetch（不能用原生 EventSource），按 \n\n 切帧，
 * 粘包/半包由 buffer 兜住。支持跨 chunk 的半行 JSON 缓冲处理。
 */
import type { EventEnvelope, EventType } from '../types/events.gen'

/**
 * 从 SSE Response 消费事件，通过回调分发。
 * run/resume 共用此函数——两入口只是请求不同，事件处理链唯一。
 */
export async function consumeSSE(
  resp: Response,
  onEvent: (env: EventEnvelope) => void,
  onDone?: () => void,
  onError?: (err: Error) => void,
): Promise<void> {
  if (!resp.ok) {
    const text = await resp.text().catch(() => '')
    throw new Error(text || `请求失败: ${resp.status}`)
  }
  if (!resp.body) throw new Error('当前环境不支持流式响应')

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // 按 \n\n 切帧，最后一个元素可能是半帧，留到下一轮
      const frames = buffer.split('\n\n')
      buffer = frames.pop() || ''

      for (const frame of frames) {
        const line = frame.trim()
        if (!line.startsWith('data:')) continue
        const payload = line.slice(5).trim()
        if (!payload) continue
        try {
          const env = JSON.parse(payload) as EventEnvelope
          onEvent(env)
        } catch {
          // 单帧解析失败不应中断整条流，跳过即可
        }
      }
    }
    onDone?.()
  } catch (err) {
    if (err instanceof Error && err.name === 'AbortError') {
      // 用户主动取消，不算错误
      onDone?.()
      return
    }
    onError?.(err instanceof Error ? err : new Error(String(err)))
    throw err
  } finally {
    reader.releaseLock()
  }
}

/**
 * POST 流式请求 — 发起 POST 并消费 SSE 流。
 * 返回的 Response 交给 consumeSSE 处理。
 */
export async function postStream(url: string, body: unknown): Promise<Response> {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return resp
}

/** 提取 SSE 行（导出供测试用） */
export function takeSseLines(buffer: string): string[] {
  const frames = buffer.split('\n\n')
  const lines: string[] = []
  for (const frame of frames) {
    const line = frame.trim()
    if (line.startsWith('data:')) {
      lines.push(line.slice(5).trim())
    }
  }
  return lines
}
