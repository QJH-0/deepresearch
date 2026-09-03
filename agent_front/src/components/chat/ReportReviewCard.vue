<script setup lang="ts">
/**
 * ReportReviewCard — HITL 报告审核卡片。
 *
 * 报告预览 + 采纳 / 再深入（方向输入，多选追加子问题）
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

const draft = computed(() => String(props.payload.draft || props.payload.report || ''))
const showDeepen = ref(false)
const deepenText = ref('')

function accept() {
  emit('resume', { kind: 'report_review', action: 'adopt' })
}
function deepen() {
  showDeepen.value = true
}
function submitDeepen() {
  const subs = deepenText.value.split('\n').map((s) => s.trim()).filter(Boolean)
  emit('resume', { kind: 'report_review', action: 'deepen', extra_sub_questions: subs })
  showDeepen.value = false
  deepenText.value = ''
}
// P1-4: reject 在后端 schema 中未定义，已删除
</script>

<template>
  <div class="hitl-card report-review">
    <div class="card-header">
      <span class="card-icon">📝</span>
      <span class="card-title">报告待审核</span>
    </div>

    <div class="card-body">
      <div class="report-preview">
        <MarkdownRender :content="draft" />
      </div>

      <div v-if="showDeepen" class="deepen-section">
        <p class="section-label">追加子问题（每行一个）</p>
        <NInput
          v-model:value="deepenText"
          type="textarea"
          :rows="4"
          placeholder="输入要深入研究的子问题，每行一个…"
        />
        <div class="deepen-actions">
          <NButton size="small" @click="showDeepen = false">取消</NButton>
          <NButton size="small" type="primary" @click="submitDeepen">提交</NButton>
        </div>
      </div>

      <div v-else class="card-actions">
        <NButton type="primary" @click="accept">采纳</NButton>
        <NButton @click="deepen">再深入</NButton>
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
  background: linear-gradient(135deg, #f0fff4 0%, #e6ffe6 100%);
  border-bottom: 1px solid #d9f5d9;
}
.card-icon { font-size: 16px; }
.card-title { font-weight: 600; font-size: 14px; color: #333; }
.card-body { padding: 16px; }
.report-preview {
  max-height: 400px;
  overflow-y: auto;
  border: 1px solid #f0f0f0;
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.deepen-section { margin-top: 12px; }
.section-label {
  font-size: 12px;
  color: #8b9bc0;
  margin-bottom: 6px;
}
.deepen-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  margin-top: 8px;
}
.card-actions {
  display: flex;
  gap: 8px;
}
</style>
