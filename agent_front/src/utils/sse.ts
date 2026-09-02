/**
 * SSE（text/event-stream）读取器。
 *
 * fetch + ReadableStream 手动解析，而不是用 EventSource —— 因为研究接口是
 * POST + JSON body，EventSource 只支持 GET。
 *
 * 按 \n\n 切帧，粘包/半包由 buffer 兜住。
 */
import type { StreamEvent } from '../types'

export async function* streamSSE<T = StreamEvent>(
  url: string,
  body: unknown,
): AsyncGenerator<T, void, void> {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
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

      // 最后一个元素可能是半帧，留到下一轮
      const frames = buffer.split('\n\n')
      buffer = frames.pop() || ''

      for (const frame of frames) {
        const line = frame.trim()
        if (!line.startsWith('data:')) continue
        const payload = line.slice(5).trim()
        if (!payload) continue
        try {
          yield JSON.parse(payload) as T
        } catch {
          // 单帧解析失败不应中断整条流，跳过即可
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}
