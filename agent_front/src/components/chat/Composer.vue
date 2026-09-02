<script setup lang="ts">
/**
 * 输入区。Enter 发送、Shift+Enter 换行，运行中可中断。
 */
import { nextTick, ref, watch } from 'vue'

const props = defineProps<{ disabled: boolean; loading: boolean }>()
const emit = defineEmits<{ (e: 'send', text: string): void; (e: 'stop'): void }>()

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
    <button v-if="loading" class="stop-btn" @click="emit('stop')">⏹ 停止</button>
    <button v-else class="send-btn" :disabled="!text.trim()" @click="send">发送</button>
  </div>
</template>
