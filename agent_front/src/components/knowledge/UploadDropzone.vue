<script setup lang="ts">
/**
 * 知识库上传区（多文件、拖拽、限制前置说明）。
 *
 * 对齐业界 RAG 产品的做法：
 *  1. 上传前就把「支持格式 / 单文件上限 / 单批数量」写在界面上，
 *     而不是上传失败才报错 —— Embedding 按量计费，限制要前置说明
 *  2. 允许一次选多个文件，逐个走流水线（见 UploadTaskList）
 */
import { computed, ref } from 'vue'
import type { UploadLimits } from '../../types'

const props = defineProps<{
  limits: UploadLimits | null
  disabled?: boolean
}>()

const emit = defineEmits<{ (e: 'upload', files: File[]): void }>()

const dragOver = ref(false)
const dragDepth = ref(0)
const fileInput = ref<HTMLInputElement | null>(null)

const acceptAttr = computed(() => (props.limits?.extensions || []).join(','))
const maxSizeMb = computed(() => props.limits?.max_file_size_mb ?? 50)
const maxFiles = computed(() => props.limits?.max_files_per_batch ?? 20)

const hintText = computed(() => {
  const exts = (props.limits?.extensions || []).map((e) => e.replace('.', '').toUpperCase())
  const shown = exts.length > 0 ? exts.join(' / ') : 'PDF / Word / Markdown / TXT / HTML / CSV / JSON'
  // 注意：computed 只在模板里自动解包，JS 模板字符串里必须显式取 .value，
  // 否则会渲染成 [object Object]
  return `支持 ${shown} · 单文件 ≤ ${maxSizeMb.value} MB · 单次最多 ${maxFiles.value} 个`
})

function onDragEnter(event: DragEvent) {
  event.preventDefault()
  dragDepth.value += 1
  dragOver.value = true
}

function onDragLeave(event: DragEvent) {
  event.preventDefault()
  dragDepth.value -= 1
  if (dragDepth.value <= 0) {
    dragDepth.value = 0
    dragOver.value = false
  }
}

function onDrop(event: DragEvent) {
  event.preventDefault()
  dragDepth.value = 0
  dragOver.value = false
  if (props.disabled) return
  const files = Array.from(event.dataTransfer?.files || [])
  if (files.length > 0) emit('upload', files)
}

function pickFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  if (files.length > 0) emit('upload', files)
  input.value = ''
}

function triggerPick() {
  if (!props.disabled) fileInput.value?.click()
}
</script>

<template>
  <div
    class="upload-dropzone"
    :class="{ 'drag-active': dragOver, disabled }"
    @dragenter="onDragEnter"
    @dragover.prevent
    @dragleave="onDragLeave"
    @drop="onDrop"
    @click="triggerPick"
  >
    <input
      ref="fileInput"
      type="file"
      multiple
      :accept="acceptAttr"
      class="hidden-input"
      @change="pickFiles"
    />
    <div class="dropzone-inner">
      <span class="dropzone-icon" aria-hidden="true">
        <svg v-if="dragOver" viewBox="0 0 24 24" width="24" height="24">
          <path d="M12 4v10m0 0l-4-4m4 4l4-4M5 19h14" fill="none" stroke="currentColor"
                stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <svg v-else viewBox="0 0 24 24" width="24" height="24">
          <path d="M6 3h8l4 4v14H6z" fill="none" stroke="currentColor" stroke-width="1.6"
                stroke-linejoin="round" />
          <path d="M14 3v4h4" fill="none" stroke="currentColor" stroke-width="1.6"
                stroke-linejoin="round" />
        </svg>
      </span>
      <p class="dropzone-title">{{ dragOver ? '松开即可上传' : '拖拽文件到此处，或点击选择' }}</p>
      <p class="dropzone-hint">{{ hintText }}</p>
    </div>
  </div>
</template>
