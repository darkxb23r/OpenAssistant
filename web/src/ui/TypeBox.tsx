import { useState } from 'react'
import { useStore } from '../store'

/**
 * Typing to Jarvis, for when speaking isn't practical: a quiet room, a long
 * path, a misheard word. Enter sends it as a turn, exactly as if it had been
 * said — no wake word needed. While he is speaking aloud a line waits and goes
 * when he finishes (lib/hold.ts); STOP, or Escape, cuts him off on purpose.
 * The app's single-key shortcuts ignore keys typed here.
 */
export function TypeBox({ onSend, onStop }: { onSend: (text: string) => void; onStop: () => void }) {
  const [text, setText] = useState('')
  const phase = useStore((s) => s.phase)
  const held = useStore((s) => s.held.length)
  const audible = useStore((s) => s.audible || s.phase === 'speaking')
  const name = useStore((s) => s.assistant.name)
  if (phase === 'offline' || phase === 'boot') return null

  return (
    <form
      className="typebox"
      onSubmit={(e) => {
        e.preventDefault()
        const said = text.trim()
        if (!said) return
        onSend(said)
        setText('')
      }}
    >
      {held > 0 && (
        <span className="typebox-held" role="status">
          {held === 1 ? 'HELD' : `${held} HELD`} · SENDS WHEN HE FINISHES
        </span>
      )}
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key !== 'Escape') return
          // While he talks Escape stops him, as it does outside the box; otherwise it leaves the box.
          if (audible || held > 0) {
            e.preventDefault()
            onStop()
          } else {
            e.currentTarget.blur()
          }
        }}
        placeholder={`Type to ${name}…`}
        aria-label={`Message ${name}`}
        maxLength={4000}
        autoComplete="off"
      />
      {audible && (
        <button type="button" className="typebox-stop" onClick={onStop} aria-label={`Stop ${name} speaking`}>
          STOP
        </button>
      )}
      <button type="submit" disabled={!text.trim()} aria-label="Send">
        SEND
      </button>
    </form>
  )
}
