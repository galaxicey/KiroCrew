/**
 * A chat diff block's Open / Copy controls must be reachable for the block's
 * whole life, whatever the highlight worker pool does after the diff painted.
 *
 * Harness: the REAL `components/DiffBlock.tsx`, `pierre/index.tsx`,
 * `pierre/PierreImpl.tsx` and `pierre/workerPoolLifecycle.ts` run, so the code
 * that decides who draws the block's header — and how Pierre's patch surface
 * reacts to a pool that fails after paint — is the code under test. Only the
 * library's imperative renderers are stubbed (`@pierre/diffs/react` builds
 * custom elements and shadow roots that cannot mount here); the stub records
 * the options Pierre is handed and renders a header slot only when told to,
 * the way `renderDiffChildren` does. `Worker` is a fake that stands in for the
 * highlight workers, so a test can kill one and drive the real lifecycle into
 * its recovery phase — the state in which `PierrePatchImpl` drops to
 * header-less plain text.
 *
 * Nothing lays out here, so `scrollHeight` is 0 everywhere and a
 * `ResizeObserver` never fires; both are stubbed with the geometry a browser
 * reports so `WarmSwap` reveals the impl the way it does on screen. Controls
 * inside a hidden warm-up box are not counted: a reader cannot reach them.
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'

const state = vi.hoisted(() => ({
  /** Pierre's highlight workers, so a test can fail the pool AFTER paint. */
  highlightWorkers: [] as Array<{ emit: (type: string, event: unknown) => void }>,
  /** Live ResizeObserver callbacks, fired by the test when geometry changes. */
  resizeCallbacks: [] as Array<() => void>,
  /** Options the mock FileDiff was last rendered with. */
  lastFileDiffOptions: undefined as Record<string, unknown> | undefined,
  /** What the Copy control handed to the clipboard. */
  copied: [] as string[],
}))

/** Stands in for Pierre's highlight workers; nothing here answers a protocol. */
class FakeWorker {
  listeners = new Map<string, Set<(event: unknown) => void>>()

  constructor(readonly url: URL | string, readonly options?: WorkerOptions) {
    state.highlightWorkers.push(this)
  }

  addEventListener(type: string, listener: (event: unknown) => void) {
    const l = this.listeners.get(type) ?? new Set()
    l.add(listener)
    this.listeners.set(type, l)
  }

  removeEventListener(type: string, listener: (event: unknown) => void) {
    this.listeners.get(type)?.delete(listener)
  }

  emit(type: string, event: unknown) {
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }

  postMessage() {}

  terminate() {}
}

vi.mock('@pierre/diffs/worker', () => ({
  WorkerPoolManager: class {
    terminate = vi.fn()
    constructor(poolOptions: { poolSize: number; workerFactory: () => unknown }) {
      Array.from({ length: poolOptions.poolSize }, () => poolOptions.workerFactory())
    }
    initialize() { return Promise.resolve() }
  },
}))

/** The library's React layer, reduced to what the slot contract guarantees:
 *  a header slot is rendered when its renderer returns non-null (and the
 *  header is enabled), exactly as `renderDiffChildren` does. */
vi.mock('@pierre/diffs/react', async () => {
  const { createContext } = await import('react')
  const slots = (props: Record<string, unknown>) => {
    const options = (props.options ?? {}) as Record<string, unknown>
    if (options.disableFileHeader === true) return null
    const fn = props.renderHeaderMetadata
    const metadata = typeof fn === 'function' ? (fn as () => ReactNode)() : null
    return (
      <div data-diffs-header="" data-testid="pierre-own-header">
        {metadata != null && <div data-slot="header-metadata">{metadata}</div>}
      </div>
    )
  }
  return {
    File: () => <div data-testid="pierre-file" />,
    FileDiff: (props: Record<string, unknown>) => {
      state.lastFileDiffOptions = props.options as Record<string, unknown> | undefined
      return (
        <div data-testid="pierre-patch">
          {slots(props)}
          <span data-testid="pierre-patch-hunks">hunks</span>
        </div>
      )
    },
    MultiFileDiff: () => <div data-testid="pierre-pair" />,
    Virtualizer: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    WorkerPoolContext: createContext<unknown>(undefined),
  }
})

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: (text: string) => {
    state.copied.push(text)
    return Promise.resolve(true)
  },
}))

