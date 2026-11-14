import { useEffect, useRef } from 'react'
import { Scene } from './scene/Scene'
import { Hud } from './ui/Hud'
import { Boot } from './ui/Boot'
import { Ignition } from './ui/Ignition'
import { TypeBox } from './ui/TypeBox'
import { Settings } from './ui/Settings'
import { Diagnostics } from './ui/Diagnostics'
import { useStore } from './store'
import { startVoice, stripCall, setCallName, type Voice, type VoiceMode } from './lib/voice'
import { brandMark, readIdentity } from './lib/identity'
import { createSpeaker, cycleVoice, currentVoiceName, audible as ttsAudible, onAudible } from './lib/tts'
import { asAnswer, drainReady, isAudible, isBusy, voiceMode } from './lib/hold'
import * as sfx from './lib/sfx'
import * as music from './lib/music'
import * as hands from './lib/hands'
import { listenForClap } from './lib/clap'
import * as camera from './lib/camera'
import * as kokoro from './lib/kokoro'
import { TTS_ENGINE } from './config'
import { forTool, attention } from './lib/fillers'
import {
  ask,
  warm,
  interrupt,
  watchServers,
  watchPanels,
  watchBlades,
  watchBladeClose,
  watchIdentity,
  watchCapture,
  watchUi,
  watchConnection,
  watchSay,
  watchStandby,
  watchQuestionClosed,
  openQuestions,
  answer,
  notePresence,
  connectedLabels,
} from './lib/brain'
import { startAnalyser, micLevel } from './lib/audio'
import { probeCapabilities } from './lib/capabilities'

/**
 * The conversation.
 *
 * This used to be a sequential loop — greet, await a capture, await an answer,
 * repeat — with the microphone opened and closed around each step. That shape
 * cannot be interrupted: while it is awaiting the answer, nothing is listening,
 * so there is no way for the user to get a word in.
 *
 * It is an event machine now. The voice loop runs continuously and pushes
 * events at us; every one of them is legal in every phase. Saying anything at
 * all stops him talking, and whatever you say next becomes the new turn.
 */

/** How long to wait for someone to start speaking after he wakes. Generous:
 *  people say his name and *then* think about what they wanted. */
const AWAIT_SPEECH_MS = 14000

/** After an answer, how long the mic stays open for a follow-up before he
 *  drops back to standby. Long enough that you don't have to say the name
 *  again to continue a thought. */
const FOLLOW_UP_MS = 11000

/** crypto.randomUUID needs a secure context, which a LAN address over plain
 *  http is not. Not worth failing a whole turn over an id. */
const newId = () =>
  globalThis.crypto?.randomUUID?.() ??
  `id${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`


