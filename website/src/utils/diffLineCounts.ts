/**
 * Pure line-counting and patch-sectioning helpers shared by the diff surfaces.
 *
 * Their own module rather than members of `components/FileChangeChips.tsx` and
 * `pages/chat/ActivityViewer.tsx` so a pure test can reach them without
 * importing either: both pull the Pierre diff runtime, framer-motion,
 * react-markdown, katex and highlight.js into the importing fork, which line
 * arithmetic over two strings has no reason to pay for.
 */

/**
 * Line-level diff count via LCS — correctly attributes moves as +N/-N
 * (a moved line shows up as a removal at the old position and an addition
 * at the new). Falls back to a cheap multiset count for huge files to bound
 * cost; that fallback can under-report pure moves but only on files we
 * already cap at 200KB, so the cap is rarely hit in practice.
 */
export function countLines(before: string, after: string): { added: number; removed: number } {
  if (before === after) return { added: 0, removed: 0 }
  // Guard empty strings: ''.split('\n') yields [''] (1 phantom line), which would
  // mis-count a new file as +1/-1 instead of +1, and a fully cleared file as
  // +1/-2 instead of -2. Treat empty content as zero lines.
  const a = before ? before.split('\n') : []
  const b = after ? after.split('\n') : []
  const m = a.length, n = b.length
  // LCS with rolling rows: O(mn) time, O(min(m,n)) space.
  // 1M cell cap = ~1000x1000 lines which covers anything inside our 200KB snapshot cap comfortably.
  if (m * n <= 1_000_000) {
    let prev = new Int32Array(n + 1)
    let curr = new Int32Array(n + 1)
    for (let i = 1; i <= m; i++) {
      for (let j = 1; j <= n; j++) {
        if (a[i - 1] === b[j - 1]) curr[j] = prev[j - 1] + 1
        else curr[j] = prev[j] >= curr[j - 1] ? prev[j] : curr[j - 1]
      }
      const tmp = prev; prev = curr; curr = tmp
      curr.fill(0)
    }
    const lcs = prev[n]
    return { added: n - lcs, removed: m - lcs }
  }
  // Huge-file fallback: multiset count. Cheap but doesn't detect pure moves.
  const aMap = new Map<string, number>()
  const bMap = new Map<string, number>()
  for (const line of a) aMap.set(line, (aMap.get(line) || 0) + 1)
  for (const line of b) bMap.set(line, (bMap.get(line) || 0) + 1)
  let added = 0, removed = 0
  for (const [line, count] of bMap) {
    const aCount = aMap.get(line) || 0
    if (count > aCount) added += count - aCount
  }
  for (const [line, count] of aMap) {
    const bCount = bMap.get(line) || 0
    if (count > bCount) removed += count - bCount
  }
  return { added, removed }
}

/**
 * Added/removed counts read straight off a UNIFIED DIFF's own markers, for a
 * surface that already holds a patch rather than the before/after pair
 * `countLines` needs. The whole patch's totals: `splitPatchSections` is the
 * walk, and this sums its per-file counts.
 */
export function countDiffStats(diff: string): { added: number; removed: number } {
  let added = 0, removed = 0
  for (const section of splitPatchSections(diff)) {
    added += section.added
    removed += section.removed
  }
  return { added, removed }
}

/** One file of a unified diff: the run of lines from where the patch says a
 *  file begins — its `diff --git` / `Index:` preamble, or its `---`/`+++`
 *  header pair when it has no preamble — up to where the next file begins. */
export interface PatchSection {
  /** The section's lines, verbatim: preamble, header pair and hunks. A section
   *  is itself a unified diff of one file, so it renders wherever the whole
   *  patch would. */
  text: string
  /** The file the section is about: the `+++` side with git's `b/` marker
   *  removed, or the `---` side for a deletion (`+++ /dev/null`). `null` when
   *  the text names none — a bare hunk, a fence of `+`/`-` lines. */
  name: string | null
  /** The `---` side (git's `a/` marker removed) when it names a different
   *  file than `name` — a rename. `null` otherwise. */
  prevName: string | null
  added: number
  removed: number
}

/**
 * Cut a unified diff into its per-file sections, each with the file it names
 * and its own added/removed counts.
 *
 * File headers are identified by POSITION, never by prefix: a `---`/`+++`
 * line is a header when it sits outside a hunk body — before the first `@@`,
 * or after a hunk has spent the line counts its `@@ -a,b +c,d @@` header
 * declared (the next file of a multi-file diff). Inside a hunk body a line is
 * counted by its first character alone, because content can begin with the
 * header prefixes too: `createTwoFilesPatch` marks a removed `-- comment`
 * (SQL, Lua, Haskell) or a removed YAML/markdown `---` rule as `--- comment` /
 * `----`, and an added `++i` as `+++i`. Skipping those by prefix under-counted
 * exactly the change they carry.
 *
 * A file begins at a `diff ` / `Index: ` preamble line, or — for the
 * preamble-less shape difflib emits — at a `---` header met once the current
 * section already holds its own header pair or a hunk. Lines before the first
 * file's header (a `diff --git` line, an `index` line) stay with that file.
 *
 * Text with no hunk header at all — a hand-pasted ```diff fence of bare `+`/`-`
 * lines — has no positions to go by, so it is one section counted by the
 * prefix rule: every `+`/`-` line counts except `+++`/`---`, which can only be
 * headers there.
 */
