import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { MemoryModeChip } from './MemoryModeChip'

describe('MemoryModeChip', () => {
  afterEach(() => { vi.restoreAllMocks() })

  it('opens the popover upward, bottom-anchored 6px above the chip', () => {
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 700 })
    renderWithProviders(<MemoryModeChip onSwitchMode={vi.fn()} />)
    const btn = screen.getByText('Choose memory mode').closest('button')!
    vi.spyOn(btn, 'getBoundingClientRect').mockReturnValue({ top: 640, bottom: 664, left: 340, right: 460, width: 120, height: 24, x: 340, y: 640, toJSON: () => ({}) } as DOMRect)
    fireEvent.click(btn)
    const pop = screen.getByTestId('memory-mode-popover')
    expect(pop.style.bottom).toBe(`${700 - 640 + 6}px`)
    expect(pop.style.top).toBe('')
  })

  it('picking an option switches mode and closes the popover', () => {
    const onSwitchMode = vi.fn()
    renderWithProviders(<MemoryModeChip onSwitchMode={onSwitchMode} />)
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    expect(onSwitchMode).toHaveBeenCalledWith('incognito')
    expect(screen.queryByTestId('memory-mode-popover')).not.toBeInTheDocument()
  })

  it('an ephemeral mode is warn-tinted, rounded-lg, and switches back to persistent', () => {
    const onSwitchMode = vi.fn()
    renderWithProviders(<MemoryModeChip memoryMode="temporary" onSwitchMode={onSwitchMode} />)
    const btn = screen.getByText('Temporary — switch to persistent mode').closest('button')!
    expect(btn.className).toContain('border-warn')
    expect(btn.className).toContain('rounded-lg')
    fireEvent.click(btn)
    expect(onSwitchMode).toHaveBeenCalledWith('persistent')
  })
})