export default function App() {
  const store = useStore
  const phase = useStore((s) => s.phase)
  const settingsOpen = useStore((s) => s.settingsOpen)
  const speaker = useRef<ReturnType<typeof createSpeaker> | null>(null)
  const voice = useRef<Voice | null>(null)

  /**
   * Monotonic turn counter. Every await in a turn checks it on the way out:
   * if it has moved, that turn was superseded by a barge-in and must not touch
   * the phase, the speaker, or the busy state on its way to the floor.
   */
  const turn = useRef(0)
  /** Jarvis asked for standby: go dormant when he's done speaking, not listen. */
  const standbyNext = useRef(false)
  /** The user last typed rather than spoke: no follow-up mic, only his name opens it (lib/hold.ts). */
  const textMode = useRef(false)
  const booting = useRef(false)
  const idleTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const voicePoll = useRef<ReturnType<typeof setInterval> | null>(null)
  const presenceTimer = useRef<ReturnType<typeof setInterval> | null>(null)

  // -- helpers --------------------------------------------------------------

  const clearIdle = () => {
    if (idleTimer.current) clearTimeout(idleTimer.current)
    idleTimer.current = null
  }

  const silence = () => {
    speaker.current?.cancel()
    speaker.current = null
  }

  const goDormant = () => {
    standbyNext.current = false
    clearIdle()
    silence()
    turn.current++
    const s = store.getState()
    s.setCaption('')
    s.setActiveTool(null)
    music.working(false)
    music.duck(false)
    sfx.duck(false)
    s.setPhase('dormant')
  }

  /** He is speaking aloud (or the turn's next sentence is on its way). */
  const audibleNow = () =>
    isAudible(ttsAudible(), store.getState().phase, openQuestions().length > 0)

  /** After he finishes speaking: send what was typed meanwhile; else, on standby or after a typed
   *  turn, wait for his name again; else listen for a follow-up. */
  const afterSpeaking = () => {
    if (store.getState().held.length) {
      // The turn is over: nothing to stop, only the last speaker to let go of.
      silence()
      store.getState().setPhase('listening')
      flushHeld()
    } else if (standbyNext.current) {
      standbyNext.current = false
      goDormant()
    } else if (textMode.current) {
      goDormant()
    } else {
      listen(FOLLOW_UP_MS)
    }
  }

  /** Send the lines typed while he spoke, as one: an answer if a question waits on one. */
  const flushHeld = () => {
    const s = store.getState()
    const text = s.held.join('\n')
    s.setHeld([])
    if (text) send(text)
  }

  /** Lines typed while he spoke go once he's quiet and the turn is over (or waits on a question). */
  const maybeFlush = () => {
    const s = store.getState()
    if (!s.held.length) return
    if (drainReady(audibleNow(), isBusy(s.phase), openQuestions().length > 0)) flushHeld()
  }

  /** Open the mic and wait. `window` is how long before he gives up. */
  const listen = (window: number) => {
    clearIdle()
    const s = store.getState()
    s.setCaption('')
    s.setPhase('listening')
    sfx.play('listen')
    idleTimer.current = setTimeout(goDormant, window)
  }

  // -- one turn -------------------------------------------------------------

  const respond = async (said: string): Promise<void> => {
    const mine = ++turn.current
    const stale = () => mine !== turn.current
    standbyNext.current = false   // a new request: whatever standby was pending is over

    clearIdle()
    const s = store.getState()
    // Last turn's panels and blades go now, before the new answer starts
    // putting its own up. Anything the model marked sticky survives.
    s.clearPanels()
    s.clearBlades()
    s.setCaption('')
    s.pushTurn({ id: newId(), role: 'user', text: said })
    s.setPhase('thinking')

    const spk = createSpeaker()
    speaker.current = spk
    sfx.duck(true)
    music.duck(true)

    const turnId = newId()
    let started = false
    let filled = false

    try {
      await ask(said, {
        onText: (delta) => {
          if (stale()) return
          if (!started) {
            started = true
            store.getState().setPhase('speaking')
            // The answer arriving is what ends the tool phase — a timer would
            // clear the readout while a slow tool was still running.
            store.getState().setActiveTool(null)
            music.working(false)
            store.getState().pushTurn({ id: turnId, role: 'jarvis', text: '' })
          }
          store.getState().appendToLastTurn(delta)
          spk.push(delta)
        },
        onTool: (name) => {
          if (stale()) return
          // Only claim the tooling phase while he has nothing to say yet.
          // Setting it unconditionally pinned the machine in 'tooling' for the
          // rest of any answer that called a tool after it started talking,
          // which also broke the reactor's lip-sync for the remainder.
          if (!started) store.getState().setPhase('tooling')
          store.getState().setActiveTool(name)
          sfx.play('tool')
          music.working(true)
          // Say something the moment work starts — a tool can take ten seconds
          // and silence that long reads as a crash. Once per turn only; a
          // chain of five tools shouldn't produce five apologies.
          if (!filled && !started) {
            filled = true
            spk.say(forTool(name))
          }
        },
      })

      if (stale()) return

      await spk.end()
      if (stale()) return
      sfx.play('done')
    } catch (err) {
      if (stale()) return
      console.error(err)
      sfx.play('error')
      store
        .getState()
        .setError(err instanceof Error ? err.message : 'Something went wrong.')
    } finally {
      if (!stale()) {
        speaker.current = null
        sfx.duck(false)
        music.duck(false)
        store.getState().setActiveTool(null)
        music.working(false)
        // Stay open. Having to say his name again to add one more sentence is
        // the difference between a conversation and a vending machine. Unless
        // he was just told to stand by.
        afterSpeaking()
      }
    }
  }

  /**
   * Jarvis speaking unprompted: task news, a background job's result, or a
   * permission question. Added to whatever he is saying now; otherwise it is a
   * turn of its own, and the mic stays open afterwards so a question can be
   * answered without the wake word.
   */
  const announce = (text: string) => {
    const s = store.getState()
    if (s.phase === 'offline' || s.phase === 'boot') return
    if (speaker.current) {
      // Mid-answer: it belongs to the answer on screen, if he has started one.
      if (s.turns[s.turns.length - 1]?.role === 'jarvis') s.appendToLastTurn(` ${text}`)
      else s.pushTurn({ id: newId(), role: 'jarvis', text })
      speaker.current.push(`${text} `)
      return
    }
    s.pushTurn({ id: newId(), role: 'jarvis', text })
    const mine = ++turn.current
    clearIdle()
    const spk = createSpeaker()
    speaker.current = spk
    s.setPhase('speaking')
    spk.push(`${text} `)
    void spk.end().then(() => {
      if (mine !== turn.current) return
      speaker.current = null
      afterSpeaking()
    })
  }

  /** A line typed into the box: a turn, as if it had been said, with no wake word needed. While he
   *  is speaking aloud it waits until he's done (an answer to an open question goes at once). */
  const typed = (text: string) => {
    const s = store.getState()
    if (s.phase === 'offline' || s.phase === 'boot') return
    textMode.current = true
    s.setError(null)
    if (answered(text)) return
    if (audibleNow()) {
      s.setHeld([...s.held, text])
      return
    }
    send(text)
  }

  /** A typed yes / no / always while a permission question is open answers it (oldest first). */
  const answered = (text: string): boolean => {
    const q = openQuestions()[0]
    const decision = q ? asAnswer(text) : null
    if (!q || !decision) return false
    store.getState().pushTurn({ id: newId(), role: 'user', text })
    answer(q.id, decision)
    return true
  }

  /** A typed line as a turn (or an answer). Typing while he works, quietly, stops that turn first. */
  const send = (text: string) => {
    if (answered(text)) return
    if (isBusy(store.getState().phase)) onSpeechStart()
    else silence()
    void respond(text)
  }

  /** Escape and the stop button: cut him off on purpose. Then whatever was typed meanwhile goes;
   *  otherwise he stands down to wait for his name. */
  const stop = () => {
    const s = store.getState()
    if (s.phase === 'offline' || s.phase === 'boot') return
    const busy = isBusy(s.phase)
    goDormant()
    if (busy) interrupt()
    flushHeld()
  }

  // -- voice events ---------------------------------------------------------

  /** What the voice loop should do with what it hears (lib/hold.ts). */
  const mode = (): VoiceMode =>
    voiceMode(store.getState().phase, audibleNow(), textMode.current)

  const onWake = (trailing: string) => {
    const phase = store.getState().phase
    if (phase === 'offline' || phase === 'boot') return
    // Called by voice: back to voice. In text mode a busy page hears only his name, and saying it
    // stops the turn like Space does.
    textMode.current = false
    if (isBusy(phase)) onSpeechStart()

    store.getState().setError(null)
    sfx.play('wake')

    // "Jarvis, what's happening in AI this week" in one breath. Waiting for a
    // greeting he didn't need is the most common way an assistant wastes time.
    if (trailing) {
      void respond(trailing)
      return
    }

    store.getState().setPhase('waking')

    // Answer to his name. Deliberately NOT awaited any more: the microphone is
    // already open and the echo filter knows his voice, so the user can talk
    // straight over the greeting instead of waiting it out.
    const greeting = createSpeaker()
    speaker.current = greeting
    greeting.say(attention())
    void greeting.end()

    listen(AWAIT_SPEECH_MS)
  }

  /**
   * Someone started talking. This is the whole point of the rewrite: he stops,
   * immediately, whatever he was doing.
   */
  const onSpeechStart = () => {
    clearIdle()
    const phase = store.getState().phase
    if (phase === 'offline' || phase === 'boot' || phase === 'dormant') return

    const wasBusy =
      phase === 'thinking' || phase === 'tooling' || phase === 'speaking'

    silence()
    if (wasBusy) {
      // Abandon the answer in flight. The turn counter moves in respond()'s
      // replacement; bumping it here covers the case where nothing replaces it.
      turn.current++
      interrupt()
      store.getState().setActiveTool(null)
      music.working(false)
      sfx.duck(false)
      music.duck(false)
    }
    store.getState().setPhase('listening')
  }

  const onUtterance = (text: string) => {
    const phase = store.getState().phase
    if (phase === 'offline' || phase === 'boot' || phase === 'dormant') return
    // Words that finish assembling while he speaks were said over him: held back, not a turn.
    if (audibleNow()) return
    textMode.current = false

    // People keep using his name as a vocative once they're already talking to
    // him. Strip it rather than sending "jarvis" to the model as a question; a
    // call that asks nothing ("Jarvis, you there?") just keeps him listening.
    const said = stripCall(text)
    if (!said) {
      listen(AWAIT_SPEECH_MS)
      return
    }

    void respond(said)
  }

  /**
   * The user is mid-sentence, or their words are still being transcribed: the wait for them starts
   * again. It used to run from the wake word, so a long command (or a slow transcriber) outlasted
   * it: the page went back to standby and dropped the transcript when it came, words on screen.
   */
  const onHearing = () => {
    const phase = store.getState().phase
    if ((phase !== 'waking' && phase !== 'listening') || !idleTimer.current) return
    clearIdle()
    idleTimer.current = setTimeout(goDormant, AWAIT_SPEECH_MS)
  }

  const onPartial = (text: string) => {
    store.getState().setCaption(text)
  }

  const onVoiceError = (message: string) => {
    store.getState().setError(message)
  }

  // -- power on -------------------------------------------------------------

  const powerOn = async () => {
    // The ignition button and the space bar can both land here, and the phase
    // only moves after the first await — so without this a double press boots
    // twice, arming two voice loops and two download polls.
    if (booting.current) return
    booting.current = true

    try {
      await ignite()
    } catch (err) {
      // The guard must not outlive a failed boot. Audio unlock can be refused,
      // the microphone prompt dismissed, the bridge unreachable at the wrong
      // moment — and with the flag still latched the ignition button was dead
      // for the rest of the page, recoverable only by reloading. Reset it and
      // put the button back so the user can simply press it again.
      booting.current = false
      console.error('[jarvis] power-up failed:', err)
      store.getState().setPhase('offline')
      store
        .getState()
        .setError(
          err instanceof Error
            ? `Power-up failed: ${err.message}`
            : 'Power-up failed. Click to try again.',
        )
    }
  }

  const ignite = async () => {
    const s = store.getState()

    // Must happen inside the click handler — browsers won't start an
    // AudioContext or speech synthesis without a user gesture.
    await sfx.unlockAudio()
    sfx.play('boot')
    // The score. Must be started from inside this click handler for the same
    // reason as the rest of the audio.
    music.enable()
    music.playBoot()
    music.startAmbient()

    s.setPhase('boot')

    watchServers((servers) => store.getState().setConnected(servers))
    watchPanels((panel) => store.getState().pushPanel(panel))
    watchBlades((blade) => store.getState().pushBlade(blade))
    watchBladeClose((id) => store.getState().closeBlade(id))
    watchIdentity((raw) => {
      const who = readIdentity(raw)
      store.getState().setAssistant(who)
      setCallName(who.name)
      document.title = brandMark(who)
    })
    watchSay(announce)
    // He fell quiet (or started): the voice loop's mode follows, and typed lines may go.
    onAudible((on) => {
      store.getState().setAudible(on)
      if (!on) maybeFlush()
    })
    // Standby arrives before "Standing by." is spoken: wait for that to finish.
    watchStandby(() => {
      if (speaker.current) standbyNext.current = true
      else goDormant()
    })

    /**
     * JARVIS asking to see something.
     *
     * Announced on screen for as long as it takes, with whatever he said he was
     * looking for. The camera's own light is on too, but a hardware light that
     * appears with no explanation is exactly the thing that makes people
     * distrust an assistant — so the interface says it before they have to ask.
     */
    watchCapture(async (req) => {
      const note =
        req.mode === 'watch'
          ? req.when === 'past'
            ? req.reason || 'reviewing the last few seconds'
            : `${req.reason || 'watching'} · ${req.seconds}s`
          : req.reason || 'taking a look'
      store.getState().setLooking(note)

      // The past is only available if something has been remembering it, and
      // that only happens while the camera is on screen. Answering plainly
      // beats opening the camera and recording the next few seconds instead,
      // which is a different question from the one that was asked.
      if (req.mode === 'watch' && req.when === 'past' && camera.bufferedSeconds() < 1) {
        store.getState().setLooking(null)
        return {
          error:
            'There is no recent footage — the camera has to be open on screen ' +
            'for me to remember what just happened. Ask me to open the camera, ' +
            'and I can watch from then on.',
        }
      }

      // Held for the whole capture. Without this the stream can be torn down by
      // whoever else was using it half way through a six-second watch.
      let held = false
      try {
        await camera.holdCamera()
        held = true
        if (req.mode === 'look') return camera.grabFrame()
        if (req.when === 'past') {
          const grid = camera.recentGrid(req.seconds, 9)
          return grid ?? { error: 'There is not enough recent footage to review.' }
        }
        return await camera.watchAhead(req.seconds, 9)
      } catch (err) {
        return {
          error:
            (err as DOMException)?.name === 'NotAllowedError'
              ? 'The camera is not permitted, so I cannot see anything.'
              : `The camera could not be read: ${(err as Error)?.message ?? err}`,
        }
      } finally {
        if (held) camera.releaseCamera()
        store.getState().setLooking(null)
      }
    })

    // The interface is JARVIS's to drive. These arrive out of band, pushed
    // mid-turn the way panels are, so a command can retint the reactor or put
    // something into orbit while he is still speaking the sentence about it.
    watchUi((op, args) => {
      const s = store.getState()
      const a = (args ?? {}) as Record<string, never>
      switch (op) {
        case 'patch':
          s.applyUi(args)
          break
        case 'orbit':
          if (a.action === 'add') s.addOrbit(args)
          else if (a.action === 'remove') s.removeOrbit(String(a.id))
          else s.clearOrbits()
          break
        case 'effect':
          s.fireEffect(a.kind)
          break
        case 'reset':
          s.resetUi()
          break
        case 'screen':
          s.clearScreen(a.what ?? 'all')
          break
        default:
          console.warn('[jarvis] unknown ui op:', op, args)
      }
    })
    // Jarvis keeps the conversation while the socket is down, but anything he
    // says meanwhile is not heard here, so a drop is worth mentioning.
    watchConnection((state, message) => {
      if (state === 'lost') {
        store.getState().setError('Connection to Jarvis lost — reconnecting.')
      } else if (state === 'reconnected') {
        store.getState().setError(null)
      } else if (state === 'refused') {
        // Not paired, revoked, or a stale page: Jarvis won't talk to this tab
        // until the user does something, so it stops dialling and says what.
        store.getState().setError(message ?? 'Jarvis refused this page.')
      }
    })
    // A permission question answered anywhere (here, another tab, by voice):
    // its card comes down everywhere.
    watchQuestionClosed((id) => store.getState().closeBlade(`q-${id}`))
    // Hands in view count as the user being here, like the pointer does
    // (bridge.ts throttles it).
    presenceTimer.current ??= setInterval(() => {
      if (hands.hands.length > 0) notePresence()
    }, 2000)
    const warming = warm().catch((err: Error) => s.setError(err.message))

    // Pull the neural voice down during the boot sequence so the first
    // "Hey Jarvis" isn't waiting on an 86MB download. Deliberately not awaited
    // — if it's slow, JARVIS comes up on the system voice and swaps over the
    // moment the model is ready.
    if (TTS_ENGINE === 'kokoro') {
      void kokoro.load()
      voicePoll.current = setInterval(() => {
        const p = kokoro.loadProgress()
        if (kokoro.isReady() || kokoro.isUnavailable()) {
          store.getState().setBootNote('')
          if (voicePoll.current) clearInterval(voicePoll.current)
          voicePoll.current = null
        } else if (p > 0 && p < 1) {
          store.getState().setBootNote(`voice ${Math.round(p * 100)}%`)
        }
      }, 200)
    }

    // Long enough for the four-beat start-up sequence in Boot.tsx to play —
    // status bar, rings, suit schematic, reactor power-up — before the live
    // interface takes over. Kept a touch under the boot cue so the music is
    // still rising as the reactor lands.
    await new Promise((r) => setTimeout(r, 9200)) // boot sequence
    await warming
    store.getState().setConnected(connectedLabels())

    // The analyser is what makes the reactor pulse with your voice. It needs a
    // getUserMedia stream; speech recognition does not, and gets its own. So a
    // failure here costs the animation and nothing else — saying "voice input
    // is unavailable" was both alarming and untrue.
    try {
      await startAnalyser()
    } catch {
      console.warn(
        '[jarvis] no microphone stream — the reactor will not pulse with your ' +
          'voice. Speech recognition is unaffected.',
      )
    }

    // Ask the bridge which speech engines exist before the loop starts, so the
    // first turn already uses ElevenLabs when a key is present and the browser
    // fallback when it is not — no flag, no reload. The HUD label is set only
    // after this resolves, or it always reports the browser fallback voice
    // even when ElevenLabs is the one actually about to speak.
    await probeCapabilities()
    store.getState().setVoice(currentVoiceName())

    // One voice loop, started once, running until the page closes.
    voice.current = await startVoice({
      mode,
      onWake,
      onSpeechStart,
      onPartial,
      onUtterance,
      onHearing,
      onError: onVoiceError,
    })

    store.getState().setPhase('dormant')
  }

  // -- clap to start --------------------------------------------------------

  /**
   * A clap brings him up, as an alternative to the button.
   *
   * Only while the ignition screen is showing, and torn down the moment he
   * boots — the microphone is about to belong to the voice loop, and two
   * analysers arguing over the same stream is how you get an assistant that
   * hears half of what you say.
   *
   * Deliberately silent about failure. If the microphone is refused, or has not
   * been granted yet, the button is still right there; announcing an error
   * about a feature nobody asked for would be worse than quietly doing without.
   */
  useEffect(() => {
    if (phase !== 'offline') return
    let live: { stop: () => void } | null = null
    let gone = false
    void listenForClap(() => {
      if (!gone) void powerOn()
    }).then((l) => {
      if (gone) l.stop()
      else live = l
    })
    return () => {
      gone = true
      live?.stop()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase])

  // -- level pump + keys ----------------------------------------------------

  useEffect(() => {
    let raf = 0

    const pump = () => {
      const st = store.getState()
      // While speaking, follow JARVIS's own output rather than the mic, so the
      // orb lip-syncs instead of reacting to room noise.
      const lvl =
        st.phase === 'speaking' && speaker.current
          ? speaker.current.level()
          : micLevel()
      st.setLevel(lvl)
      raf = requestAnimationFrame(pump)
    }
    pump()

    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if (store.getState().settingsOpen) return   // the panel has the keyboard

      // V auditions the next British voice installed on this machine. Which
      // ones exist varies per Mac, so hearing them beats trusting a ranking.
      // Bare V only — ⌘V and ⌃V are paste, and swallowing those was rude.
      if (
        e.key === 'v' &&
        !e.repeat &&
        !e.metaKey &&
        !e.ctrlKey &&
        !e.altKey
      ) {
        e.preventDefault()
        const name = cycleVoice()
        store.getState().setVoice(name)
        silence()
        const demo = createSpeaker()
        speaker.current = demo
        demo.say(`Voice set to ${name.replace(/\(.*?\)/g, '').trim()}. At your service, sir.`)
        void demo.end()
        return
      }

      // G puts the camera on and starts tracking hands. Off by default and
      // never implicit: a webcam that turns itself on because an interface
      // thought it might be useful is not a trade anyone agreed to.
      if (e.key === 'g' && !e.repeat && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        const on = store.getState().gestures
        if (on) {
          hands.disableHands()
          store.getState().setGestures(false)
        } else {
          store.getState().setError(null)
          void hands
            .enableHands()
            .then(() => store.getState().setGestures(true))
            .catch((err: Error) => {
              store.getState().setGestures(false)
              store
                .getState()
                .setError(
                  err?.name === 'NotAllowedError'
                    ? 'Camera access denied — gesture control is unavailable.'
                    : `Gesture control failed to start: ${err?.message ?? err}`,
                )
            })
        }
        return
      }

      // T speaks a fixed line, bypassing the wake word, the recogniser and the
      // model entirely. When "I can't hear him" is the report, this is the one
      // keypress that separates a broken voice engine from a broken voice loop
      // — and it prints the verdict rather than making you infer it.
      if (e.key === 't' && !e.repeat && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        silence()
        const t = createSpeaker()
        speaker.current = t
        t.say('Audio test. If you can hear this, speech output is working, sir.')
        void t.end().then(() => {
          const d = (window as unknown as Record<string, Record<string, unknown>>).__tts
          console.info('[jarvis] audio test →', d)
          if (d && d.started === 0 && d.rescued === 0) {
            store.getState().setError(
              `No sound produced. engine=${d.engine} voice=${d.voice} error=${d.lastError || 'none'}`,
            )
          }
        })
        return
      }

      // Escape cuts him off and stands the whole thing down.
      if (e.key === 'Escape') {
        e.preventDefault()
        stop()
        return
      }

      // Space starts a turn without the wake word. Worth using while filming so
      // a missed wake word doesn't cost a take.
      if (e.code !== 'Space' || e.repeat) return
      e.preventDefault()

      const phase = store.getState().phase
      if (phase !== 'offline' && phase !== 'boot') textMode.current = false
      if (phase === 'offline') {
        void powerOn()
      } else if (phase === 'boot') {
        /* ignore — the boot sequence owns the phase until it finishes */
      } else if (
        phase === 'thinking' ||
        phase === 'tooling' ||
        phase === 'speaking'
      ) {
        onSpeechStart()
        listen(AWAIT_SPEECH_MS)
      } else {
        onWake('')
      }
    }
    window.addEventListener('keydown', onKey)

    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('keydown', onKey)
      clearIdle()
      if (voicePoll.current) clearInterval(voicePoll.current)
      voice.current?.stop()
      speaker.current?.cancel()
      // The camera must not outlive the page that turned it on.
      hands.disableHands()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <>
      <Scene />
      <Hud />
      <Boot />
      <Diagnostics />
      <TypeBox onSend={typed} onStop={stop} />
      {settingsOpen && <Settings onClose={() => store.getState().setSettingsOpen(false)} />}
      <Ignition onStart={() => void powerOn()} />
    </>
  )
}
