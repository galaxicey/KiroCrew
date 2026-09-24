/**
 * `credential_redaction_changed` (owner sockets only): the owner flipped the
 * credential-redaction switch, possibly in ANOTHER browser tab. This document
 * must re-read the switch and drop every file body it holds -- react-query
 * `['file-read', path]` / `['file-diff', path]` and open side-panel tab bodies --
 * so no dashboard document keeps showing raw credentials after redaction is
 * back on.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    credentialRedaction: vi.fn(),
  },
}))

const WS_INSTANCES: MockWebSocket[] = []
class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()
  constructor() { WS_INSTANCES.push(this) }
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateMessage(data: object) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) })) }
}

describe('useWebSocket credential_redaction_changed frame', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient
  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    __resetPanelTabs()
  })
  afterEach(() => { vi.unstubAllGlobals() })
  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  async function reconnectWith(opts: { held?: { enabled: boolean }; server: { enabled: boolean } }) {
    qc.setQueryData(['file-read', '/tmp/raw.txt'], { content: 'AKIA-raw-while-off' })
    if (opts.held) qc.setQueryData(['credential-redaction'], { ...opts.held, changed_at: '' })
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue({ ...opts.server, changed_at: '' })
    const tabs = renderHook(() => usePanelTabs('slot-a', []))
    act(() => tabs.result.current.openFile('/tmp/raw.txt', 'AKIA-raw-while-off', 'slot-a'))
    act(() => tabs.result.current.openDiff('/tmp/raw.txt', 'a', 'b'))
    renderHook(() => useWebSocket(), { wrapper })
    const first = WS_INSTANCES[0]
    act(() => { first.simulateOpen() })
    act(() => { first.onclose?.(new CloseEvent('close')) })
    const second = WS_INSTANCES[WS_INSTANCES.length - 1]
    act(() => { second.simulateOpen() })
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    return tabs
  }

  it('a RECONNECT purges when the switch moved while the socket was down (the push has no replay)', async () => {
    const tabs = await reconnectWith({ held: { enabled: false }, server: { enabled: true } })
    expect(qc.getQueryData(['file-read', '/tmp/raw.txt'])).toBeUndefined()
    expect(tabs.result.current.tabs.find(t => t.id === 'file:/tmp/raw.txt')?.content).toBeUndefined()
    expect(tabs.result.current.tabs.some(t => t.kind === 'diff')).toBe(false)
  })

  it('a RECONNECT with the switch unmoved leaves diff tabs and file bodies alone', async () => {
    const tabs = await reconnectWith({ held: { enabled: true }, server: { enabled: true } })
    expect(qc.getQueryData(['file-read', '/tmp/raw.txt'])).toEqual({ content: 'AKIA-raw-while-off' })
    expect(tabs.result.current.tabs.find(t => t.id === 'file:/tmp/raw.txt')?.content).toBe('AKIA-raw-while-off')
    expect(tabs.result.current.tabs.some(t => t.kind === 'diff')).toBe(true)
  })

  it('a document that never read the switch purges on reconnect when the switch is now ON', async () => {
    // A file opened raw while OFF may be on screen without Settings ever having
    // been visited; only this re-read can catch the flip made elsewhere.
    const tabs = await reconnectWith({ server: { enabled: true } })
    expect(api.credentialRedaction).toHaveBeenCalledTimes(1)
    expect(qc.getQueryData(['file-read', '/tmp/raw.txt'])).toBeUndefined()
    expect(tabs.result.current.tabs.some(t => t.kind === 'diff')).toBe(false)
  })

  it('a document that never read the switch is left alone when the switch is still OFF', async () => {
    const tabs = await reconnectWith({ server: { enabled: false } })
    expect(tabs.result.current.tabs.some(t => t.kind === 'diff')).toBe(true)
    expect(qc.getQueryData(['file-read', '/tmp/raw.txt'])).toEqual({ content: 'AKIA-raw-while-off' })
  })

  it('re-reads the switch and drops every cached file body plus the open tab bodies', () => {
    qc.setQueryData(['file-read', '/tmp/raw.txt'], { content: 'AKIA-raw-while-off' })
    qc.setQueryData(['file-diff', '/tmp/raw.txt'], { diff: '', original: 'AKIA-raw-while-off', status: 'clean' })
    qc.setQueryData(['credential-redaction'], { enabled: false, changed_at: '2026-01-01T00:00:00Z' })
    const tabs = renderHook(() => usePanelTabs('slot-a', []))
    act(() => tabs.result.current.openFile('/tmp/raw.txt', 'AKIA-raw-while-off', 'slot-a'))
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => { ws.simulateMessage({ type: 'credential_redaction_changed', data: { enabled: true, changed_at: '2026-01-01T00:01:00Z' } }) })
    const keys = invalidateSpy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['credential-redaction']))
    // The frame's payload seeds the switch entry, so the reconnect heal can compare later.
    expect(qc.getQueryData(['credential-redaction'])).toEqual({ enabled: true, changed_at: '2026-01-01T00:01:00Z' })
    expect(qc.getQueryData(['file-read', '/tmp/raw.txt'])).toBeUndefined()
    expect(qc.getQueryData(['file-diff', '/tmp/raw.txt'])).toBeUndefined()
    // The open tab body is gone too: it rehydrates through /api/file-read.
    expect(tabs.result.current.tabs.find(t => t.id === 'file:/tmp/raw.txt')?.content).toBeUndefined()
  })
})
