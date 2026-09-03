/**
 * API 统一出口 — 向后兼容 re-export。
 *
 * 新代码请直接从 api/rest.ts 和 api/sse.ts 导入。
 * 旧代码的 import { ... } from '../api' 仍可工作。
 */
export {
  ApiError,
  getUserId,
  setUserId,
  fetchThreads,
  fetchThreadMessages,
  renameThreadApi as renameThread,
  pinThreadApi as pinThread,
  deleteThreadApi as deleteThread,
  cancelResearch,
  fetchHistory,
  rollbackThread,
  fetchMemories,
  fetchDocuments,
  fetchDocumentStats,
  fetchUploadLimits,
  deleteDocument,
  batchDeleteDocuments,
  retryDocument,
  uploadDocument,
  toChatMessages,
} from './rest'
export type { DocumentItem } from '../types'
