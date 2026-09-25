/**
 * One invariant, across every chat surface that discloses a file or folder path:
 *
 *   A path carried in a native `title`, or an action reachable only by a pointer
 *   gesture, must ALSO be reachable without a pointer.
 *
 * `title` opens on pointer hover only — no browser opens it on keyboard focus —
 * and these chips show a short label as their visible text, so a path left in
 * `title` alone reaches nobody using a keyboard or a screen reader. The path is
 * therefore also the element's accessible name, the same naming
 * `FileHeaderBreadcrumb` uses.
 *
 * Each test asserts BOTH halves: the pointer route still works and the
 * non-pointer route exists. Asserting only the second would pass on a change
 * that dropped the tooltip.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import type { PasteBlock } from '../utils/pasteTokens'
import { i18nT } from '../i18n/t'

const touchEnv = vi.hoisted(() => ({ touch: false }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => touchEnv.touch }))
// Only the prose of a bubble goes through the renderer; the path chips are its
// siblings, so echoing the markdown keeps them in the tree without pulling the
// markdown pipeline into an accessibility test.
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content?: string }) => content ?? null,
}))

import { renderUserContent } from '../pages/chat/ChatPageMessageContent'
import ChatInput from '../components/ChatInput'

beforeEach(() => {
  touchEnv.touch = false
  localStorage.clear()
})

// ── Sent-message chips ──────────────────────────────────────────────────────
//
// A chip degrades to an inert span on a host with no file viewer (a split pane,
// a Crew Member DM). That inert branch is what these pin: its clickable sibling
// is a real button and is named with the path already.

describe('sent-message path chips name the full path, not only the tooltip', () => {
  /** The inert host: no handlers at all, so every chip takes its degraded form. */
  const inert = (content: string, meta?: Record<string, unknown>) =>
    render(<div>{renderUserContent({ content, meta })}</div>)

  it('names an inert folder chip with the full path', () => {
    const dir = '/repo/main/website/src'
    inert('look in [attached_dir 1] /repo/main/website/src for dead code', { dirs: [dir] })
    // No button: this host cannot open the folder.
    expect(screen.queryByRole('button')).toBeNull()
    const chip = screen.getByRole('group', { name: dir })
    // The pointer route is intact.
    expect(chip).toHaveAttribute('title', dir)
    // Visible text is the short label alone, which is why the name matters.
    expect(chip).toHaveTextContent('@src')
    expect(chip.textContent).not.toContain('/repo/main')
  })

  it('names an inert file-mention chip with the full path', () => {
    const file = '/home/u/notes/todo.md'
    inert('please read [attached_file 1] /home/u/notes/todo.md and summarise', { files: [file] })
    const chip = screen.getByRole('group', { name: file })
    expect(chip).toHaveAttribute('title', file)
    expect(chip).toHaveTextContent('@todo.md')
    expect(chip.textContent).not.toContain('/home/u/notes')
  })

  it('names an inert attachment card with the sentence its tooltip shows', () => {
    // This card's tooltip is a localized sentence CONTAINING the path, which
    // also says the card cannot be opened here. The accessible name is that
    // same sentence, so the two routes cannot disagree.
    const pdf = '/home/u/q3/report.pdf'
    const sentence = i18nT('pages.chatPage.attached_file_inert', { path: pdf })
    inert('[attached_file 1] /home/u/q3/report.pdf', { files: [pdf] })
    const card = screen.getByRole('group', { name: sentence })
    expect(card).toHaveAttribute('title', sentence)
    expect(sentence).toContain(pdf)
  })

  it('tells two same-named files in different directories apart by accessible name', () => {
    // The point of carrying the path: both chips read `report.pdf` on screen,
    // so the accessible name is the only thing that separates them.
    const a = '/home/u/q3/report.pdf'
    const b = '/home/u/q4/report.pdf'
    inert('[attached_file 1] /home/u/q3/report.pdf\n[attached_file 2] /home/u/q4/report.pdf', {
      files: [a, b],
    })
    for (const path of [a, b]) {
      const name = i18nT('pages.chatPage.attached_file_inert', { path })
      expect(screen.getByRole('group', { name })).toBeInTheDocument()
    }
  })
})

// ── Composer preview strip ──────────────────────────────────────────────────

