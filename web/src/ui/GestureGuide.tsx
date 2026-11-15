import { useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { diag, dwellSettings, enableHands, setDwellSettings } from '../lib/hands'
import { detectionState } from '../lib/gestureHealth'
import { DWELL_MAX_MS, DWELL_MIN_MS } from '../lib/dwell'
import { useStore } from '../store'

/**
 * What your hands can do.
 *
 * A touchless interface has the same problem a voice interface has: no menus,
 * no buttons, nothing on screen that tells you what is possible. The
 * suggestions strip solves that for speech, and this is its equivalent for
 * hands — shown when the camera comes on, when you would actually be wondering.
 *
 * It fades once you have used it. A legend that stays up forever is clutter,
 * and the moment you have successfully pinched something you no longer need to
 * be told how; but it comes back whenever the camera is turned on again,
 * because that is when you have forgotten.
 */

const MOVES: { gesture: string; hand: string; does: string }[] = [
  { gesture: 'point', hand: '☝', does: 'move the cursor' },
  { gesture: 'point', hand: '⏱', does: 'hold over a button to click' },
  { gesture: 'pinch', hand: '🤏', does: 'grab a blade · move it · press' },
  { gesture: 'open', hand: '🖐', does: 'let go' },
  { gesture: 'peace', hand: '✌', does: 'two fingers up-down to scroll' },
  { gesture: 'frame', hand: '📐', does: 'two L-corners to resize' },
]

/** How long the legend stays after the first successful press. */
const DISMISS_MS = 1400

export function GestureGuide({ live }: { live: boolean }) {
  const [show, show_] = useState(false)
  const used = useRef(false)
  const poll = useRef(0)

  useEffect(() => {
    if (!live) {
      show_(false)
      used.current = false
      return
    }
    show_(true)

    // Polled rather than subscribed: the tracker publishes a plain mutable
    // object on purpose, so that the loop's timing is not at the mercy of
    // React. Four times a second is plenty to notice a first pinch.
    poll.current = window.setInterval(() => {
      if (used.current) return
      if (diag.gesture.includes('pinch')) {
        used.current = true
        window.setTimeout(() => show_(false), DISMISS_MS)
      }
    }, 250)
    return () => window.clearInterval(poll.current)
  }, [live])

  return (
    <AnimatePresence>
      {show && (
        <motion.div
          className="gguide"
          initial={{ opacity: 0, y: 10, filter: 'blur(6px)' }}
          animate={{ opacity: 1, y: 0, filter: 'blur(0px)' }}
          exit={{ opacity: 0, y: 8, filter: 'blur(6px)', transition: { duration: 0.5 } }}
          transition={{ type: 'spring', stiffness: 260, damping: 28 }}
        >
          <div className="gguide-head">HAND CONTROL</div>
          {MOVES.map((m) => (
            <div key={m.does} className="gguide-row">
              <span className="gguide-icon">{m.hand}</span>
              <span className="gguide-name">{m.gesture}</span>
              <span className="gguide-does">{m.does}</span>
            </div>
          ))}
          <div className="gguide-foot">
            grab a blade by its bar · <kbd>G</kbd> to stop
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

/**
 * Whether the tracker is working, and if not why. Shows a start-up failure with a Retry button,
 * and while the camera is on, what it currently sees (no hand / low light / tracking). A small
 * toggle opens a debug readout: FPS, hands, gesture, delegate and the last error. Off by default.
 */
export function GestureStatus({ live }: { live: boolean }) {
  const [, tick] = useState(0)
  const [debug, setDebug] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!live && !diag.failure) return
    const t = window.setInterval(() => tick((n) => n + 1), 250)
    return () => window.clearInterval(t)
  }, [live, diag.failure])

  const retry = () => {
    setBusy(true)
    useStore.getState().setError(null)
    enableHands()
      .then(() => useStore.getState().setGestures(true))
      .catch((err: Error) => useStore.getState().setError(err?.message ?? String(err)))
      .finally(() => setBusy(false))
  }

  if (!live && !diag.failure) return null
  const det = detectionState(diag.count, diag.gesture, performance.now() - diag.handSeenAt)
  return (
    <div className={`gstatus gstatus-${diag.failure && !live ? 'fail' : det.kind}`} role="status">
      {diag.failure && !live ? (
        <>
          <span className="gstatus-text">{diag.failure.message}</span>
          <button className="gstatus-btn" disabled={busy} onClick={retry}>
            {busy ? 'Retrying...' : 'Retry'}
          </button>
        </>
      ) : (
        <>
          <span className="gstatus-text">{det.text}</span>
          <button className="gstatus-btn" onClick={() => setDebug((d) => !d)}>
            {debug ? 'hide debug' : 'debug'}
          </button>
        </>
      )}
      {live && (
        <div className="gstatus-dwell">
          <label>
            <input
              type="checkbox"
              checked={dwellSettings.enabled}
              onChange={(e) => {
                setDwellSettings({ enabled: e.target.checked })
                tick((n) => n + 1)
              }}
            />{' '}
            dwell click
          </label>
          <input
            type="range"
            aria-label="Dwell time"
            min={DWELL_MIN_MS}
            max={DWELL_MAX_MS}
            step={100}
            value={dwellSettings.ms}
            disabled={!dwellSettings.enabled}
            onChange={(e) => {
              setDwellSettings({ ms: Number(e.target.value) })
              tick((n) => n + 1)
            }}
          />{' '}
          <span>{(dwellSettings.ms / 1000).toFixed(1)} s</span>
        </div>
      )}
      {live && diag.lastError && <div className="gstatus-err">{diag.lastError}</div>}
      {live && debug && (
        <pre className="gstatus-debug">
          {`fps ${diag.fps}  hands ${diag.count}  gesture ${diag.gesture || '-'}\ndelegate ${diag.delegate || '-'}  error ${diag.lastError || '-'}`}
        </pre>
      )}
    </div>
  )
}
