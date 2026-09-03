<script setup lang="ts">
/**
 * StopButton — 生成中显示；点击调 /cancel；本地先置 cancelled 态（乐观更新）。
 */
import { NButton } from 'naive-ui'

const props = defineProps<{
  loading: boolean
}>()
const emit = defineEmits<{ (e: 'stop'): void }>()
</script>

<template>
  <NButton
    v-if="loading"
    type="error"
    ghost
    size="small"
    class="stop-button"
    @click="emit('stop')"
  >
    ⏹ 停止生成
  </NButton>
</template>

<style scoped>
.stop-button {
  flex-shrink: 0;
}
</style>