describe('composer preview tiles name the full path, not only the tooltip', () => {
  const props = { value: '', onChange: vi.fn(), onSend: vi.fn() }

  it('names an image tile with the full path', () => {
    const png = '/home/u/shots/screenshot.png'
    renderWithProviders(<ChatInput {...props} pendingFiles={[png]} />)
    const tile = screen.getByRole('group', { name: png })
    expect(tile).toHaveAttribute('title', png)
  })

  it('names a non-image tile with the full path its visible text omits', () => {
    const txt = '/home/u/notes/todo.txt'
    renderWithProviders(<ChatInput {...props} pendingFiles={[txt]} />)
    const tile = screen.getByRole('group', { name: txt })
    expect(tile).toHaveAttribute('title', txt)
    expect(tile).toHaveTextContent('todo.txt')
    expect(tile.textContent).not.toContain('/home/u/notes')
  })

  it('names a folder tile with the full path', () => {
    const dir = '/repo/main/website/docs'
    renderWithProviders(<ChatInput {...props} pendingDirs={[dir]} />)
    const tile = screen.getByRole('group', { name: dir })
    expect(tile).toHaveAttribute('title', dir)
  })

  it('puts each bare-verb remove button inside its own file group', () => {
    // Two files staged means two buttons both named just "Remove". The group
    // each sits in is what says WHICH file it removes.
    const a = '/home/u/a/report.txt'
    const b = '/home/u/b/report.txt'
    const onRemoveFile = vi.fn()
    renderWithProviders(<ChatInput {...props} pendingFiles={[a, b]} onRemoveFile={onRemoveFile} />)
    const remove = i18nT('components.chatInput.remove')
    for (const path of [a, b]) {
      const tile = screen.getByRole('group', { name: path })
      fireEvent.click(within(tile).getByRole('button', { name: remove }))
      expect(onRemoveFile).toHaveBeenLastCalledWith(path)
    }
  })

  it('puts the remove-folder button inside its folder group', () => {
    const dir = '/repo/main/website/docs'
    const onRemoveDir = vi.fn()
    renderWithProviders(<ChatInput {...props} pendingDirs={[dir]} onRemoveDir={onRemoveDir} />)
    const tile = screen.getByRole('group', { name: dir })
    const label = i18nT('components.filePickerMenu.remove_folder')
    fireEvent.click(within(tile).getByRole('button', { name: label }))
    expect(onRemoveDir).toHaveBeenCalledWith(dir)
  })
})

// ── Collapsed-paste-token expansion ────────────────────────────────────────

describe('collapsed paste token expands by keyboard, not only by pointer', () => {
  const onSend = vi.fn()
  const block: PasteBlock = { id: 'p1', seq: 1, lines: 40, content: 'TRACEBACK: boom\n...40 lines...' }
  const token = '[ Paste #1 · 40 lines ]'

  function PasteHarness({ initial }: { initial: string }) {
    const [v, setV] = React.useState(initial)
    const [blocks, setBlocks] = React.useState<PasteBlock[]>([block])
    return (
      <ChatInput
        value={v}
        onChange={setV}
        onSend={onSend}
        pasteBlocks={blocks}
        onPasteBlocksChange={setBlocks}
      />
    )
  }

  const composer = () => screen.getByLabelText('Message input') as HTMLTextAreaElement

  beforeEach(() => { onSend.mockClear() })

  it('expands the token on Alt+Enter with the caret on it', () => {
    renderWithProviders(<PasteHarness initial={token} />)
    const ta = composer()
    ta.setSelectionRange(2, 2)
    fireEvent.keyDown(ta, { key: 'Enter', altKey: true })
    expect(ta.value).toBe(block.content)
  })

  it('expands from a caret at the token edge, where a keyboard caret rests', () => {
    // Arrow keys snap the caret OUT of a token interior, so a keyboard user's
    // caret always sits on a boundary. Expansion has to work from there, or the
    // chord is unreachable in exactly the case it exists for.
    renderWithProviders(<PasteHarness initial={`hi ${token}`} />)
    const ta = composer()
    ta.setSelectionRange(3, 3) // the token's start edge
    fireEvent.keyDown(ta, { key: 'Enter', altKey: true })
    expect(ta.value).toBe(`hi ${block.content}`)
  })

  it('does not also send the message', () => {
    // Plain Enter sends in the default mode, so the expansion branch has to run
    // ahead of the send branch and stop there.
    renderWithProviders(<PasteHarness initial={token} />)
    const ta = composer()
    ta.setSelectionRange(2, 2)
    fireEvent.keyDown(ta, { key: 'Enter', altKey: true })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('leaves Alt+Enter alone when the caret is not on a token', () => {
    renderWithProviders(<PasteHarness initial={`${token} tail text`} />)
    const ta = composer()
    const caret = token.length + 5 // inside "tail"
    ta.setSelectionRange(caret, caret)
    fireEvent.keyDown(ta, { key: 'Enter', altKey: true })
    expect(ta.value).toBe(`${token} tail text`)
  })

  it('is its own chord, not an overload of the paste key', () => {
    // Overloading paste cannot work: clipboard text is readable synchronously
    // only inside a `paste` event, so a keydown handler cannot tell an expand
    // press from a real paste. A modifier+V press leaves the token collapsed.
    renderWithProviders(<PasteHarness initial={token} />)
    const ta = composer()
    ta.setSelectionRange(2, 2)
    fireEvent.keyDown(ta, { key: 'v', metaKey: true, shiftKey: true })
    expect(ta.value).toBe(token)
  })
})