const PATCH = `--- a/file.ts
+++ b/file.ts
@@ -1,3 +1,4 @@
 const a = 1
-const b = 2
+const b = 3
+const c = 4
 const d = 5`

const OPEN_NAME = /^Open .* in side panel$/

/** Two files as `git diff` writes them: a modification, then a rename that also
 *  changes the basename and adds a line. */
const MULTI_FILE_PATCH = `diff --git a/src/a.ts b/src/a.ts
index 1111111..2222222 100644
--- a/src/a.ts
+++ b/src/a.ts
@@ -1,2 +1,2 @@
-const a = 1
+const a = 2
 export { a }
diff --git a/src/old.ts b/src/new.ts
similarity index 90%
rename from src/old.ts
rename to src/new.ts
--- a/src/old.ts
+++ b/src/new.ts
@@ -1,1 +1,2 @@
 const n = 1
+const m = 2`

/** Controls the reader can actually reach: a warm-up box mounts the impl inside
 *  an `aria-hidden` zero-height box until it paints, so counting every match
 *  would count a control nobody can click. */
const reachable = (els: HTMLElement[]) => els.filter(el => el.closest('[aria-hidden="true"]') == null)
const reachableCopy = () => reachable(screen.queryAllByTitle('Copy patch'))
const reachableOpen = () => reachable(screen.queryAllByTitle(OPEN_NAME))

/** Header rows the reader can see — the invariant is exactly one, always. */
const visibleHeaders = (container: HTMLElement) =>
  [...container.querySelectorAll<HTMLElement>('[data-diffs-header]')].filter(el => el.closest('[aria-hidden="true"]') == null)

async function loadDiffBlock() {
  const { default: DiffBlock } = await import('../components/DiffBlock')
  return DiffBlock
}

/** Geometry the warm-up measures, modelled on what a browser reports and
 *  ADDITIVE like a real box: a plain-text body has its own height, and diff
 *  rows are tall once the highlight worker has answered. */
const PAINTED_PX = 240
const TEXT_PX = 120
let scrollHeightSpy: ReturnType<typeof vi.spyOn> | undefined

function stubScrollHeight() {
  scrollHeightSpy = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) {
    let h = 0
    if (this.querySelector('pre')) h += TEXT_PX
    if (this.querySelector('[data-testid="pierre-patch-hunks"]')) h += PAINTED_PX
    return h
  })
}

/** A browser's ResizeObserver fires when the observed box changes size; here
 *  the test fires the LIVE observers once the content they wait on has
 *  mounted. */
const fireResize = () => act(() => { for (const cb of [...state.resizeCallbacks]) cb() })

class FakeResizeObserver {
  constructor(private readonly cb: () => void) {}
  observe() { if (!state.resizeCallbacks.includes(this.cb)) state.resizeCallbacks.push(this.cb) }
  unobserve() { this.disconnect() }
  disconnect() {
    const i = state.resizeCallbacks.indexOf(this.cb)
    if (i >= 0) state.resizeCallbacks.splice(i, 1)
  }
}

beforeEach(() => {
  state.highlightWorkers.length = 0
  state.resizeCallbacks.length = 0
  state.lastFileDiffOptions = undefined
  state.copied.length = 0
  vi.resetModules()
  vi.stubGlobal('Worker', FakeWorker)
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  // The Open affordance is offered once a HEAD probe says the file exists.
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true })))
  stubScrollHeight()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.spyOn(console, 'error').mockImplementation(() => {})
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  scrollHeightSpy?.mockRestore()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

/** Mount a complete, within-budget block and let Pierre paint it. */
async function renderPainted(onFileOpen: (path: string) => void) {
  const DiffBlock = await loadDiffBlock()
  const view = render(<DiffBlock code={PATCH} complete onFileOpen={onFileOpen} />)
  expect(await screen.findByTestId('pierre-patch')).toBeInTheDocument()
  fireResize()
  await vi.waitFor(() => {
    expect(reachableCopy()).toHaveLength(1)
    expect(reachableOpen()).toHaveLength(1)
  })
  expect(state.highlightWorkers.length).toBeGreaterThan(0)
  return view
}

