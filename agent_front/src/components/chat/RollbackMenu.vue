<script setup lang="ts">
/**
 * RollbackMenu — 回滚入口组件。
 *
 * 显示 checkpoint 列表（调 GET /history/{thread_id}），
 * 选择某检查点 → 调 POST /rollback → 刷新消息流。
 * 回滚后如 thread 处于 running，先 cancel 再回滚（409 防护联动）。
 */
import { ref, onMounted, computed } from 'vue'
import { NButton, NPopselect, NSpin } from 'naive-ui'
import { fetchHistory, rollbackThread, cancelResearch } from '../../api/rest'
import type { CheckpointItem } from '../../api/rest'
import { useChatStore } from '../../stores/chat'
import { fetchThreadMessages, toChatMessages } from '../../api/rest'

const props = defineProps<{ threadId: string }>()
const emit = defineEmits<{ (e: 'rolled'): void }>()

const checkpoints = ref<CheckpointItem[]>([])
const loading = ref(false)
const rolling = ref(false)
const error = ref('')

const chat = useChatStore()
const isRunning = computed(() => chat.isRunning(props.threadId))

async function loadCheckpoints() {
  loading.value = true
  error.value = ''
  try {
    const result = await fetchHistory(props.threadId)
    checkpoints.value = result.history || []
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载历史快照失败'
  } finally {
    loading.value = false
  }
}

async function doRollback(checkpointId: string) {
  rolling.value = true
  error.value = ''
  try {
    // 如果正在运行，先取消
    if (isRunning.value) {
      try {
        await cancelResearch(props.threadId)
      } catch {
        // 即使取消失败也继续回滚
      }
    }
    await rollbackThread(props.threadId, checkpointId)
    // 回滚后重新加载消息
    const data = await fetchThreadMessages(props.threadId)
    const loaded = toChatMessages(props.threadId, data.messages || [])
    chat.setMessages(props.threadId, loaded as never)
    emit('rolled')
  } catch (err) {
    error.value = err instanceof Error ? err.message : '回滚失败'
  } finally {
    rolling.value = false
  }
}

onMounted(() => { void loadCheckpoints() })

const options = computed(() =>
  checkpoints.value.map((cp) => ({
    label: cp.created_at ? new Date(cp.created_at).toLocaleString('zh-CN') : cp.checkpoint_id,
    value: cp.checkpoint_id,
  })),
)
</script>

<template>
  <div class="rollback-menu">
    <NButton
      size="small"
      quaternary
      :loading="loading"
      :disabled="checkpoints.length === 0"
      @click="loadCheckpoints"
    >
      ⟲ 回滚
    </NButton>
    <NPopselect
      v-if="checkpoints.length > 0"
      :options="options"
      trigger="click"
      @update:value="(v: string) => doRollback(v)"
    >
      <NButton size="small" quaternary :loading="rolling">
        选择检查点
      </NButton>
    </NPopselect>
    <p v-if="error" class="rollback-error">{{ error }}</p>
  </div>
</template>

<style scoped>
.rollback-menu {
  display: flex;
  align-items: center;
  gap: 8px;
}
.rollback-error {
  font-size: 12px;
  color: #f5222d;
}
</style>
