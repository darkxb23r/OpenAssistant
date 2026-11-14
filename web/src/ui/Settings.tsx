import { useEffect, useMemo, useState } from 'react'
import {
  requestSettings,
  restartCore,
  saveSettings,
  watchSettings,
  type SettingField,
} from '../lib/bridge'
import { MEANING_MAX } from '../lib/identity'

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
    <div className="settings-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
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
                  {f.key === 'JARVIS_CODEX' ? (
                    <select
                      id={id}
                      value={draft[f.key] ?? (['1', 'true', 'yes', 'on'].includes((f.value ?? '').toLowerCase()) ? '1' : '0')}
                      onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
                    >
                      <option value="0">Off</option>
                      <option value="1">On</option>
                    </select>
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
              onClick={() =>
                window.confirm('Restart Jarvis now? Anything he is doing in the background stops.') &&
                restartCore() &&
                setNote('Restarting Jarvis…')
              }
            >
              RESTART JARVIS
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
