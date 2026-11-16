import { useEffect, useMemo, useRef, useState } from 'react'
import {
  requestSettings,
  restartCore,
  saveSettings,
  watchSettings,
  type SettingField,
} from '../lib/bridge'
import { MEANING_MAX } from '../lib/identity'
import { peaceScroll } from '../lib/hands'

/**
 * Settings with a few fixed values are a row of buttons, not a drop-down: a hand can dwell on a
 * button, never on a <select>. Everything here but typing works by dwell (or a pinch): the
 * choices, CLEAR / KEEP, SAVE, RESTART (pressed twice) and CLOSE; two fingers up scrolls.
 */
const CHOICES: Record<string, [string, string][]> = {
  JARVIS_CODEX: [['0', 'OFF'], ['1', 'ON']],
  MODEL_PROVIDER: [['claude', 'CLAUDE'], ['ollama', 'OLLAMA']],
}
const CHOICE_NOW: Record<string, (v: string) => string> = {
  JARVIS_CODEX: (v) => (['1', 'true', 'yes', 'on'].includes(v.toLowerCase()) ? '1' : '0'),
  MODEL_PROVIDER: (v) => (v.toLowerCase() === 'ollama' ? 'ollama' : 'claude'),
}
/** How long RESTART waits for its second press. */
const CONFIRM_MS = 6000

/**
 * Settings: ~/.jarvis/secrets.env, edited here (settings.py on the core).
 *
 * Secrets are input only: the core never sends their values, so this panel can only say whether
 * one is set, replace it, or clear it. A field left empty is left as it is. Changes take effect
 * when Jarvis restarts: the panel's restart button, always there, stands out once something is saved.
 */