export function splitPatchSections(diff: string): PatchSection[] {
  const lines = diff.split('\n')
  let added = 0, removed = 0
  let name: string | null = null, prevName: string | null = null
  if (!lines.some(isHunkHeader)) {
    for (const line of lines) {
      if (line.startsWith('+') && !line.startsWith('+++')) added++
      else if (line.startsWith('-') && !line.startsWith('---')) removed++
      else if (line.startsWith('+++ ')) name = headerPath(line)
      else if (line.startsWith('--- ')) prevName = headerPath(line)
    }
    return [patchSection(diff, name, prevName, added, removed)]
  }
  const sections: PatchSection[] = []
  let start = 0
  // The section in progress already holds its header pair or a hunk, so a
  // `---` header met outside a hunk body opens the NEXT file rather than
  // naming this one.
  let opened = false
  const close = (end: number) => {
    sections.push(patchSection(lines.slice(start, end).join('\n'), name, prevName, added, removed))
    start = end
    name = prevName = null
    added = removed = 0
    opened = false
  }
  let inHunk = false
  // Lines the current hunk still owes per side, from its `@@` header. A `---`
  // or `+++` line while the side is spent is the next file's header; while
  // the side still has lines it is content that happens to start that way.
  let oldLeft = 0, newLeft = 0
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const hunk = HUNK_HEADER_RE.exec(line)
    if (hunk) {
      inHunk = true
      opened = true
      oldLeft = hunk[1] == null ? 1 : Number(hunk[1])
      newLeft = hunk[2] == null ? 1 : Number(hunk[2])
      continue
    }
    if (inHunk) {
      if (line.startsWith('\\')) continue // "\ No newline at end of file"
      if (line.startsWith('+')) {
        if (!(newLeft <= 0 && line.startsWith('+++'))) { added++; newLeft--; continue }
      } else if (line.startsWith('-')) {
        if (!(oldLeft <= 0 && line.startsWith('---'))) { removed++; oldLeft--; continue }
      } else if (!line.startsWith('diff ') && !line.startsWith('Index: ')) {
        oldLeft-- // a context line (or a blank one some emitters leave bare)
        newLeft--
        continue
      }
      // A spent side's `---`/`+++`, or the next file's preamble: the hunk is
      // over and this line is read as a header below.
      inHunk = false
    }
    if (line.startsWith('diff ') || line.startsWith('Index: ')) {
      if (opened) close(i)
    } else if (line.startsWith('--- ') && (lines[i + 1] ?? '').startsWith('+++ ')) {
      // Paired with its `+++`, the way a header always is: a lone `--- x` past
      // a hunk's declared count is a deleted `-- x` under a miscounted
      // header, and cutting a file there would hand the renderer a headless
      // half of it.
      if (opened) close(i)
      prevName = headerPath(line)
    } else if (line.startsWith('+++ ')) {
      name = headerPath(line)
      opened = true
    }
  }
  close(lines.length)
  return sections
}

/** The path a `--- ` / `+++ ` header names: the remainder up to the TAB that
 *  separates an optional timestamp, with git's `a/` / `b/` side marker
 *  removed. `null` for the placeholders of an added or deleted side
 *  (`/dev/null`, a bare `-` / `+`). */
function headerPath(line: string): string | null {
  const raw = line.slice(4).split('\t')[0]
  if (!raw || raw === '/dev/null' || raw === '-' || raw === '+') return null
  return /^[ab]\//.test(raw) ? raw.slice(2) : raw
}

function patchSection(text: string, name: string | null, prevName: string | null, added: number, removed: number): PatchSection {
  // A deletion names its file on the `---` side only; a modification names
  // the same file twice.
  if (name == null) return { text, name: prevName, prevName: null, added, removed }
  return { text, name, prevName: prevName === name ? null : prevName, added, removed }
}

/** `@@ -a[,b] +c[,d] @@…` — captures the two optional line counts. */
const HUNK_HEADER_RE = /^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@/
const isHunkHeader = (line: string) => HUNK_HEADER_RE.test(line)

/** The 0-based line span that differs between two texts, per side, or `null`
 *  when they are identical. `oldStart`/`oldEnd` index `before`'s lines and
 *  `newStart`/`newEnd` index `after`'s; each `*End` is exclusive.
 *
 *  This is a common-prefix / common-suffix walk, NOT a diff: it finds only the
 *  OUTER bounds of the change (first line that differs from the top, last line
 *  that differs from the bottom), so a scatter of edits reports one span that
 *  covers them all. That span is a locality region, not a row-level diff. Only
 *  its first and last non-empty rows are proven to differ; consumers must not
 *  give the interior add/remove semantics. The oversized fallback exists because
 *  a real line-level diff is too expensive to run on the renderer thread for
 *  these inputs (see `renderBudget`), and this stays cheap for the same reason:
 *  two pointer walks that stop at the first difference, no LCS, no allocation
 *  beyond the two line arrays the caller already holds. Its one job is to tell
 *  the fallback WHERE to look so it can anchor there instead of at line 1. */
export function changedLineSpan(
  beforeLines: readonly string[],
  afterLines: readonly string[],
): { oldStart: number; oldEnd: number; newStart: number; newEnd: number } | null {
  const m = beforeLines.length
  const n = afterLines.length
  let start = 0
  const max = Math.min(m, n)
  while (start < max && beforeLines[start] === afterLines[start]) start++
  if (start === m && start === n) return null // identical
  // Walk the common suffix, but never cross the common prefix on either side.
  let endBack = 0
  while (
    endBack < m - start
    && endBack < n - start
    && beforeLines[m - 1 - endBack] === afterLines[n - 1 - endBack]
  ) endBack++
  return { oldStart: start, oldEnd: m - endBack, newStart: start, newEnd: n - endBack }
}
