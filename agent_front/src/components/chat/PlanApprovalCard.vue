<script setup lang="ts">
/**
 * PlanApprovalCard — HITL 计划审批卡片。
 *
 * 子问题列表 + 三按钮（批准/修改/否决）
 * "修改"弹出原因输入框（NInput + 确认）
 * 显示 revision_count（第 n/3 次修改）
 */
import { computed, ref } from 'vue'
import { NInput, NButton } from 'naive-ui'
import MarkdownRender from './MarkdownRender.vue'

const props = defineProps<{
  payload: Record<string, unknown>
}>()
const emit = defineEmits<{
  (e: 'resume', value: Record<string, unknown>): void
}>()

const feedback = ref('')
const showFeedback = ref(false)

const plan = computed(() => String(props.payload.plan || ''))
const subQuestions = computed(() => (props.payload.sub_questions as string[] | undefined) || [])
const revisionCount = computed(() => Number(props.payload.revision_count || 0))

function approve() {
  emit('resume', { action: 'approve' })
}
function requestRevision() {
  showFeedback.value = true
}
function submitRevision() {
  emit('resume', { action: 'revise', reason: feedback.value })
  showFeedback.value = false
  feedback.value = ''
}
function reject() {
  emit('resume', { action: 'reject' })
}
</script>

<template>
  <div class="hitl-card plan-approval">
    <div class="card-header">
      <span class="card-icon">📋</span>
      <span class="card-title">研究计划待审批</span>
      <span v-if="revisionCount > 0" class="revision-badge">第 {{ revisionCount + 1 }} 次修改</span>
    </div>

    <div class="card-body">
      <div v-if="plan" class="plan-section">
        <p class="section-label">研究计划</p>
        <MarkdownRender :content="plan" />
      </div>

      <div v-if="subQuestions.length" class="sub-questions">
        <p class="section-label">子问题</p>
        <ol>
          <li v-for="(sq, idx) in subQuestions" :key="idx">{{ sq }}</li>
        </ol>
      </div>

      <div v-if="showFeedback" class="feedback-section">
        <p class="section-label">修改原因</p>
        <NInput
          v-model:value="feedback"
          type="textarea"
          :rows="3"
          placeholder="请输入修改意见…"
        />
        <div class="feedback-actions">
          <NButton size="small" @click="showFeedback = false">取消</NButton>
          <NButton size="small" type="primary" @click="submitRevision">提交修改</NButton>
        </div>
      </div>

      <div v-else class="card-actions">
        <NButton type="primary" @click="approve">批准</NButton>
        <NButton @click="requestRevision">修改</NButton>
        <NButton type="error" ghost @click="reject">否决</NButton>
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
  background: linear-gradient(135deg, #f8faff 0%, #f0f4ff 100%);
  border-bottom: 1px solid #eef2fb;
}
.card-icon { font-size: 16px; }
.card-title { font-weight: 600; font-size: 14px; color: #333; }
.revision-badge {
  font-size: 11px;
  color: #fa8c16;
  background: #fff7e6;
  border-radius: 999px;
  padding: 2px 8px;
  margin-left: auto;
}
.card-body {
  padding: 16px;
}
.section-label {
  font-size: 12px;
  color: #8b9bc0;
  margin-bottom: 6px;
}
.sub-questions ol {
  margin: 4px 0 0;
  padding-left: 20px;
}
.sub-questions li {
  font-size: 13px;
  color: #5f719b;
  line-height: 1.8;
}
.feedback-section {
  margin-top: 12px;
}
.feedback-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  margin-top: 8px;
}
.card-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
</style>
