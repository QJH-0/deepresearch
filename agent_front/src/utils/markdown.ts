/**
 * 极简 Markdown -> HTML 渲染。
 *
 * 只覆盖研究助手实际会输出的语法：标题、列表、代码块、行内代码、
 * 粗斜体、链接、引用、表格分隔线。够用且不引入运行时依赖。
 *
 * 所有用户内容先做 HTML 转义再拼标签，避免 XSS。
 */

const escapeHtml = (value: string): string =>
  value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')

/** 行内语法：先转义再替换，顺序不能反 */
function inline(text: string): string {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^[\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>')
}

export function markdownToHtml(markdown: string): string {
  if (!markdown) return ''

  const codeBlocks: string[] = []
  // 先摘出代码块，避免块内内容被行内/块级规则误伤
  let text = markdown.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang: string, block: string) => {
    const index = codeBlocks.length
    const cls = lang ? ` class="language-${escapeHtml(lang)}"` : ''
    codeBlocks.push(`<pre><code${cls}>${escapeHtml(block.replace(/\n$/, ''))}</code></pre>`)
    return `@@CODE_BLOCK_${index}@@`
  })

  const lines = text.split('\n')
  const out: string[] = []
  let listType: 'ul' | 'ol' | null = null
  let inQuote = false

  const closeList = () => {
    if (listType) {
      out.push(listType === 'ul' ? '</ul>' : '</ol>')
      listType = null
    }
  }
  const closeQuote = () => {
    if (inQuote) {
      out.push('</blockquote>')
      inQuote = false
    }
  }
  const closeAll = () => {
    closeList()
    closeQuote()
  }

  for (const rawLine of lines) {
    const line = rawLine.trimEnd()

    if (!line.trim()) {
      closeAll()
      continue
    }

    // 分隔线
    if (/^(-{3,}|\*{3,})$/.test(line.trim())) {
      closeAll()
      out.push('<hr />')
      continue
    }

    // 代码块占位
    const placeholder = line.trim().match(/^@@CODE_BLOCK_(\d+)@@$/)
    if (placeholder) {
      closeAll()
      out.push(codeBlocks[Number(placeholder[1])] || '')
      continue
    }

    // 引用
    if (line.startsWith('> ')) {
      closeList()
      if (!inQuote) {
        out.push('<blockquote>')
        inQuote = true
      }
      out.push(`<p>${inline(line.slice(2))}</p>`)
      continue
    }
    closeQuote()

    // 标题
    const heading = line.match(/^(#{1,4})\s+(.*)$/)
    if (heading?.[1] && heading[2] !== undefined) {
      closeAll()
      const level = heading[1].length
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`)
      continue
    }

    // 有序列表
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/)
    if (ordered?.[1] !== undefined) {
      if (listType && listType !== 'ol') closeList()
      if (!listType) {
        out.push('<ol>')
        listType = 'ol'
      }
      out.push(`<li>${inline(ordered[1])}</li>`)
      continue
    }

    // 无序列表
    const unordered = line.match(/^\s*[-*+]\s+(.*)$/)
    if (unordered?.[1] !== undefined) {
      if (listType && listType !== 'ul') closeList()
      if (!listType) {
        out.push('<ul>')
        listType = 'ul'
      }
      out.push(`<li>${inline(unordered[1])}</li>`)
      continue
    }

    closeAll()
    out.push(`<p>${inline(line)}</p>`)
  }

  closeAll()
  return out.join('')
}
