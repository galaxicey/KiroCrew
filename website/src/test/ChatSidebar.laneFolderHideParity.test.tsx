/**
 * Every lane that renders session rows honours the person's folder hide.
 *
 * The filter menu's folder checkboxes are the person saying "do not show me this
 * folder". That is a stronger statement than the status, tag and search chips: those
 * decide what a lane is ABOUT, so a row they exclude can still appear as a dimmed
 * context anchor, while a folder the person unchecked must put no session row on
 * screen at all. The one sanctioned way back in is the reveal row at the bottom of a
 * container, which is the person asking to look.
 *
 * The lanes are ENUMERATED OUT OF THE SOURCE rather than listed here, because the way
 * this defect comes back is a lane nobody has written yet. A new member of the
 * `SidebarLane` union with no entry in `LANE_COVERAGE` fails this file before it can
 * ship, which a hand-written list of today's lanes would not do.
 *
 * Both directions are asserted for every lane. "Hidden stays hidden" alone would pass
 * for an implementation that filtered every folder away, so each lane must also prove
 * that a folder the person did NOT hide still renders its sessions.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const { boardColumns } = vi.hoisted(() => ({
  boardColumns: [
    {
      id: 'col-idle', name: '', tag_ids: [] as string[], mode: 'any' as const,
      order: 0, source: 'state' as const, state_key: 'idle' as const,
    },
  ],
}))

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'tagColumns') return () => Promise.resolve(boardColumns)
      return vi.fn().mockResolvedValue([])
    },
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'
import type { RootState } from '../store'
import type { ChatFolder, ChatSlot } from '../types'

// ── the lane universe, read out of the component's own source ────────────────

const SIDEBAR_SRC = readFileSync(join(__dirname, '..', 'pages', 'ChatSidebar.tsx'), 'utf8')

/** The `SidebarLane` union's members, in declaration order. */
function declaredLanes(src: string): string[] {
  const m = /^type SidebarLane =([^\n]+)$/m.exec(src)
  if (!m) return []
  return m[1].split('|').map(s => s.trim().replace(/^'(.*)'$/, '$1')).filter(Boolean)
}

const DECLARED_LANES = declaredLanes(SIDEBAR_SRC)

/**
 * How to put each lane on screen.
 *
 * `board` is deliberately here without being a `SidebarLane`: it is a separate axis
 * that PREEMPTS the union's choice whenever columns are configured, so a lane-shaped
 * renderer that the union cannot name still has to answer for itself.
 */
const LANE_COVERAGE: Record<string, { lanePref: string; columns: boolean }> = {
  tree: { lanePref: 'tree', columns: false },
  flat: { lanePref: 'flat', columns: false },
  conductor: { lanePref: 'conductor', columns: false },
  board: { lanePref: 'tree', columns: true },
}

// ── fixture ─────────────────────────────────────────────────────────────────

const HIDDEN_FOLDER = 'folder-hidden'
const SHOWN_FOLDER = 'folder-shown'

const FOLDERS: ChatFolder[] = [
  { id: HIDDEN_FOLDER, name: 'hidden folder', collapsed: false, order: 0 },
  { id: SHOWN_FOLDER, name: 'shown folder', collapsed: false, order: 1 },
]

/**
 * A conductor inside the hidden folder with a child outside it.
 *
 * This shape is what makes the conductor lane's anchor rule reachable at all: the
 * child matches the filter, so the lane wants the ancestor it hangs from, and that
 * ancestor is exactly the row the person hid.
 */
const SLOTS: ChatSlot[] = [
  { key: 'k-hidden-conductor', title: 'Hidden Conductor', running: false, messages: 2, modified: 3000, folder_id: HIDDEN_FOLDER },
  { key: 'k-shown-child', title: 'Shown Child', running: false, messages: 2, modified: 2000, folder_id: SHOWN_FOLDER, parent: { slot: 'k-hidden-conductor', key: 'k-hidden-conductor' } },
  { key: 'k-shown-plain', title: 'Shown Plain', running: false, messages: 2, modified: 1000, folder_id: SHOWN_FOLDER },
] as unknown as ChatSlot[]

function renderSidebar(withColumns: boolean) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], FOLDERS)
  qc.setQueryData(['tag-columns'], withColumns ? boardColumns : [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  // The stale collapse would fold settled rows behind an expander, which would make a
  // reverse assertion fail for a reason that has nothing to do with folder hiding.
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
})
afterEach(() => vi.clearAllMocks())

/** Seed the persisted hidden-folder set the filter checkboxes write to. */
function hideFolders(...ids: string[]) {
  localStorage.setItem('mc-flat-hidden-folders', JSON.stringify(ids))
}

// ── the enumeration itself ──────────────────────────────────────────────────

describe('sidebar lanes honour the folder hide — enumerated from source', () => {
  it('parses the lane union out of the source', () => {
    // Control for the two assertions below: a regex that silently matched nothing
    // would make "every declared lane is covered" vacuously true, so the parse has to
    // prove it found the real union first.
    expect(DECLARED_LANES.length).toBeGreaterThanOrEqual(3)
    expect(DECLARED_LANES).toContain('conductor')
  })

  it('covers every lane the source declares', () => {
    for (const lane of DECLARED_LANES) {
      expect(
        Object.prototype.hasOwnProperty.call(LANE_COVERAGE, lane),
        `SidebarLane declares '${lane}' but this file never renders it. A lane that renders `
        + 'session rows has to prove it honours the folder hide; add it to LANE_COVERAGE.',
      ).toBe(true)
    }
  })

  it('still preempts the union with the board lane', () => {
    // Why `board` is in LANE_COVERAGE without being a SidebarLane. If this gate moves,
    // the board entry is either misnamed or no longer reachable, and the reader of this
    // file needs to know which.
    expect(SIDEBAR_SRC).toMatch(/conductorLaneActive\s*=\s*!boardLaneActive/)
  })
})

describe.each(Object.entries(LANE_COVERAGE))(
  'the %s lane',
  (laneName, setup) => {
    it('does not render a session whose folder the person hid', () => {
      hideFolders(HIDDEN_FOLDER)
      localStorage.setItem('mc-sidebar-lane', setup.lanePref)
      const { queryByText } = renderSidebar(setup.columns)
      expect(
        queryByText('Hidden Conductor'),
        `the ${laneName} lane rendered a session from a folder the person unchecked`,
      ).toBeNull()
    })

    it('renders sessions whose folder the person left alone', () => {
      // The other direction. Without it, filtering every folder away would pass.
      hideFolders(HIDDEN_FOLDER)
      localStorage.setItem('mc-sidebar-lane', setup.lanePref)
      const { getByText } = renderSidebar(setup.columns)
      expect(getByText('Shown Plain')).toBeTruthy()
      // The hidden conductor's child is filed OUTSIDE the hide, so concealing its
      // parent must not take it down too -- it stands on its own instead.
      expect(getByText('Shown Child')).toBeTruthy()
    })

    it('renders every session when the person hid nothing', () => {
      // The control for the forward assertion: it proves 'Hidden Conductor' is
      // renderable in this lane at all, so its absence above is the hide doing work
      // and not a fixture this lane was never going to show.
      localStorage.setItem('mc-sidebar-lane', setup.lanePref)
      const { getByText } = renderSidebar(setup.columns)
      expect(getByText('Hidden Conductor')).toBeTruthy()
      expect(getByText('Shown Child')).toBeTruthy()
      expect(getByText('Shown Plain')).toBeTruthy()
    })
  },
)
