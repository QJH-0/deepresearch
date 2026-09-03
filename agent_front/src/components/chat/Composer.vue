<script setup lang="ts">
/**
 * 输入区（重构版）。Enter 发送、Shift+Enter 换行，运行中显示 StopButton。
 */
import { nextTick, ref, watch } from 'vue'
import StopButton from './StopButton.vue'

const props = defineProps<{ disabled: boolean; loading: boolean }>()
const emit = defineEmits<{
  (e: 'send', text: string): void
  (e: 'stop'): void
}>()

const text = ref('')
const textarea = ref<HTMLTextAreaElement | null>(null)

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
    <button v-else class="send-btn" :disabled="!text.trim()" @click="send">发送</button>
  </div>
</template>

<style scoped>
.composer {
  display: flex;
  gap: 8px;
  align-items: flex-end;
  padding: 12px 16px;
  border-top: 1px solid #eef2fb;
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
