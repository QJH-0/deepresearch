<script setup lang="ts">
/**
 * HITL 人工干预卡片。
 *
 * 三种中断类型各自有不同的确认动作，这里按 type 分支渲染，
 * 而不是把三种表单全摊开给用户。
 */
import { computed, ref } from 'vue'
import type { StreamEvent } from '../../types'
import { markdownToHtml } from '../../utils/markdown'

const props = defineProps<{ interrupt: StreamEvent }>()
const emit = defineEmits<{ (e: 'resume', value: Record<string, unknown>): void }>()

const feedback = ref('')
const value = computed(() => (props.interrupt.value || {}) as Record<string, unknown>)
const interruptType = computed(() => String(value.value.type || ''))
const message = computed(() => String(value.value.message || props.interrupt.message || ''))

const draftHtml = computed(() => markdownToHtml(String(value.value.draft || '')))
const subQuestions = computed(() => (value.value.sub_questions as string[] | undefined) || [])
const missingGaps = computed(() => (value.value.missing_gaps as string[] | undefined) || [])
const analysisSummary = computed(() => String(value.value.analysis_summary || ''))
const plan = computed(() => String(value.value.plan || ''))
</script>

<template>
  <div class="interrupt-card">
    <div class="interrupt-header">
      <span class="interrupt-icon">⏸</span>
      <span>任务已暂停，需要你确认</span>
    </div>

    <div class="interrupt-body">
      <p v-if="message" class="interrupt-message">{{ message }}</p>

      <!-- plan_review: 规划确认 -->
      <div v-if="interruptType === 'plan_review'" class="interrupt-content">
        <div class="plan-display">
          <p v-if="plan"><strong>研究计划：</strong>{{ plan }}</p>
          <template v-if="subQuestions.length">
            <p class="sub-title"><strong>子问题：</strong></p>
            <ul>
              <li v-for="sq in subQuestions" :key="sq">{{ sq }}</li>
            </ul>
          </template>
        </div>
        <textarea v-model="feedback" class="interrupt-input" rows="3" placeholder="修改意见（留空表示同意当前计划）" />
        <div class="interrupt-actions">
          <button class="interrupt-btn primary" @click="emit('resume', { approved: true })">确认计划</button>
          <button class="interrupt-btn" @click="emit('resume', { approved: false, feedback })">提交修改</button>
        </div>
      </div>

      <!-- analyze_clarify: 证据不足，需要补充 -->
      <div v-else-if="interruptType === 'analyze_clarify'" class="interrupt-content">
        <div class="gaps-display">
          <p><strong>信息缺口：</strong></p>
          <ul>
            <li v-for="gap in missingGaps" :key="gap">{{ gap }}</li>
          </ul>
          <p v-if="analysisSummary" class="analysis-preview">{{ analysisSummary }}</p>
        </div>
        <textarea v-model="feedback" class="interrupt-input" rows="3" placeholder="输入补充信息（可选）" />
        <div class="interrupt-actions">
          <button class="interrupt-btn primary" @click="emit('resume', { action: 'auto_search' })">自动补搜</button>
          <button class="interrupt-btn" @click="emit('resume', { action: 'user_supply', info: feedback })">补充信息</button>
          <button class="interrupt-btn" @click="emit('resume', { action: 'skip' })">跳过缺口</button>
        </div>
      </div>

      <!-- write_review: 报告审核 -->
      <div v-else-if="interruptType === 'write_review'" class="interrupt-content">
        <div class="draft-preview markdown-body" v-html="draftHtml" />
        <textarea v-model="feedback" class="interrupt-input" rows="3" placeholder="修改意见（留空表示通过）" />
        <div class="interrupt-actions">
          <button class="interrupt-btn primary" @click="emit('resume', { approved: true })">通过</button>
          <button class="interrupt-btn" @click="emit('resume', { approved: false, feedback })">要求修改</button>
        </div>
      </div>

      <!-- 兜底：未知中断类型也给出继续入口 -->
      <div v-else class="interrupt-content">
        <div class="interrupt-actions">
          <button class="interrupt-btn primary" @click="emit('resume', { approved: true })">继续</button>
        </div>
      </div>
    </div>
  </div>
</template>
