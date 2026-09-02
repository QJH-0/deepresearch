<script setup lang="ts">
/**
 * 上传中的任务列表：每个文件一条，阶段指示 + 字节进度 + 切片索引进度。
 *
 * 只有 uploading 阶段展示字节百分比；进入 parsing / embedding 后改为
 * 「阶段点 + 切片计数」，因为这时候进度由服务端异步推进，前端拿不到百分比。
 */
import type { UploadTask } from '../../types'
import { formatBytes } from '../../utils/datetime'
import { STAGE_LABELS, stageIndex } from '../../utils/upload'

defineProps<{ tasks: UploadTask[] }>()
const emit = defineEmits<{ (e: 'dismiss', id: string): void }>()

const stageSteps = ['排队', '上传', '解析', '向量化']
</script>

<template>
  <TransitionGroup v-if="tasks.length" name="task" tag="ul" class="upload-task-list">
    <li v-for="task in tasks" :key="task.id" class="upload-task" :class="`stage-${task.stage}`">
      <div class="task-head">
        <span class="task-name" :title="task.filename">{{ task.filename }}</span>
        <span class="task-size">{{ formatBytes(task.size) }}</span>
        <span class="task-stage">{{ STAGE_LABELS[task.stage] }}</span>
        <button
          v-if="task.stage === 'done' || task.stage === 'failed'"
          class="task-dismiss"
          title="移除"
          @click="emit('dismiss', task.id)"
        >
          ✕
        </button>
      </div>

      <!-- 上传阶段：真实字节进度 -->
      <div v-if="task.stage === 'uploading'" class="progress-track slim">
        <div class="progress-fill" :style="{ width: `${task.percent}%` }" />
      </div>

      <!-- 非上传阶段：阶段指示点，诚实反映「只有状态没有百分比」 -->
      <ol v-else-if="task.stage !== 'failed'" class="stage-steps">
        <li
          v-for="(step, idx) in stageSteps"
          :key="step"
          class="stage-step"
          :class="{
            done: idx < stageIndex(task.stage),
            current: idx === stageIndex(task.stage),
          }"
        >
          {{ step }}
        </li>
      </ol>

      <p v-if="task.stage === 'embedding' && task.chunkCount > 0" class="task-note">
        已生成 {{ task.chunkCount }} 个切片，{{ task.indexedChunks }} 个已写入向量库
      </p>
      <p v-if="task.stage === 'failed'" class="task-error">{{ task.error }}</p>
      <p v-if="task.stage === 'done'" class="task-done">
        ✅ 已进入向量库 · {{ task.indexedChunks }}/{{ task.chunkCount }} 切片
      </p>
    </li>
  </TransitionGroup>
</template>
