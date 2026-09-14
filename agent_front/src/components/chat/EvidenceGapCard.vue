<script setup lang="ts">
/**
 * EvidenceGapCard — HITL 证据缺口处置卡片（kind=evidence_gap）。
 *
 * 由 analyze 节点在判定信息缺口时发起，载荷形如：
 *   { node, missing_gaps: string[], analysis_summary, message }
 *
 * 与 ClarifyCard 的区别：澄清卡片收集「问题答案」，本卡片收集「缺口处置动作」。
 * 两者载荷结构不兼容，因此在协议层使用独立 kind。
 */
import { computed, ref } from 'vue'
import { NInput, NButton } from 'naive-ui'

const props = defineProps<{
  payload: Record<string, unknown>
}>()
const emit = defineEmits<{
  (e: 'resume', value: Record<string, unknown>): void
}>()

const gaps = computed<string[]>(() => {
  const raw = props.payload.missing_gaps
  if (!Array.isArray(raw)) return []
  return raw.map((item) => String(item)).filter(Boolean)
})

const summary = computed(() => String(props.payload.analysis_summary || ''))

const showSupply = ref(false)
const supplyText = ref('')

function autoSearch() {
  emit('resume', { kind: 'evidence_gap', action: 'auto_search' })
}

function toggleSupply() {
  showSupply.value = !showSupply.value
}

function submitSupply() {
  const info = supplyText.value.trim()
  if (!info) return
  emit('resume', { kind: 'evidence_gap', action: 'user_supply', info })
  showSupply.value = false
  supplyText.value = ''
}

function skip() {
  emit('resume', { kind: 'evidence_gap', action: 'skip' })
}
</script>

<template>
  <div class="hitl-card evidence-gap-card">
    <div class="card-header">
      <span class="card-icon">🔍</span>
      <span class="card-title">证据不足</span>
    </div>

    <div class="card-body">
      <p v-if="payload.message" class="card-message">{{ payload.message }}</p>

      <div v-if="gaps.length" class="gap-list">
        <p class="section-label">待补信息缺口</p>
        <ul>
          <li v-for="(gap, idx) in gaps" :key="idx">{{ gap }}</li>
        </ul>
      </div>

      <p v-else-if="summary" class="gap-summary">{{ summary }}</p>

      <div v-if="showSupply" class="supply-section">
        <NInput
          v-model:value="supplyText"
          type="textarea"
          :rows="3"
          placeholder="请输入你已知的补充信息…"
        />
      </div>

      <div class="card-actions">
        <NButton type="primary" @click="autoSearch">自动补搜</NButton>
        <NButton @click="toggleSupply">
          {{ showSupply ? '取消补充' : '我来补充' }}
        </NButton>
        <NButton v-if="showSupply" type="primary" ghost @click="submitSupply">提交补充</NButton>
        <NButton @click="skip">跳过缺口</NButton>
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
  flex-shrink: 0;
}
.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  background: linear-gradient(135deg, #f3f6ff 0%, #eef2ff 100%);
  border-bottom: 1px solid #e6ebfa;
}
.card-icon { font-size: 16px; }
.card-title { font-weight: 600; font-size: 14px; color: #333; }
.card-body { padding: 16px; }
.card-message {
  font-size: 13px;
  color: #666;
  margin-bottom: 12px;
}
.section-label {
  font-size: 12px;
  color: #5f719b;
  margin-bottom: 6px;
}
.gap-list ul {
  margin: 0 0 12px;
  padding-left: 18px;
}
.gap-list li {
  font-size: 13px;
  color: #444;
  line-height: 1.7;
}
.gap-summary {
  font-size: 13px;
  color: #666;
  line-height: 1.7;
  margin-bottom: 12px;
}
.supply-section { margin-bottom: 12px; }
.card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

.evidence-gap-card {
  max-height: 60vh;
  overflow-y: auto;
}
</style>
