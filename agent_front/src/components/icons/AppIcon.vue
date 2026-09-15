<script setup lang="ts">
/**
 * AppIcon — 统一图标组件。
 *
 * 用内联 SVG 而不是 emoji：emoji 在不同平台字形差异大、无法与文字基线对齐，
 * 也无法跟随 currentColor。所有图标共用一套描边风格（1.6px、圆头圆角）。
 */
const props = withDefaults(
  defineProps<{
    name: IconName
    size?: number
  }>(),
  { size: 16 },
)

type IconName =
  | 'plus'
  | 'chat'
  | 'library'
  | 'question'
  | 'list'
  | 'report'
  | 'search'
  | 'thinking'
  | 'check'
  | 'cross'
  | 'download'
  | 'copy'
  | 'link'
  | 'globe'
  | 'book'
  | 'paperclip'
  | 'file'
  | 'file-pdf'
  | 'file-doc'

const PATHS: Record<IconName, string> = {
  plus: 'M8 3v10M3 8h10',
  chat: 'M4 5h12v8H8l-4 3V5z',
  library: 'M4 4h5v12H4zM11 4h5v12h-5z',
  question: 'M8 6a2 2 0 112.6 1.9c-.4.2-.6.5-.6.9v.7M8 12.4h.01',
  list: 'M6 5h7M6 8h7M6 11h5M3.5 5h.01M3.5 8h.01M3.5 11h.01',
  report: 'M4 2.5h5l3 3v8H4zM9 2.5v3h3M6 9h4M6 11h3',
  search: 'M7.5 3a4.5 4.5 0 103.1 7.8L14 14',
  thinking: 'M8 3a3.5 3.5 0 00-2 6.4V11h4V9.4A3.5 3.5 0 008 3zM6.5 13h3',
  check: 'M3.5 8.5l3 3 6-6.5',
  cross: 'M4.5 4.5l7 7M11.5 4.5l-7 7',
  download: 'M8 3v7m0 0L5.5 7.5M8 10l2.5-2.5M3.5 13h9',
  copy: 'M6 6h7v7H6zM3 3h7v2M3 3v7h2',
  link: 'M6.5 9.5l3-3M6 11a2.5 2.5 0 01-.5-3.9l1-1M10 5a2.5 2.5 0 01.5 3.9l-1 1',
  globe: 'M8 2.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM2.5 8h11M8 2.5c1.6 2 1.6 9 0 11M8 2.5c-1.6 2-1.6 9 0 11',
  book: 'M3.5 3.5h4v9h-4zM8.5 3.5h4v9h-4z',
  paperclip: 'M10.5 6.5l-3.7 3.7a1.6 1.6 0 002.3 2.3l4-4a3 3 0 00-4.2-4.2L4.4 8.8a4 4 0 005.7 5.7',
  file: 'M4.5 2.5h4.5l3 3v8h-7.5zM9 2.5v3h3',
  'file-pdf': 'M4.5 2.5h4.5l3 3v8h-7.5zM9 2.5v3h3M6 8.5h4M6 11h2.5',
  'file-doc': 'M4.5 2.5h4.5l3 3v8h-7.5zM9 2.5v3h3M6 8.5h1M8 8.5h2M6 11h4',
}
</script>

<template>
  <svg
    :width="props.size"
    :height="props.size"
    viewBox="0 0 16 16"
    fill="none"
    stroke="currentColor"
    stroke-width="1.6"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
  >
    <path :d="PATHS[props.name]" />
  </svg>
</template>
