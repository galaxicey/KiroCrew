import { describe, it, expect } from 'vitest'
// Both from `utils/diffLineCounts`, not from the component/page that re-exports
// them: importing those pulled the Pierre diff runtime, framer-motion,
// react-markdown, katex and highlight.js into this fork — measured 144.65s, of
// which 51ms was the tests.
import { createTwoFilesPatch } from 'diff'
import { countLines, countDiffStats, changedLineSpan, splitPatchSections } from '../utils/diffLineCounts'

describe('countLines (diff stats)', () => {
  it('returns zeros for identical content', () => {
    expect(countLines('hello', 'hello')).toEqual({ added: 0, removed: 0 })
  })

  it('counts added lines for new file', () => {
    const { added, removed } = countLines('', 'line1\nline2\nline3')
    expect(added).toBe(3)
    expect(removed).toBe(0)
  })

  it('counts removed lines for deleted content', () => {
    const { added, removed } = countLines('line1\nline2\nline3', '')
    expect(added).toBe(0)
    expect(removed).toBe(3)
  })

  it('counts both added and removed for modifications', () => {
    const { added, removed } = countLines('old1\nold2\nkeep', 'keep\nnew1\nnew2\nnew3')
    expect(added).toBeGreaterThan(0)
    expect(removed).toBeGreaterThan(0)
  })

  it('handles single line change', () => {
    const { added, removed } = countLines('before', 'after')
    expect(added).toBe(1)
    expect(removed).toBe(1)
  })
})

// Test the countDiffStats from ActivityViewer (parsing unified diff output)
describe('countDiffStats (unified diff parsing)', () => {

  it('returns zeros for empty diff', () => {
    expect(countDiffStats('')).toEqual({ added: 0, removed: 0 })
  })

  it('counts added lines from unified diff', () => {
    const diff = `--- a/file.ts
+++ b/file.ts
@@ -1,3 +1,4 @@
 keep
+new line 1
+new line 2
 keep2`
    expect(countDiffStats(diff)).toEqual({ added: 2, removed: 0 })
  })

  it('counts removed lines from unified diff', () => {
    const diff = `--- a/file.ts
+++ b/file.ts
@@ -1,4 +1,2 @@
 keep
-removed 1
-removed 2
 keep2`
    expect(countDiffStats(diff)).toEqual({ added: 0, removed: 2 })
  })

  it('counts both added and removed', () => {
    const diff = `--- a/file.ts
+++ b/file.ts
@@ -1,3 +1,3 @@
 keep
-old line
+new line
 keep2`
    expect(countDiffStats(diff)).toEqual({ added: 1, removed: 1 })
  })

  it('ignores --- and +++ header lines', () => {
    const diff = `--- a/file.ts
+++ b/file.ts
@@ -1 +1 @@
-old
+new`
    expect(countDiffStats(diff)).toEqual({ added: 1, removed: 1 })
  })

  // Headers are found by POSITION, never by prefix: content can start with the
  // header prefixes too, and the diff worker's own producer marks it that way.
  it('counts a removed `-- comment` line the way createTwoFilesPatch emits it (`--- comment`)', () => {
    const before = 'SELECT 1;\n-- retired note\nSELECT 2;\n'
    const after = 'SELECT 1;\nSELECT 2;\n'
    const patch = createTwoFilesPatch('q.sql', 'q.sql', before, after, undefined, undefined, { context: 3 })
    expect(patch).toContain('\n--- retired note\n') // the producer's shape this guards
    expect(countDiffStats(patch)).toEqual({ added: 0, removed: 1 })
  })

  it('counts content lines that begin with the header prefixes inside a hunk', () => {
    const diff = `--- a/doc.md
+++ b/doc.md
@@ -1,4 +1,4 @@
 title: x
----
+--- rule
-i++
++++i
 end`
    // `----` is a removed markdown rule, `+++i` an added C-style increment.
    expect(countDiffStats(diff)).toEqual({ added: 2, removed: 2 })
  })

  it('does not count the second file header pair of a multi-file diff', () => {
    const diff = `diff --git a/one.ts b/one.ts
--- a/one.ts
+++ b/one.ts
@@ -1,2 +1,2 @@
 keep
-old one
+new one
diff --git a/two.ts b/two.ts
--- a/two.ts
+++ b/two.ts
@@ -1 +1 @@
-old two
+new two`
    expect(countDiffStats(diff)).toEqual({ added: 2, removed: 2 })
  })

  it('keeps the prefix rule for a fence with no hunk header', () => {
    expect(countDiffStats('-old\n+new\n+newer')).toEqual({ added: 2, removed: 1 })
    expect(countDiffStats('--- a\n+++ b\n-old\n+new')).toEqual({ added: 1, removed: 1 })
  })
})