describe('diff block: the header survives the highlight pool', () => {
  it('keeps Open and Copy reachable and working after the pool fails post-paint', async () => {
    const onFileOpen = vi.fn()
    const { container } = await renderPainted(onFileOpen)
    expect(visibleHeaders(container)).toHaveLength(1)

    // A highlight worker dies AFTER the diff painted: the lifecycle publishes
    // `recovering` with no generation and Pierre's patch surface drops to
    // header-less plain text — the recover path.
    act(() => { state.highlightWorkers[0].emit('error', { message: 'boom' }) })
    expect(screen.queryByTestId('pierre-patch')).not.toBeInTheDocument()
    expect(container.textContent).toContain('@@ -1,3 +1,4 @@')

    // The block's controls are not Pierre's to take away.
    expect(reachableCopy()).toHaveLength(1)
    expect(reachableOpen()).toHaveLength(1)
    expect(visibleHeaders(container)).toHaveLength(1)

    // … and they still do their jobs.
    const user = userEvent.setup()
    await user.click(reachableOpen()[0])
    expect(onFileOpen).toHaveBeenCalledWith('file.ts')
    await user.click(reachableCopy()[0])
    expect(state.copied).toEqual([PATCH])
  })

  it('draws the header itself and tells Pierre to draw the body only', async () => {
    const { container } = await renderPainted(vi.fn())
    // One header, the block's own: Pierre receives `disableFileHeader: true`, so
    // there is no second header for the controls to vanish into when Pierre
    // has nothing to draw.
    expect(state.lastFileDiffOptions?.disableFileHeader).toBe(true)
    expect(screen.queryByTestId('pierre-own-header')).not.toBeInTheDocument()
    const headers = visibleHeaders(container)
    expect(headers).toHaveLength(1)
    // The row carries what Pierre's header carries elsewhere: the basename and
    // the exact ± counts read off the patch.
    expect(headers[0].querySelector('[data-title]')).toHaveTextContent('file.ts')
    expect(headers[0].querySelector('[data-deletions-count]')).toHaveTextContent('-1')
    expect(headers[0].querySelector('[data-additions-count]')).toHaveTextContent('+2')
    // The controls sit in that row, not inside Pierre's surface.
    expect(reachableCopy()[0].closest('[data-diffs-header]')).toBe(headers[0])
  })

  /** A patch may carry several files: Pierre's own headers named each one, with
   *  its own counts and a rename's old name. The block's rows must, too — one
   *  per file, the patch-level controls on the first — and stay through a pool
   *  death exactly like the single-file row. */
  it('gives every file of a multi-file patch its own row, names a rename by both files, and keeps the rows through a pool death', async () => {
    const DiffBlock = await loadDiffBlock()
    const { container } = render(<DiffBlock code={MULTI_FILE_PATCH} complete onFileOpen={vi.fn()} />)
    expect(await screen.findAllByTestId('pierre-patch')).toHaveLength(2)
    fireResize()
    await vi.waitFor(() => expect(reachableCopy()).toHaveLength(1))

    const rows = () => visibleHeaders(container)
    const titles = () => rows().map(row => row.querySelector('[data-title]')?.textContent)
    const counts = (row: HTMLElement) => [
      row.querySelector('[data-deletions-count]')?.textContent ?? null,
      row.querySelector('[data-additions-count]')?.textContent ?? null,
    ]
    expect(titles()).toEqual(['a.ts', 'old.ts → new.ts'])
    expect(counts(rows()[0])).toEqual(['-1', '+1'])
    expect(counts(rows()[1])).toEqual([null, '+1'])
    // Open / Copy act on the whole patch, so they ride the first row only.
    expect(reachableOpen()).toHaveLength(1)
    expect(reachableCopy()[0].closest('[data-diffs-header]')).toBe(rows()[0])

    act(() => { state.highlightWorkers[0].emit('error', { message: 'boom' }) })
    expect(screen.queryByTestId('pierre-patch')).not.toBeInTheDocument()
    expect(titles()).toEqual(['a.ts', 'old.ts → new.ts'])
    expect(reachableCopy()).toHaveLength(1)
    expect(reachableOpen()).toHaveLength(1)
  })
})
