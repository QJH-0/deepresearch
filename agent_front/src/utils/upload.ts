/**
 * 上传任务的阶段定义与展示辅助。
 *
 * RAG 入库是流水线：排队 → 上传 → 解析切块 → Embedding → 完成。
 * 只有「上传」阶段有真实字节进度，其余阶段只能报状态，
 * 所以阶段指示和进度条要分开表达，不能混成一根假的进度条。
 */
import type { UploadStage } from '../types'

const STAGE_ORDER: UploadStage[] = ['queued', 'uploading', 'parsing', 'embedding', 'done']

export const STAGE_LABELS: Record<UploadStage, string> = {
  queued: '排队中',
  uploading: '上传中',
  parsing: '解析切块中',
  embedding: 'Embedding 并写入向量库',
  done: '已完成',
  failed: '失败',
}

export function stageIndex(stage: UploadStage): number {
  const idx = STAGE_ORDER.indexOf(stage)
  return idx < 0 ? 0 : idx
}

/** 文件扩展名是否受支持 */
export function isSupportedFile(filename: string, extensions: string[]): boolean {
  if (extensions.length === 0) return true
  const dot = filename.lastIndexOf('.')
  if (dot < 0) return false
  return extensions.includes(filename.slice(dot).toLowerCase())
}