describe('splitPatchSections (one section per file)', () => {
  const summary = (diff: string) =>
    splitPatchSections(diff).map(({ name, prevName, added, removed }) => ({ name, prevName, added, removed }))

  it('cuts a git multi-file diff at each `diff --git` preamble, keeping every line', () => {
    const one = `diff --git a/one.ts b/one.ts
index 1111111..2222222 100644
--- a/one.ts
+++ b/one.ts
@@ -1,2 +1,2 @@
 keep
-old one
+new one`
    const two = `diff --git a/two.ts b/two.ts
--- a/two.ts
+++ b/two.ts
@@ -1 +1 @@
-old two
+new two
`
    const sections = splitPatchSections(one + '\n' + two)
    expect(sections.map(s => s.text)).toEqual([one, two])
    expect(summary(one + '\n' + two)).toEqual([
      { name: 'one.ts', prevName: null, added: 1, removed: 1 },
      { name: 'two.ts', prevName: null, added: 1, removed: 1 },
    ])
  })

  it('cuts a preamble-less diff at the next header pair once the hunk is spent', () => {
    const diff = `--- a.py
+++ a.py
@@ -1 +1 @@
-x = 1
+x = 2
--- b.py
+++ b.py
@@ -1,2 +1 @@
 keep
-gone`
    expect(splitPatchSections(diff).map(s => s.text.split('\n')[0])).toEqual(['--- a.py', '--- b.py'])
    expect(summary(diff)).toEqual([
      { name: 'a.py', prevName: null, added: 1, removed: 1 },
      { name: 'b.py', prevName: null, added: 0, removed: 1 },
    ])
  })

  it('names an addition, a deletion and a rename the way their headers do', () => {
    const diff = `--- /dev/null
+++ b/added.ts
@@ -0,0 +1 @@
+new
--- a/gone.ts
+++ /dev/null
@@ -1 +0,0 @@
-old
--- a/before.ts\t2026-01-01 00:00:00
+++ b/after.ts\t2026-01-02 00:00:00
@@ -1 +1 @@
-x
+y`
    expect(summary(diff)).toEqual([
      { name: 'added.ts', prevName: null, added: 1, removed: 0 },
      { name: 'gone.ts', prevName: null, added: 0, removed: 1 },
      { name: 'after.ts', prevName: 'before.ts', added: 1, removed: 1 },
    ])
  })

  it('does not cut at header-shaped content inside a hunk body', () => {
    const diff = `--- a/doc.md
+++ b/doc.md
@@ -1,4 +1,4 @@
 title: x
----
+--- rule
-i++
++++i
 end`
    expect(summary(diff)).toEqual([{ name: 'doc.md', prevName: null, added: 2, removed: 2 }])
  })

  it('does not cut at a lone `--- ` line past a miscounted hunk, only at a header pair', () => {
    // The hunk declares one old line but removes two; the second, `-- note`,
    // is content that a prefix rule would read as the next file's header.
    const diff = `--- a/q.sql
+++ b/q.sql
@@ -1,1 +1,1 @@
-SELECT 1;
--- note
+SELECT 2;`
    expect(splitPatchSections(diff)).toHaveLength(1)
  })

  it('treats a fence with no hunk header as one section, named by its headers if any', () => {
    expect(summary('-old\n+new\n+newer')).toEqual([{ name: null, prevName: null, added: 2, removed: 1 }])
    expect(summary('--- a/x.ts\n+++ b/x.ts\n-old\n+new')).toEqual([{ name: 'x.ts', prevName: null, added: 1, removed: 1 }])
    expect(splitPatchSections('')).toEqual([{ text: '', name: null, prevName: null, added: 0, removed: 0 }])
  })
})

describe('changedLineSpan (bounded change locality)', () => {
  const lines = (arr: string[]) => arr
  it('returns null for identical content', () => {
    expect(changedLineSpan(['a', 'b', 'c'], ['a', 'b', 'c'])).toBeNull()
  })

  it('finds a single deep edit as a one-line span on both sides', () => {
    const before = Array.from({ length: 1000 }, (_, i) => `L${i}`)
    const after = [...before]
    after[869] = 'L869 changed'
    expect(changedLineSpan(before, after)).toEqual({ oldStart: 869, oldEnd: 870, newStart: 869, newEnd: 870 })
  })

  it('covers scattered edits with one outer span', () => {
    const before = lines(['a', 'b', 'c', 'd', 'e'])
    const after = lines(['a', 'B', 'c', 'D', 'e'])
    // First diff at index 1, last diff at index 3 -> [1,4) on both sides.
    expect(changedLineSpan(before, after)).toEqual({ oldStart: 1, oldEnd: 4, newStart: 1, newEnd: 4 })
  })

  it('handles a pure insertion (empty removed range on the old side)', () => {
    const before = lines(['a', 'b', 'c'])
    const after = lines(['a', 'x', 'y', 'b', 'c'])
    const span = changedLineSpan(before, after)!
    // Common prefix 'a' (1), common suffix 'b','c' (2): old range is empty, new range is the two inserts.
    expect(span.oldStart).toBe(1)
    expect(span.oldEnd).toBe(1)
    expect(span.newStart).toBe(1)
    expect(span.newEnd).toBe(3)
  })

  it('handles append at end and prepend at start', () => {
    expect(changedLineSpan(['a', 'b'], ['a', 'b', 'c'])).toEqual({ oldStart: 2, oldEnd: 2, newStart: 2, newEnd: 3 })
    expect(changedLineSpan(['b', 'c'], ['a', 'b', 'c'])).toEqual({ oldStart: 0, oldEnd: 0, newStart: 0, newEnd: 1 })
  })

  it('treats a whole-file replacement as a full-range span', () => {
    expect(changedLineSpan(['a', 'b'], ['x', 'y', 'z'])).toEqual({ oldStart: 0, oldEnd: 2, newStart: 0, newEnd: 3 })
  })
})
