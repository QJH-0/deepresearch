<script setup lang="ts">
/**
 * 输入区（重构版）。Enter 发送、Shift+Enter 换行，运行中显示 StopButton。
 *
 * 停止态（用户手动停止且 checkpoint 可续）额外给出两个显式动作入口：
 * 「补充条件继续研究」与「换个主题重新研究」。停止后的下一步有两种截然不同
 * 的归宿，让用户明确表态比用关键词猜意图可靠 —— 直接发送仍走关键词分流。
 */
import { computed, nextTick, ref, watch } from 'vue'
import StopButton from './StopButton.vue'

const props = defineProps<{ disabled: boolean; loading: boolean; stopped?: boolean }>()
const emit = defineEmits<{
  (e: 'send', text: string): void
  (e: 'stop'): void
  (e: 'continue-with-condition', text: string): void
  (e: 'restart-topic', text: string): void
}>()

const text = ref('')
const textarea = ref<HTMLTextAreaElement | null>(null)

/** 两个显式入口都要拿输入框内容当参数，空输入时无从下手，直接禁用 */
const hasText = computed(() => text.value.trim().length > 0)
const showStopActions = computed(() => Boolean(props.stopped) && !props.loading)

function autoResize() {
  const el = textarea.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 200)}px`
}

watch(text, () => nextTick(autoResize))

function send() {
  const value = text.value.trim()
  if (!value || props.loading) return
  text.value = ''
  nextTick(autoResize)
  emit('send', value)
}

function emitStopAction(event: 'continue-with-condition' | 'restart-topic') {
  const value = text.value.trim()
  if (!value || props.loading) return
  text.value = ''
  nextTick(autoResize)
  emit(event, value)
}

function fill(text_: string) {
  text.value = text_
  nextTick(() => {
    autoResize()
    textarea.value?.focus()
  })
}

defineExpose({ fill })
</script>

<template>
  <div class="composer-wrap">
    <div v-if="showStopActions" class="stop-actions">
      <span class="stop-hint">上次研究已停止，接下来：</span>
      <button
        class="stop-action-btn primary"
        :disabled="!hasText"
        :title="hasText ? '' : '先填写要补充的条件'"
        @click="emitStopAction('continue-with-condition')"
      >
        补充条件继续研究
      </button>
      <button
        class="stop-action-btn"
        :disabled="!hasText"
        :title="hasText ? '' : '先填写新的主题'"
        @click="emitStopAction('restart-topic')"
      >
        换个主题重新研究
      </button>
    </div>

    <div class="composer">
      <textarea
        ref="textarea"
        v-model="text"
        class="composer-input"
        rows="1"
        :disabled="disabled"
        :placeholder="loading ? '研究进行中…（可随时点停止）' : '输入你的问题，Enter 发送，Shift + Enter 换行'"
        @keydown.enter.exact.prevent="send"
      />
      <StopButton v-if="loading" :loading="loading" @stop="emit('stop')" />
      <button v-else class="send-btn" :disabled="!hasText" @click="send">发送</button>
    </div>
  </div>
</template>

<style scoped>
.composer-wrap {
  border-top: 1px solid #eef2fb;
}
.stop-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 16px 0;
}
.stop-hint {
  color: #6b7a99;
  font-size: 12px;
}
.stop-action-btn {
  padding: 6px 12px;
  border: 1px solid #d9e3f9;
  border-radius: 8px;
  background: #f8faff;
  color: #3f67d4;
  font-size: 13px;
  cursor: pointer;
  transition: background 0.15s;
}
.stop-action-btn.primary {
  border-color: #3f67d4;
  background: #eef2fd;
}
.stop-action-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.stop-action-btn:not(:disabled):hover {
  background: #eef2fd;
}
.composer {
  display: flex;
  gap: 8px;
  align-items: flex-end;
  padding: 12px 16px;
}
.composer-input {
  flex: 1;
  resize: none;
  border: 1px solid #d9e3f9;
  border-radius: 12px;
  padding: 10px 14px;
  font-size: 14px;
  line-height: 1.5;
  outline: none;
  font-family: inherit;
  transition: border-color 0.2s;
}
.composer-input:focus {
  border-color: #3f67d4;
}
.composer-input:disabled {
  background: #f5f5f5;
  cursor: not-allowed;
}
.send-btn {
  padding: 8px 20px;
  border: none;
  border-radius: 8px;
  background: #3f67d4;
  color: #fff;
  font-size: 14px;
  cursor: pointer;
  transition: opacity 0.2s;
  flex-shrink: 0;
}
.send-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.send-btn:not(:disabled):hover {
  opacity: 0.9;
}
</style>
