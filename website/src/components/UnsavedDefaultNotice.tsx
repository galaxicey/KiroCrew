/**
 * UnsavedDefaultNotice — tells the user that new chats are not being saved
 * because the gateway could not read its config.
 *
 * ## Why a banner and not only a Settings row
 *
 * When `config.json` (or its `dashboard` section) cannot be read, the config
 * loader fails closed and defaults every new dashboard chat to Temporary — a
 * privacy default, chosen so an unreadable privacy choice is never widened. The
 * consequence, though, is data loss: a Temporary chat's transcript exists only
 * while the process is running, so every chat opened until the file is fixed is
 * gone at the next restart or upgrade. Nobody opens Settings to discover that,
 * and the chat header's mode icon is one small glyph. The notice has to arrive
 * unprompted, on whatever page the user is on, while the condition holds.
 *
 * ## Why it renders ONLY in the forced state
 *
 * A user who chose Incognito or Temporary as their default made that choice on
 * a Settings row that now spells out the consequence, and a banner that nags a
 * deliberate choice on every launch is one the user learns to ignore — including
 * here, when it matters. So this component reads the gateway's read-only
 * `default_memory_mode_forced` flag and nothing else: a normal install, whatever
 * its default, never sees it.
 *
 * ## Why it is an ErrorNotice, and why it hides on the Chat settings tab
 *
 * The forced state IS a config-read failure, so it renders through the shared
 * `ErrorNotice` (AUTOSDE `errors-use-error-notice`) with the agent hand-off on:
 * the hand-off navigates to a chat and this banner holds no draft to lose. On
 * `/settings/chat` the row it points at already carries the same failure in its
 * own notice, so showing both is one problem reported twice on one screen and a
 * button that leads to the page the user is on — the banner yields to the row
 * there and returns on every other route.
 *
 * ## Why it is dismissible per session
 *
 * The banner is a report of a condition, not a task with a completion. Once seen
 * it has done its job for this visit; it returns on the next page load if the
 * file is still unreadable, because the condition (and the loss) is still live.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation, useNavigate } from 'react-router-dom'
import { Settings } from 'lucide-react'
import { api } from '../api/client'
import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'
import { settingsPath } from './settingsPath'

import { i18nT } from '../i18n/t'

/** The Chat settings tab, where the Default Memory Mode row reports the same failure. */
const CHAT_SETTINGS_PREFIX = '/settings/chat'

export default function UnsavedDefaultNotice() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [dismissed, setDismissed] = useState(false)
  // Same query key and staleTime as every other reader of this endpoint, so
  // this adds no request of its own — it rides the fetch the chat surface and
  // the share menu already make. A failed GET is not this banner's report: the
  // surfaces that own the config (Settings → Chat) already name that failure,
  // and an advisory flag that could not be read is simply not raised, the same
  // as when it is false.
  const { data } = useQuery<{ default_memory_mode_forced?: boolean }>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
    staleTime: 30_000,
  })

  if (dismissed || data?.default_memory_mode_forced !== true) return null
  if (pathname === CHAT_SETTINGS_PREFIX || pathname.startsWith(`${CHAT_SETTINGS_PREFIX}/`)) return null

  return (
    <ErrorNotice
      className="mx-3 md:mx-6 mt-4 mb-2 animate-rise"
      testId="unsaved-default-notice"
      title={i18nT('components.unsavedDefaultNotice.new_chats_are_not_being_saved')}
      message={i18nT('components.unsavedDefaultNotice.config_unreadable_forced_temporary')}
      askAgent
      onDismiss={() => setDismissed(true)}
      footer={(
        <Btn
          onClick={() => navigate(settingsPath({ tab: 'chat', highlight: 'key:dashboard.default_memory_mode' }))}
          className="justify-center"
        >
          <Settings size={14} /> {i18nT('components.unsavedDefaultNotice.open_chat_settings')}
        </Btn>
      )}
    />
  )
}
