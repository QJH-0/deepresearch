<script setup lang="ts">
/**
 * ClarifyCard — HITL 澄清卡片。
 *
 * 澄清问题列表 + 多输入框回答 + 提交
 */
import { computed, ref } from 'vue'
import { NInput, NButton } from 'naive-ui'

const props = defineProps<{
  payload: Record<string, unknown>
}>()
const emit = defineEmits<{
  (e: 'resume', value: Record<string, unknown>): void
}>()

const questions = computed(() => {
  const raw = props.payload.questions as string[] | undefined
  return raw || []
})

const answers = ref<Record<number, string>>({})

function submit() {
  const answerList = questions.value.map((_, idx) => answers.value[idx] || '')
  emit('resume', { answers: answerList })
}

function skip() {
  emit('resume', { action: 'skip' })
}
</script>

<template>
  <div class="hitl-card clarify-card">
    <div class="card-header">
      <span class="card-icon">❓</span>
      <span class="card-title">需要补充信息</span>
    </div>

    <div class="card-body">
      <p v-if="payload.message" class="card-message">{{ payload.message }}</p>

      <div v-for="(q, idx) in questions" :key="idx" class="question-item">
        <p class="question-label">{{ idx + 1 }}. {{ q }}</p>
        <NInput
          v-model:value="answers[idx]"
          type="textarea"
          :rows="2"
          placeholder="请回答…"
        />
      </div>

      <div v-if="questions.length === 0" class="no-questions">
        <NInput
          v-model:value="answers[0]"
          type="textarea"
          :rows="3"
          placeholder="请输入补充信息…"
        />
      </div>

      <div class="card-actions">
        <NButton type="primary" @click="submit">提交回答</NButton>
        <NButton @click="skip">跳过</NButton>
      </div>
    </div>
  </div>
</template>

<style scoped>
.hitl-card {
  border: 1px solid #e4eafb;
  border-radius: 12px;
  background: #fff;
  margin: 12px 0;
  overflow: hidden;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}
.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  background: linear-gradient(135deg, #fff9f0 0%, #fff5e6 100%);
  border-bottom: 1px solid #f5eee0;
}
.card-icon { font-size: 16px; }
.card-title { font-weight: 600; font-size: 14px; color: #333; }
.card-body { padding: 16px; }
.card-message {
  font-size: 13px;
  color: #666;
  margin-bottom: 12px;
}
.question-item {
  margin-bottom: 12px;
}
.question-label {
  font-size: 13px;
  color: #5f719b;
  margin-bottom: 4px;
}
.no-questions { margin-bottom: 12px; }
.card-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
</style>
