import { screen, waitFor, fireEvent } from '@testing-library/react'
import { Routes, Route, useLocation } from 'react-router-dom'
import { renderWithProviders } from '../test/helpers'
import UnsavedDefaultNotice from './UnsavedDefaultNotice'

const { dashboardConfigMock } = vi.hoisted(() => ({
  dashboardConfigMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: { dashboardConfig: dashboardConfigMock },
}))

/** Echoes the current URL so a navigation can be asserted without a page. */
function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location">{loc.pathname + loc.search}</div>
}

function mount(route = '/chat') {
  return renderWithProviders(
    <>
      <UnsavedDefaultNotice />
      <Routes>
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </>,
    { route },
  )
}

const forced = () => dashboardConfigMock.mockResolvedValue({
  default_memory_mode: 'temporary',
  default_memory_mode_forced: true,
})

describe('UnsavedDefaultNotice', () => {
  beforeEach(() => {
    dashboardConfigMock.mockReset()
  })

  it('reports the forced Temporary default through ErrorNotice, with the agent hand-off', async () => {
    forced()
    mount()
    const notice = await screen.findByTestId('unsaved-default-notice')
    // The shared error surface, not a hand-written status box: the forced
    // state is a config-read failure (AUTOSDE errors-use-error-notice).
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent(/New chats are not being saved/)
    expect(notice).toHaveTextContent(/config\.json could not be read/)
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  // A user who CHOSE Temporary made that choice on a Settings row that spells
  // out the consequence; a banner nagging a deliberate choice on every launch
  // is one the user learns to ignore.
  it('stays silent for a chosen non-persistent default', async () => {
    dashboardConfigMock.mockResolvedValue({
      default_memory_mode: 'temporary',
      default_memory_mode_forced: false,
    })
    mount()
    await waitFor(() => expect(dashboardConfigMock).toHaveBeenCalled())
    expect(screen.queryByTestId('unsaved-default-notice')).toBeNull()
  })

  it('stays silent when a gateway older than the field omits it', async () => {
    dashboardConfigMock.mockResolvedValue({ default_memory_mode: 'persistent' })
    mount()
    await waitFor(() => expect(dashboardConfigMock).toHaveBeenCalled())
    expect(screen.queryByTestId('unsaved-default-notice')).toBeNull()
  })

  // The Default Memory Mode row on that tab reports the same failure in its
  // own notice; two copies on one screen read as two problems.
  it('yields to the Settings row on the Chat settings tab', async () => {
    forced()
    mount('/settings/chat?highlight=key%3Adashboard.default_memory_mode')
    await waitFor(() => expect(dashboardConfigMock).toHaveBeenCalled())
    expect(screen.queryByTestId('unsaved-default-notice')).toBeNull()
  })

  it('dismisses for the session and deep-links to the setting', async () => {
    forced()
    mount()
    await screen.findByTestId('unsaved-default-notice')
    fireEvent.click(screen.getByRole('button', { name: /Open Chat settings/ }))
    expect(screen.getByTestId('location')).toHaveTextContent(
      '/settings/chat?highlight=key%3Adashboard.default_memory_mode',
    )
    // Landing on the Chat settings tab is what hides it here; the dismiss
    // control is exercised on a route where the banner stays visible.
    expect(screen.queryByTestId('unsaved-default-notice')).toBeNull()
  })

  it('can be dismissed for the session', async () => {
    forced()
    mount()
    await screen.findByTestId('unsaved-default-notice')
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByTestId('unsaved-default-notice')).toBeNull()
  })
})