export function Settings({ onClose }: { onClose: () => void }) {
  const [fields, setFields] = useState<SettingField[] | null>(null)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [clear, setClear] = useState<Set<string>>(new Set())
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [note, setNote] = useState('')
  const [saved, setSaved] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const backdrop = useRef<HTMLDivElement>(null)

  // RESTART asks for a second press, in the panel (a browser confirm() can't be dwelt on).
  useEffect(() => {
    if (!confirming) return
    const t = setTimeout(() => setConfirming(false), CONFIRM_MS)
    return () => clearTimeout(t)
  }, [confirming])

  // Two fingers up, moved up or down, scrolls the panel (as it does a blade).
  useEffect(() => {
    let raf = 0
    let last: number | null = null
    const tick = () => {
      raf = requestAnimationFrame(tick)
      const travelled = peaceScroll()
      if (travelled === null) {
        last = null
        return
      }
      if (last !== null) backdrop.current?.scrollBy(0, (last - travelled) * 2.4)
      last = travelled
    }
    tick()
    return () => cancelAnimationFrame(raf)
  }, [])

  useEffect(() => {
    watchSettings((reply) => {
      if (reply.type === 'settings') {
        if (reply.error) setNote(reply.error)
        if (reply.fields) setFields(reply.fields)
      } else if (reply.errors) {
        setErrors(reply.errors)
        setNote('Nothing was saved: see the fields marked below.')
      } else if (reply.saved) {
        setErrors({})
        setDraft({})
        setClear(new Set())
        setSaved(true)
        setNote(`Saved ${reply.saved.length} setting${reply.saved.length === 1 ? '' : 's'}. They apply when Jarvis restarts.`)
        requestSettings()
      }
    })
    if (!requestSettings()) setNote('Not connected to Jarvis.')
    return () => watchSettings(null)
  }, [])

  const groups = useMemo(() => {
    const out = new Map<string, SettingField[]>()
    for (const f of fields ?? []) out.set(f.group, [...(out.get(f.group) ?? []), f])
    return [...out.entries()]
  }, [fields])

  const changes = (): Record<string, string | null> => {
    const out: Record<string, string | null> = {}
    for (const [key, value] of Object.entries(draft)) {
      const field = fields?.find((f) => f.key === key)
      if (!field) continue
      // A secret left empty stays as it is; a plain field is saved as typed (empty clears it).
      if (field.secret ? value !== '' : value !== (field.value ?? '')) out[key] = value
    }
    for (const key of clear) out[key] = null
    return out
  }
  const pending = Object.keys(changes()).length

  return (
    <div ref={backdrop} className="settings-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <form
        className="settings"
        role="dialog"
        aria-label="Settings"
        autoComplete="off"
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.preventDefault()
            onClose()
          }
        }}
        onSubmit={(e) => {
          e.preventDefault()
          const values = changes()
          if (!Object.keys(values).length) return
          setNote('Saving…')
          if (!saveSettings(values)) setNote('Not connected to Jarvis.')
        }}
      >
        <header className="settings-head">
          <span>SETTINGS</span>
          <button type="button" onClick={onClose} aria-label="Close settings">
            CLOSE
          </button>
        </header>
        <p className="settings-lead">
          Secrets are input only: once saved they can be replaced or cleared, never shown again.
        </p>
        {!fields && <p className="settings-note">{note || 'Loading…'}</p>}
        {groups.map(([group, list]) => (
          <fieldset key={group} className="settings-group">
            <legend>{group}</legend>
            {list.map((f) => {
              const id = `setting-${f.key}`
              const clearing = clear.has(f.key)
              return (
                <div key={f.key} className={`settings-row${errors[f.key] ? ' has-error' : ''}`}>
                  <label htmlFor={id}>
                    {f.label}
                    <span className="settings-state">
                      {f.secret ? (clearing ? 'will be cleared' : f.set ? 'set' : 'not set') : ''}
                      {f.environment ? ' · set in the environment, which wins' : ''}
                    </span>
                  </label>
                  {CHOICES[f.key] ? (
                    <div id={id} className="settings-choices" role="radiogroup" aria-label={f.label}>
                      {CHOICES[f.key].map(([value, label]) => {
                        const on = (draft[f.key] ?? CHOICE_NOW[f.key](f.value ?? '')) === value
                        return (
                          <button
                            key={value}
                            type="button"
                            role="radio"
                            aria-checked={on}
                            className={`settings-choice${on ? ' on' : ''}`}
                            onClick={() => setDraft({ ...draft, [f.key]: value })}
                          >
                            {label}
                          </button>
                        )
                      })}
                    </div>
                  ) : f.key === 'JARVIS_NAME_MEANING' ? (
                    <textarea
                      id={id}
                      rows={3}
                      spellCheck
                      placeholder="Not set"
                      value={draft[f.key] ?? (f.value ?? '')}
                      // One line in secrets.env: a new line becomes a space.
                      onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value.replace(/[\r\n]+/g, ' ') })}
                      maxLength={MEANING_MAX}
                    />
                  ) : (
                    <input
                      id={id}
                      type={f.secret ? 'password' : 'text'}
                      autoComplete={f.secret ? 'new-password' : 'off'}
                      spellCheck={false}
                      disabled={clearing}
                      placeholder={f.secret ? (f.set ? 'Set — type to replace' : 'Not set') : 'Not set'}
                      value={draft[f.key] ?? (f.secret ? '' : (f.value ?? ''))}
                      onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
                      maxLength={500}
                    />
                  )}
                  {f.secret && f.set && (
                    <button
                      type="button"
                      className="settings-clear"
                      onClick={() => {
                        const next = new Set(clear)
                        if (clearing) next.delete(f.key)
                        else next.add(f.key)
                        setClear(next)
                      }}
                    >
                      {clearing ? 'KEEP' : 'CLEAR'}
                    </button>
                  )}
                  {(errors[f.key] || f.help) && (
                    <small className={errors[f.key] ? 'settings-error' : 'settings-help'}>
                      {errors[f.key] || f.help}
                    </small>
                  )}
                </div>
              )
            })}
          </fieldset>
        ))}
        {fields && (
          <footer className="settings-foot">
            <span className="settings-note" role="status">
              {note}
            </span>
            <button
              type="button"
              className={saved ? 'settings-restart-due' : undefined}
              title={saved ? 'Saved settings apply after a restart' : 'Restart Jarvis'}
              onClick={() => {
                if (!confirming) {
                  setConfirming(true)
                  setNote('Press again to restart. Anything he is doing in the background stops.')
                } else {
                  setConfirming(false)
                  setNote(restartCore() ? 'Restarting Jarvis…' : 'Not connected to Jarvis.')
                }
              }}
            >
              {confirming ? 'CONFIRM RESTART' : 'RESTART JARVIS'}
            </button>
            <button type="submit" disabled={!pending}>
              SAVE{pending ? ` (${pending})` : ''}
            </button>
          </footer>
        )}
      </form>
    </div>
  )
}
