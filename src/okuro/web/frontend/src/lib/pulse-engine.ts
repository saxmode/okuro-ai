// <!-- AGENT_HEADER
// role: code
// purpose: provides 2D noise functions and a PulseEngine class for canvas animations
// index: function noise2D | function pNoise | export class PulseEngine
// AGENT_HEADER_END -->
/* ══════════════════════════════════════════════════════════
   PULSE ENGINE — 3 visualization modes on canvas.
   The living heart of okuro: driven by updateActivity,
   updateActiveRoles, and processActivityStream.
   ══════════════════════════════════════════════════════════ */

import { SHEET_CHANGED_EVENT } from './theme'

// ── Types ────────────────────────────────────────────────

export interface PulseActivity {
  calls: number
  live: number
  rate: number
  /** Precomputed 0..1 intensity: blends live-agent count with total tool-call
   *  rate (see usePulseData). Drives size, deformation, heartbeat, node-fade. */
  intensity: number
}

export interface PulseConfig {
  canvas: HTMLCanvasElement
  onStatusText?: (text: string, cursor: boolean) => void
  onModeChange?: (mode: string) => void
  /** When true, the per-frame background fill is replaced with a clearRect
   *  so the canvas composites onto whatever is beneath it. Use in contexts
   *  where the pulse should appear on a non-surface background (e.g. the
   *  onboarding shell which already paints its own background). */
  transparent?: boolean
}

interface CortexNode {
  nx: number; ny: number
  x: number; y: number
  vx: number; vy: number
  brightness: number; refractory: number
  phase: number; r: number
  isCenter?: boolean; fadeIn: number
  label?: string; labelAlpha: number
}

interface CortexEdge {
  a: number; b: number
  pulses: Array<{ t: number; dir: number }>
}

interface CortexState {
  nodes: CortexNode[]
  edges: CortexEdge[]
  heartbeat: number
  hbInterval: number
}

interface SimplexInstance {
  seed: number; radiusMul: number
  offsetX: number; offsetY: number
  alpha: number; speedMul: number
}

interface ActivityEntry {
  ts: string
  tool?: string
  preview?: string
}

// ── Simplex noise (2D) ──────────────────────────────────

const _nP = new Uint8Array(512)
;(function () {
  const p = [
    151,160,137,91,90,15,131,13,201,95,96,53,194,233,7,225,140,36,103,30,
    69,142,8,99,37,240,21,10,23,190,6,148,247,120,234,75,0,26,197,62,94,252,
    219,203,117,35,11,32,57,177,33,88,237,149,56,87,174,20,125,136,171,168,68,
    175,74,165,71,134,139,48,27,166,77,146,158,231,83,111,229,122,60,211,133,
    230,220,105,92,41,55,46,245,40,244,102,143,54,65,25,63,161,1,216,80,73,
    209,76,132,187,208,89,18,169,200,196,135,130,116,188,159,86,164,100,109,
    198,173,186,3,64,52,217,226,250,124,123,5,202,38,147,118,126,255,82,85,
    212,207,206,59,227,47,16,58,17,182,189,28,42,223,183,170,213,119,248,152,
    2,44,154,163,70,221,153,101,155,167,43,172,9,129,22,39,253,19,98,108,
    110,79,113,224,232,178,185,112,104,218,246,97,228,251,34,242,193,238,210,
    144,12,191,179,162,241,81,51,145,235,249,14,239,107,49,192,214,31,181,
    199,106,157,184,84,204,176,115,121,50,45,127,4,150,254,138,236,205,93,
    222,114,67,29,24,72,243,141,128,195,78,66,215,61,156,180,
  ]
  for (let i = 0; i < 256; i++) { _nP[i] = p[i]!; _nP[256 + i] = p[i]! }
})()

const _nG2: [number, number][] = [[1,1],[-1,1],[1,-1],[-1,-1],[1,0],[-1,0],[0,1],[0,-1]]

function noise2D(xin: number, yin: number): number {
  const F2 = 0.5 * (Math.sqrt(3) - 1)
  const G2 = (3 - Math.sqrt(3)) / 6
  const s = (xin + yin) * F2
  const i = Math.floor(xin + s), j = Math.floor(yin + s)
  const t = (i + j) * G2
  const x0 = xin - (i - t), y0 = yin - (j - t)
  const i1 = x0 > y0 ? 1 : 0, j1 = x0 > y0 ? 0 : 1
  const x1 = x0 - i1 + G2, y1 = y0 - j1 + G2
  const x2 = x0 - 1 + 2 * G2, y2 = y0 - 1 + 2 * G2
  const ii = i & 255, jj = j & 255
  const gi0 = _nP[ii + _nP[jj]!]! % 8
  const gi1 = _nP[ii + i1 + _nP[jj + j1]!]! % 8
  const gi2 = _nP[ii + 1 + _nP[jj + 1]!]! % 8
  let n0 = 0, n1 = 0, n2 = 0
  let t0 = 0.5 - x0 * x0 - y0 * y0
  if (t0 > 0) { t0 *= t0; n0 = t0 * t0 * (_nG2[gi0]![0] * x0 + _nG2[gi0]![1] * y0) }
  let t1 = 0.5 - x1 * x1 - y1 * y1
  if (t1 > 0) { t1 *= t1; n1 = t1 * t1 * (_nG2[gi1]![0] * x1 + _nG2[gi1]![1] * y1) }
  let t2 = 0.5 - x2 * x2 - y2 * y2
  if (t2 > 0) { t2 *= t2; n2 = t2 * t2 * (_nG2[gi2]![0] * x2 + _nG2[gi2]![1] * y2) }
  return 35 * (n0 + n1 + n2)
}

/** Returns 0..1 (legacy compat for cortex mode) */
function pNoise(x: number, y: number): number {
  return noise2D(x, y) * 0.5 + 0.5
}

// Brand-green defaults. Previous gray fallback (#888) survived whenever
// getComputedStyle('--color-accent') returned empty or an unresolvable
// var() chain — browsers do that for self-referential custom properties,
// and globals.css defines --color-accent as `var(--color-accent, #22c55e)`.
// Result on some renders: blob stays gray forever. Defaulting to the brand
// hex here means a bad read degrades to correct brand color, not off-brand
// gray. refreshTokens() still overrides when the CSS read succeeds.
const tokens = {
  accent: '#22c55e',
  accentHover: '#16a34a',
  surface: '#0a0a0a',
  accentR: 34,
  accentG: 197,
  accentB: 94,
  surfaceIsDark: true,
  // Pulse blob outline mode — driven by :root CSS vars
  //   --pulse-outlines-only:    "1" | "0"   (default off)
  //   --pulse-outline-strength: px lineWidth (default 1.5)
  // Set via theme.applyPulseOutlineOverride() from Settings → Design.
  outlinesOnly: false,
  outlineStrength: 1.5,
}
let G = 'rgba(34,197,94,'

function _hexLuminance(hex: string): number | null {
  const h = hex.replace('#', '')
  if (!/^[0-9a-f]{6}$/i.test(h)) return null
  const chan = (n: number) => {
    const v = n / 255
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)
  }
  return 0.2126 * chan(parseInt(h.slice(0, 2), 16))
       + 0.7152 * chan(parseInt(h.slice(2, 4), 16))
       + 0.0722 * chan(parseInt(h.slice(4, 6), 16))
}

function _isHex6(s: string): boolean {
  return /^#?[0-9a-f]{6}$/i.test(s.trim())
}

function refreshTokens(): void {
  if (typeof window === 'undefined') return
  const r = getComputedStyle(document.documentElement)
  // Only trust the CSS read if it resolves to a clean 6-digit hex. Browsers
  // can return an unresolved var() chain literally (self-referential custom
  // property edge case), which would poison tokens.accent and paint the
  // blob as garbage. Reject anything non-hex and keep the current value.
  const rawAccent = r.getPropertyValue('--color-accent').trim()
  const rawHover = r.getPropertyValue('--color-accent-hover').trim()
  const rawSurface = r.getPropertyValue('--color-surface').trim()
  const accent = _isHex6(rawAccent) ? rawAccent : tokens.accent
  const accentHover = _isHex6(rawHover) ? rawHover : tokens.accentHover
  const surface = _isHex6(rawSurface) ? rawSurface : tokens.surface
  tokens.accent = accent
  tokens.accentHover = accentHover
  tokens.surface = surface
  const surfaceLum = _hexLuminance(surface)
  if (surfaceLum !== null) tokens.surfaceIsDark = surfaceLum < 0.5
  const rawOutlinesOnly = r.getPropertyValue('--pulse-outlines-only').trim()
  tokens.outlinesOnly = rawOutlinesOnly === '1' || rawOutlinesOnly === 'true'
  const rawStrength = r.getPropertyValue('--pulse-outline-strength').trim()
  const parsedStrength = parseFloat(rawStrength)
  if (Number.isFinite(parsedStrength) && parsedStrength > 0) {
    tokens.outlineStrength = parsedStrength
  }
  const hex = accent.replace('#', '')
  if (/^[0-9a-f]{6}$/i.test(hex)) {
    const rr = parseInt(hex.slice(0, 2), 16)
    const gg = parseInt(hex.slice(2, 4), 16)
    const bb = parseInt(hex.slice(4, 6), 16)
    tokens.accentR = rr
    tokens.accentG = gg
    tokens.accentB = bb
    G = `rgba(${rr},${gg},${bb},`
  }
}

let _tokensDirty = true
function _markTokensDirty() { _tokensDirty = true }
function maybeRefreshTokens(): void {
  if (_tokensDirty) {
    _tokensDirty = false
    refreshTokens()
  }
}

let _tokenObserver: MutationObserver | null = null
function _ensureTokenObserver(): void {
  if (typeof window === 'undefined' || _tokenObserver) return
  // Appearance switches set an attribute on :root; overrides set inline style.
  // Mark dirty on either; the next animation frame picks up new values.
  _tokenObserver = new MutationObserver(_markTokensDirty)
  _tokenObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['style', 'class', 'data-appearance'],
  })

  // THE SHEET SWAP IS AN EVENT, NOT AN ATTRIBUTE, and that distinction is the
  // whole fix. Watching the <link>'s `href` DOES fire — measured, both a
  // head-subtree and a direct observer catch it — but it fires when the
  // attribute is ASSIGNED, before the new sheet is fetched and applied. The
  // canvas then re-read during that gap, got the OLD colour, and cleared its
  // own dirty flag: after switching design system the blob stayed on the
  // previous brand's colour while getComputedStyle already reported the new
  // one. Measured 2026-09-06 — lime accent, yellow blob.
  //
  // `theme.ts::reloadEngineSheet` dispatches this once the sheet has LOADED,
  // which is the moment the values are really readable.
  window.addEventListener(SHEET_CHANGED_EVENT, _markTokensDirty)
}

// ── Tool label mapping ──────────────────────────────────

const TOOL_LABELS: Record<string, string> = {
  'cortex_search':          'searching codebase...',
  'cortex_route':           'navigating codebase...',
  'cortex_read_header':     'reading structure...',
  'cortex_read_section':    'reading code...',
  'cortex_read_file':       'reading file...',
  'cortex_navigate':        'exploring files...',
  'Read':                   'reading...',
  'Edit':                   'coding...',
  'Write':                  'writing file...',
  'Grep':                   'searching patterns...',
  'Glob':                   'finding files...',
  'Bash':                   'executing command...',
  'WebSearch':              'web research...',
  'WebFetch':               'fetching page...',
  'eichi_match':            'evaluating matching role...',
  'eichi_get':              'loading role definition...',
  'eichi_list':             'browsing roles...',
  'hashi_invoke':           'calling LLM bridge...',
  'bootstrap':              'bootstrapping agent...',
  'write_memory':           'writing memory...',
  'read_memory':            'reading memory...',
  'log_progress':           'logging progress...',
  'session_report':         'filing session report...',
  'capture_thought':        'capturing thought...',
  'search_thoughts':        'searching thoughts...',
  'person_add':             'registering person...',
  'person_get':             'loading person profile...',
  'sysinfo_gpu_status':     'checking GPU...',
  'sysinfo_service_health': 'checking services...',
  'keyring_get':            'reading secret...',
  'TodoWrite':              'planning tasks...',
  'Agent':                  'spawning subagent...',
}

const FILE_TOOLS = [
  'Read', 'cortex_read_header', 'cortex_read_section',
  'cortex_read_file', 'Edit', 'Write', 'Grep', 'Glob',
]

// ═════════════════════════════════════════════════════════
// PulseEngine class
// ═════════════════════════════════════════════════════════

export class PulseEngine {
  // Canvas state
  private canvas: HTMLCanvasElement
  private ctx: CanvasRenderingContext2D
  private dpr: number
  private w = 0
  private h = 0
  private zoom = 1
  private zoomTarget = 1
  private panX = 0.5
  private panY = 0.5
  private t = 0
  private t0 = 0

  // Activity
  private activity: PulseActivity = { calls: 0, live: 0, rate: 0, intensity: 0 }
  private actSmooth = 0
  // Forced activity level (0..1), overriding real agent activity while set —
  // see setActivityOverride. null = follow the live data again.
  private actOverride: number | null = null
  // While true the activity EMA settles in ~0.5s instead of ~8s. Set on every
  // override frame and held after release until actSmooth has caught up with
  // real activity, so BOTH entering and leaving an override are visible.
  private actSettleFast = false

  // Cortex state
  private cortexState: CortexState | null = null

  // Simplex blob instances
  private simplexInstances: SimplexInstance[] = [
    { seed: 0,   radiusMul: 1.0,  offsetX: 0,   offsetY: 0,   alpha: 0.5,  speedMul: 1.0 },
    { seed: 50,  radiusMul: 0.75, offsetX: 12,  offsetY: -8,  alpha: 0.35, speedMul: 0.7 },
    { seed: 120, radiusMul: 0.55, offsetX: -8,  offsetY: 10,  alpha: 0.25, speedMul: 1.3 },
    { seed: 200, radiusMul: 0.88, offsetX: -14, offsetY: -6,  alpha: 0.30, speedMul: 0.85 },
    { seed: 275, radiusMul: 0.65, offsetX: 10,  offsetY: 14,  alpha: 0.22, speedMul: 1.15 },
  ]

  // Visualization modes — SIMPLEX is the only shipped visual.
  private visModes = ['SIMPLEX'] as const
  private visMode = 0

  // File queue for cortex node labels
  private fileQueue: string[] = []

  // Active roles (for metaball labels)
  private activeRoles: string[] = []
  private agentsActive = false

  // Status text / typewriter
  private statusTypingTimer: ReturnType<typeof setInterval> | null = null
  private statusHoldTimer: ReturnType<typeof setTimeout> | null = null
  private onStatusText: PulseConfig['onStatusText']
  private transparent = false
  private onModeChange: PulseConfig['onModeChange']

  // Activity stream dedup
  private lastActivityTs = ''

  // Animation frame
  private animFrameId: number | null = null
  private running = false

  // Fullscreen
  private fsCanvas: HTMLCanvasElement | null = null
  private fsCtx: CanvasRenderingContext2D | null = null
  private fsActive = false

  // Event listener cleanup
  private cleanupFns: Array<() => void> = []

  // Resize debounce
  private resizeTimer: ReturnType<typeof setTimeout> | null = null

  constructor(config: PulseConfig) {
    this.canvas = config.canvas
    const ctx = this.canvas.getContext('2d')
    if (!ctx) throw new Error('Could not get 2d context from canvas')
    this.ctx = ctx
    this.dpr = typeof window !== 'undefined' ? (window.devicePixelRatio || 1) : 1
    this.onStatusText = config.onStatusText
    this.onModeChange = config.onModeChange
    this.transparent = config.transparent ?? false

    // Wheel zoom — always toward/away from the canvas center, regardless of
    // pointer position. Earlier the origin tracked the cursor, but that made
    // the blob fly off-screen on accidental wheel events near the edges and
    // confused users who expected a "zoom in toward me" gesture.
    const wheelHandler = (e: WheelEvent) => {
      e.preventDefault()
      this.zoomTarget = Math.max(1, Math.min(5, this.zoomTarget + (e.deltaY > 0 ? -0.15 : 0.15)))
      this.panX = 0.5
      this.panY = 0.5
    }
    this.canvas.addEventListener('wheel', wheelHandler, { passive: false })
    this.cleanupFns.push(() => this.canvas.removeEventListener('wheel', wheelHandler))
  }

  // ── Lifecycle ───────────────────────────────────────────

  start(): void {
    if (this.running) return
    _ensureTokenObserver()
    _markTokensDirty()
    refreshTokens()
    this.running = true
    this.t0 = Date.now()
    this.cortexState = this.cortexInit()

    this.loop()
  }

  stop(): void {
    this.running = false
    if (this.animFrameId !== null) {
      cancelAnimationFrame(this.animFrameId)
      this.animFrameId = null
    }
  }

  resize(): void {
    const parent = this.canvas.parentElement
    if (!parent) return
    const newW = parent.clientWidth
    const newH = parent.clientHeight
    if (newW < 1 || newH < 1) return

    this.w = newW
    this.h = newH

    if (this.resizeTimer !== null) clearTimeout(this.resizeTimer)
    this.resizeTimer = setTimeout(() => {
      this.canvas.width = this.w * this.dpr
      this.canvas.height = this.h * this.dpr
      this.canvas.style.width = this.w + 'px'
      this.canvas.style.height = this.h + 'px'
      this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0)
    }, 200)
  }

  /** Immediately size the canvas buffer (no debounce) — call on mount. */
  sizeImmediate(): void {
    const parent = this.canvas.parentElement
    if (!parent) return
    this.w = parent.clientWidth
    this.h = parent.clientHeight
    if (this.w < 1 || this.h < 1) return
    this.canvas.width = this.w * this.dpr
    this.canvas.height = this.h * this.dpr
    this.canvas.style.width = this.w + 'px'
    this.canvas.style.height = this.h + 'px'
    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0)
  }

  destroy(): void {
    this.stop()
    if (this.statusTypingTimer !== null) { clearInterval(this.statusTypingTimer); this.statusTypingTimer = null }
    if (this.statusHoldTimer !== null) { clearTimeout(this.statusHoldTimer); this.statusHoldTimer = null }
    if (this.resizeTimer !== null) { clearTimeout(this.resizeTimer); this.resizeTimer = null }
    for (const fn of this.cleanupFns) fn()
    this.cleanupFns = []
    this.fsCanvas = null
    this.fsCtx = null
    this.fsActive = false
    this.cortexState = null
  }

  // ── Data update methods ─────────────────────────────────

  updateActivity(activity: PulseActivity): void {
    this.activity = { ...activity }
  }

  /**
   * Force the orb's activity level, ignoring real agent activity until
   * released with null. Used to make okuro LOOK like it is talking while the
   * morning brief plays (see BriefIndicator) — the orb is the mouth, so a
   * playing brief has to read as a busy pulse rather than an idle one.
   */
  setActivityOverride(level: number | null): void {
    this.actOverride = level === null ? null : Math.max(0, Math.min(1, level))
  }

  updateActiveRoles(roles: string[], active: boolean): void {
    this.activeRoles = roles
    this.agentsActive = active
  }

  processActivityStream(entries: ActivityEntry[]): void {
    if (!entries || !entries.length) return
    const newEntries: ActivityEntry[] = []
    for (const entry of entries) {
      if (entry.ts > this.lastActivityTs) newEntries.push(entry)
    }
    if (!newEntries.length) return
    this.lastActivityTs = newEntries[newEntries.length - 1]!.ts

    for (const e of newEntries) {
      const name = e.tool || ''
      const preview = e.preview || ''

      // File-read tools -> cortex node labels
      if (FILE_TOOLS.indexOf(name) >= 0 && preview.length > 2) {
        this.fileQueue.push(preview)
        if (this.fileQueue.length > 20) this.fileQueue.shift()
      }

      // Typewriter status text
      const label = TOOL_LABELS[name]
      if (label) {
        this.showStatus(label)
      } else if (name.indexOf('mcp__') === 0) {
        const parts = name.split('__')
        const short = parts[parts.length - 1]!.replace(/_/g, ' ')
        this.showStatus(short + '...')
      } else if (name && !TOOL_LABELS[name]) {
        this.showStatus(name.replace(/_/g, ' ') + '...')
      }
    }
  }

  // ── Mode switching ──────────────────────────────────────

  cycleMode(): void {
    this.visMode = (this.visMode + 1) % this.visModes.length
    this.onModeChange?.(this.visModes[this.visMode]!)
  }

  getMode(): string {
    return this.visModes[this.visMode]!
  }

  setMode(idx: number): void {
    if (idx >= 0 && idx < this.visModes.length) {
      this.visMode = idx
      this.onModeChange?.(this.visModes[this.visMode]!)
    }
  }

  // ── Fullscreen ──────────────────────────────────────────

  enterFullscreen(fsCanvas: HTMLCanvasElement): void {
    this.fsCanvas = fsCanvas
    const ctx = fsCanvas.getContext('2d')
    if (!ctx) return
    this.fsCtx = ctx
    this.fsActive = true

    const w = window.innerWidth, h = window.innerHeight
    const dpr = this.dpr
    fsCanvas.width = w * dpr
    fsCanvas.height = h * dpr
    fsCanvas.style.width = w + 'px'
    fsCanvas.style.height = h + 'px'
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  }

  exitFullscreen(): void {
    this.fsActive = false
    this.fsCanvas = null
    this.fsCtx = null
  }

  get isFullscreen(): boolean {
    return this.fsActive
  }

  // ── SVG snapshot ────────────────────────────────────────

  /**
   * Capture the current blob frame as an inline SVG string suitable for
   * pasting into Figma. Recomputes the same geometry the canvas is drawing
   * at this instant (same simplex noise, same instance offsets, same
   * Catmull-Rom→bezier spline math) and emits radial-gradient fills plus
   * Gaussian blur filters to approximate the layered glow stack.
   *
   * Blend mode: canvas uses 'lighter' (additive) on dark surfaces and
   * 'multiply' on light surfaces. The closest SVG/CSS analogues are
   * 'screen' and 'multiply' respectively — visually close enough for
   * design handoff, not pixel-identical to the canvas render.
   */
  snapshotSVG(): string {
    const w = this.w
    const h = this.h
    const cx = w / 2
    const cy = h / 2
    const baseR = Math.min(w, h) * (0.12 + this.actSmooth * 0.22)
    const a = this.actSmooth
    const N = 10
    const t = this.t

    const layerDefs = [
      { blur: 40, alpha: 0.06 + a * 0.04, scale: 1.3 },
      { blur: 25, alpha: 0.1 + a * 0.06, scale: 1.15 },
      { blur: 12, alpha: 0.15 + a * 0.08, scale: 1.05 },
      { blur: 0, alpha: 0.7 + a * 0.3, scale: 1.0 },
    ]

    const blendMode = tokens.surfaceIsDark ? 'screen' : 'multiply'
    const gradientDefs: string[] = []
    const filterDefs: string[] = []
    let paths = ''

    layerDefs.forEach((L, li) => {
      if (L.blur > 0) {
        filterDefs.push(
          `<filter id="blur${li}" x="-50%" y="-50%" width="200%" height="200%">` +
            `<feGaussianBlur stdDeviation="${L.blur}"/>` +
            `</filter>`,
        )
      }
    })

    for (let ii = 0; ii < this.simplexInstances.length; ii++) {
      const inst = this.simplexInstances[ii]!
      const icx = cx + inst.offsetX
      const icy = cy + inst.offsetY
      const R = baseR * inst.radiusMul
      const breathe = 1 + 0.08 * Math.sin(t * 0.8 + inst.seed)
      const amp = 0.15 + a * 1.2
      const speed = 0.1 * inst.speedMul

      const pts: Array<{ x: number; y: number }> = []
      for (let j = 0; j < N; j++) {
        const angle = (j / N) * Math.PI * 2
        const nv = noise2D(
          Math.cos(angle) * 1.5 + t * speed + inst.seed,
          Math.sin(angle) * 1.5 + t * speed * 0.7 + inst.seed,
        )
        const pr = R * breathe * (1 + nv * amp)
        pts.push({ x: icx + Math.cos(angle) * pr, y: icy + Math.sin(angle) * pr })
      }

      const gradId = `pulse-grad-${ii}`
      gradientDefs.push(
        `<radialGradient id="${gradId}" cx="${icx.toFixed(2)}" cy="${icy.toFixed(2)}" ` +
          `r="${(R * 1.8).toFixed(2)}" gradientUnits="userSpaceOnUse">` +
          `<stop offset="0" stop-color="${tokens.accent}"/>` +
          `<stop offset="0.6" stop-color="${tokens.accentHover}"/>` +
          `<stop offset="1" stop-color="${tokens.surface}"/>` +
          `</radialGradient>`,
      )

      if (tokens.outlinesOnly) {
        // Single stroked path per instance.
        let d = ''
        for (let j = 0; j < pts.length; j++) {
          const p0 = pts[(j - 1 + N) % N]!
          const p1 = pts[j]!
          const p2 = pts[(j + 1) % N]!
          const p3 = pts[(j + 2) % N]!
          const cp1x = p1.x + (p2.x - p0.x) / 4
          const cp1y = p1.y + (p2.y - p0.y) / 4
          const cp2x = p2.x - (p3.x - p1.x) / 4
          const cp2y = p2.y - (p3.y - p1.y) / 4
          if (j === 0) d += `M${p1.x.toFixed(2)},${p1.y.toFixed(2)} `
          d +=
            `C${cp1x.toFixed(2)},${cp1y.toFixed(2)} ` +
            `${cp2x.toFixed(2)},${cp2y.toFixed(2)} ` +
            `${p2.x.toFixed(2)},${p2.y.toFixed(2)} `
        }
        d += 'Z'
        const opacity = Math.min(1, inst.alpha * (0.7 + a * 0.3) * 2.2).toFixed(3)
        paths +=
          `<path d="${d}" fill="none" stroke="${tokens.accent}" ` +
          `stroke-width="${tokens.outlineStrength}" stroke-linejoin="round" ` +
          `stroke-opacity="${opacity}"/>`
      } else {
        layerDefs.forEach((L, li) => {
          const scaled: Array<{ x: number; y: number }> = pts.map((p) => ({
            x: icx + (p.x - icx) * L.scale,
            y: icy + (p.y - icy) * L.scale,
          }))
          let d = ''
          for (let j = 0; j < scaled.length; j++) {
            const p0 = scaled[(j - 1 + N) % N]!
            const p1 = scaled[j]!
            const p2 = scaled[(j + 1) % N]!
            const p3 = scaled[(j + 2) % N]!
            const cp1x = p1.x + (p2.x - p0.x) / 4
            const cp1y = p1.y + (p2.y - p0.y) / 4
            const cp2x = p2.x - (p3.x - p1.x) / 4
            const cp2y = p2.y - (p3.y - p1.y) / 4
            if (j === 0) d += `M${p1.x.toFixed(2)},${p1.y.toFixed(2)} `
            d +=
              `C${cp1x.toFixed(2)},${cp1y.toFixed(2)} ` +
              `${cp2x.toFixed(2)},${cp2y.toFixed(2)} ` +
              `${p2.x.toFixed(2)},${p2.y.toFixed(2)} `
          }
          d += 'Z'

          const opacity = (L.alpha * inst.alpha).toFixed(3)
          const filter = L.blur > 0 ? ` filter="url(#blur${li})"` : ''
          paths +=
            `<path d="${d}" fill="url(#${gradId})" fill-opacity="${opacity}" ` +
            `style="mix-blend-mode:${blendMode}"${filter}/>`
        })
      }
    }

    // Cortex layer — emit edges, edge pulses, nodes (+glow), and labels
    // BEFORE the blob, mirroring the canvas draw order (cortexDraw runs
    // before simplexBlobDraw inside drawCurrentMode).
    let cortex = ''
    if (this.cortexState) {
      const s = this.cortexState
      const spread = Math.min(w, h) * (0.15 + this.actSmooth * 0.35)
      for (let i = 0; i < s.nodes.length; i++) {
        s.nodes[i]!.x = cx + s.nodes[i]!.nx * spread
        s.nodes[i]!.y = cy + s.nodes[i]!.ny * spread
      }

      // Edges + pulses
      for (let e = 0; e < s.edges.length; e++) {
        const edge = s.edges[e]!
        const na = s.nodes[edge.a]!
        const nb = s.nodes[edge.b]!
        const edgeFade = Math.min(na.fadeIn, nb.fadeIn)
        if (edgeFade < 0.01) continue
        const ea = (Math.max(na.brightness, nb.brightness) * 0.15 + 0.02) * edgeFade
        cortex +=
          `<line x1="${na.x.toFixed(2)}" y1="${na.y.toFixed(2)}" ` +
          `x2="${nb.x.toFixed(2)}" y2="${nb.y.toFixed(2)}" ` +
          `stroke="${G}${ea.toFixed(3)})" stroke-width="0.5"/>`
        for (let p = 0; p < edge.pulses.length; p++) {
          const pt = edge.pulses[p]!.t
          const fromN = edge.pulses[p]!.dir > 0 ? na : nb
          const toN = edge.pulses[p]!.dir > 0 ? nb : na
          const px = fromN.x + (toN.x - fromN.x) * pt
          const py = fromN.y + (toN.y - fromN.y) * pt
          cortex +=
            `<circle cx="${px.toFixed(2)}" cy="${py.toFixed(2)}" r="2" ` +
            `fill="${G}${(0.8 * edgeFade).toFixed(3)})"/>`
        }
      }

      // Nodes + glow — center node (idx 0) is an invisible edge anchor;
      // skip its dot + halo, matching the canvas draw.
      for (let i = 0; i < s.nodes.length; i++) {
        const n = s.nodes[i]!
        if (n.isCenter) continue
        if (n.fadeIn < 0.01) continue
        const ba = (0.1 + n.brightness * 0.9) * n.fadeIn
        const drawR = n.r
        cortex +=
          `<circle cx="${n.x.toFixed(2)}" cy="${n.y.toFixed(2)}" ` +
          `r="${drawR.toFixed(2)}" fill="${G}${ba.toFixed(3)})"/>`
        if (n.brightness > 0.3) {
          const glowR = drawR * 3
          const glowA = n.brightness * 0.06 * n.fadeIn
          cortex +=
            `<circle cx="${n.x.toFixed(2)}" cy="${n.y.toFixed(2)}" ` +
            `r="${glowR.toFixed(2)}" fill="${G}${glowA.toFixed(3)})"/>`
        }
      }

      // Labels (skip center node, idx 0 — matches cortexDraw loop start at 1)
      for (let i = 1; i < s.nodes.length; i++) {
        const n = s.nodes[i]!
        if (!n.label || n.labelAlpha < 0.02 || n.fadeIn < 0.1) continue
        const labelAlpha = (n.labelAlpha * n.fadeIn * 0.7).toFixed(3)
        const lx = (n.x + n.r + 4).toFixed(2)
        const ly = n.y.toFixed(2)
        const safeLabel = n.label
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
        cortex +=
          `<text x="${lx}" y="${ly}" fill="${tokens.accent}" ` +
          `fill-opacity="${labelAlpha}" font-family="JetBrains Mono, monospace" ` +
          `font-size="9" dominant-baseline="middle">${safeLabel}</text>`
      }
    }

    return (
      `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" ` +
      `viewBox="0 0 ${w} ${h}">` +
      `<defs>${filterDefs.join('')}${gradientDefs.join('')}</defs>` +
      `<rect width="100%" height="100%" fill="${tokens.surface}"/>` +
      `<g>${cortex}</g>` +
      `<g>${paths}</g>` +
      `</svg>`
    )
  }

  // ── Status text (typewriter effect via callback) ────────

  private showStatus(text: string): void {
    if (this.statusTypingTimer !== null) clearInterval(this.statusTypingTimer)
    if (this.statusHoldTimer !== null) clearTimeout(this.statusHoldTimer)

    let i = 0
    let display = ''
    this.onStatusText?.('', true)

    this.statusTypingTimer = setInterval(() => {
      if (i < text.length) {
        display += text[i]; i++
        this.onStatusText?.(display, true)
      } else {
        if (this.statusTypingTimer !== null) clearInterval(this.statusTypingTimer)
        this.statusTypingTimer = null
        this.onStatusText?.(display, true)
        // Hold for 8s, then hide
        this.statusHoldTimer = setTimeout(() => {
          this.onStatusText?.('', false)
        }, 8000)
      }
    }, 35)
  }

  // ── Cortex neural network ──────────────────────────────

  private cortexInit(): CortexState {
    const nodes: CortexNode[] = []
    const edges: CortexEdge[] = []
    const count = 50

    // Node 0 is center — knowledge converges here
    nodes.push({
      nx: 0, ny: 0, x: 0, y: 0,
      vx: 0, vy: 0, brightness: 0, refractory: 0,
      phase: 0, r: 2.5, isCenter: true, fadeIn: 1,
      labelAlpha: 0,
    })

    for (let i = 1; i <= count; i++) {
      const angle = Math.random() * Math.PI * 2
      const dist = 0.15 + Math.random() * 0.85
      nodes.push({
        nx: Math.cos(angle) * dist,
        ny: Math.sin(angle) * dist,
        x: 0, y: 0,
        vx: 0, vy: 0, brightness: 0, refractory: 0,
        phase: Math.random() * Math.PI * 2,
        r: 1.5 + Math.random() * 1,
        fadeIn: 0, labelAlpha: 0,
      })
    }

    // Connect every outer node to center
    for (let i = 1; i < nodes.length; i++) {
      edges.push({ a: i, b: 0, pulses: [] })
    }

    // Some peer connections for organic feel
    for (let i = 1; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const dx = nodes[i]!.nx - nodes[j]!.nx
        const dy = nodes[i]!.ny - nodes[j]!.ny
        if (Math.sqrt(dx * dx + dy * dy) < 0.35) {
          edges.push({ a: i, b: j, pulses: [] })
          break
        }
      }
    }

    return { nodes, edges, heartbeat: 0, hbInterval: 5 }
  }

  private cortexUpdate(s: CortexState): void {
    const t = this.t
    const actN = this.actSmooth
    const visibleCount = Math.floor(8 + actN * 42)

    // Heartbeat: faster when busy (5s idle -> 0.8s busy)
    s.hbInterval = 5 - actN * 4.2
    s.heartbeat += 0.016
    if (s.heartbeat > s.hbInterval) {
      s.heartbeat = 0
      const ci = Math.floor(Math.random() * s.nodes.length)
      if (s.nodes[ci]!.fadeIn > 0.1) {
        s.nodes[ci]!.brightness = 1
        s.nodes[ci]!.refractory = 30
        // Label priority: active agents first (provider / role names),
        // file paths only as a fallback when nothing is running. The
        // network is "who is alive," not "what files got read" — the
        // fileQueue is kept as the secondary source so the viz still
        // has something to whisper when the system is idle.
        if (this.agentsActive && this.activeRoles.length > 0) {
          const ri = Math.floor(Math.random() * this.activeRoles.length)
          s.nodes[ci]!.label = this.activeRoles[ri]
          s.nodes[ci]!.labelAlpha = 1.0
        } else if (this.fileQueue.length > 0) {
          s.nodes[ci]!.label = this.fileQueue.shift()
          s.nodes[ci]!.labelAlpha = 1.0
        }
      }
    }

    // Spontaneous firing: more frequent when busy
    if (Math.random() < 0.002 + actN * 0.015) {
      const ri = Math.floor(Math.random() * s.nodes.length)
      if (s.nodes[ri]!.refractory <= 0 && s.nodes[ri]!.fadeIn > 0.1) {
        s.nodes[ri]!.brightness = 1
        s.nodes[ri]!.refractory = 30
      }
    }

    // Pulse travel speed
    const pulseSpeed = 0.02 + actN * 0.03

    for (let i = 0; i < s.nodes.length; i++) {
      const n = s.nodes[i]!
      const fadeTarget = (i === 0 || i < visibleCount) ? 1 : 0
      n.fadeIn += (fadeTarget - n.fadeIn) * 0.015
      n.brightness *= 0.965
      if (n.refractory > 0) n.refractory--

      // Fade file labels
      if (n.labelAlpha > 0) n.labelAlpha *= 0.992
      if (n.labelAlpha < 0.02) { n.labelAlpha = 0; n.label = '' }

      if (n.brightness > 0.8 && n.refractory === 29 && n.fadeIn > 0.3) {
        for (let e = 0; e < s.edges.length; e++) {
          const edge = s.edges[e]!
          if (edge.a === i || edge.b === i) {
            let dir: number
            if (edge.b === 0) dir = 1
            else if (edge.a === 0) dir = -1
            else dir = edge.a === i ? 1 : -1
            edge.pulses.push({ t: 0, dir })
          }
        }
      }

      // Center node stays pinned
      if (n.isCenter) continue

      // Drift with noise
      const driftStr = 0.002 + actN * 0.004
      n.nx += (pNoise(n.nx * 3 + t * 0.5, n.ny * 3) - 0.5) * driftStr
      n.ny += (pNoise(n.nx * 3, n.ny * 3 + t * 0.5) - 0.5) * driftStr

      // Slow orbit around center
      const orbitSpeed = 0.0002 + actN * 0.0005
      const ang = Math.atan2(n.ny, n.nx)
      n.nx += Math.cos(ang + Math.PI * 0.5) * orbitSpeed
      n.ny += Math.sin(ang + Math.PI * 0.5) * orbitSpeed

      // Gravity
      const nd = Math.sqrt(n.nx * n.nx + n.ny * n.ny) + 0.01
      if (nd > 0.9) { n.nx -= n.nx / nd * 0.008; n.ny -= n.ny / nd * 0.008 }
      if (nd < 0.25) { n.nx += n.nx / nd * 0.003; n.ny += n.ny / nd * 0.003 }
    }

    // Update pulses along edges
    for (let e = 0; e < s.edges.length; e++) {
      const edge = s.edges[e]!
      const alive: Array<{ t: number; dir: number }> = []
      for (let p = 0; p < edge.pulses.length; p++) {
        edge.pulses[p]!.t += pulseSpeed
        if (edge.pulses[p]!.t < 1) {
          alive.push(edge.pulses[p]!)
        } else {
          const tgt = edge.pulses[p]!.dir > 0 ? edge.b : edge.a
          if (s.nodes[tgt]!.refractory <= 0) {
            s.nodes[tgt]!.brightness = Math.max(s.nodes[tgt]!.brightness, 0.7)
            s.nodes[tgt]!.refractory = 20
          }
        }
      }
      edge.pulses = alive
    }
  }

  private cortexDraw(s: CortexState): void {
    const ctx = this.ctx
    if (this.transparent) {
      // Composite onto whatever is beneath the canvas instead of painting a
      // solid surface fill. The pulse still renders its blobs / nodes on top.
      ctx.clearRect(0, 0, this.w + 1, this.h + 1)
    } else {
      ctx.fillStyle = tokens.surface
      ctx.fillRect(0, 0, this.w + 1, this.h + 1)
    }

    const cx = this.w / 2, cy = this.h / 2
    const spread = Math.min(this.w, this.h) * (0.15 + this.actSmooth * 0.35)

    // Compute pixel positions
    for (let i = 0; i < s.nodes.length; i++) {
      s.nodes[i]!.x = cx + s.nodes[i]!.nx * spread
      s.nodes[i]!.y = cy + s.nodes[i]!.ny * spread
    }

    // Edges
    for (let e = 0; e < s.edges.length; e++) {
      const edge = s.edges[e]!
      const na = s.nodes[edge.a]!, nb = s.nodes[edge.b]!
      const edgeFade = Math.min(na.fadeIn, nb.fadeIn)
      if (edgeFade < 0.01) continue
      const ea = (Math.max(na.brightness, nb.brightness) * 0.15 + 0.02) * edgeFade
      ctx.beginPath(); ctx.moveTo(na.x, na.y); ctx.lineTo(nb.x, nb.y)
      ctx.strokeStyle = G + ea.toFixed(3) + ')'; ctx.lineWidth = 0.5; ctx.stroke()

      // Pulses along edge
      for (let p = 0; p < edge.pulses.length; p++) {
        const pt = edge.pulses[p]!.t
        const fromN = edge.pulses[p]!.dir > 0 ? na : nb
        const toN = edge.pulses[p]!.dir > 0 ? nb : na
        const px = fromN.x + (toN.x - fromN.x) * pt
        const py = fromN.y + (toN.y - fromN.y) * pt
        ctx.beginPath(); ctx.arc(px, py, 2, 0, Math.PI * 2)
        ctx.fillStyle = G + (0.8 * edgeFade).toFixed(3) + ')'; ctx.fill()
      }
    }

    // Nodes — center node (idx 0) is an invisible edge anchor; skip its
    // visible dot + halo to keep the canvas center clean.
    for (let i = 0; i < s.nodes.length; i++) {
      const n = s.nodes[i]!
      if (n.isCenter) continue
      if (n.fadeIn < 0.01) continue
      const ba = (0.1 + n.brightness * 0.9) * n.fadeIn
      const drawR = n.r
      ctx.beginPath(); ctx.arc(n.x, n.y, drawR, 0, Math.PI * 2)
      ctx.fillStyle = G + ba.toFixed(3) + ')'; ctx.fill()

      if (n.brightness > 0.3) {
        const glowR = drawR * 3
        const glowA = n.brightness * 0.06 * n.fadeIn
        ctx.beginPath(); ctx.arc(n.x, n.y, glowR, 0, Math.PI * 2)
        ctx.fillStyle = G + glowA.toFixed(3) + ')'; ctx.fill()
      }
    }

    // File labels on nodes
    ctx.font = '9px JetBrains Mono, monospace'
    ctx.textAlign = 'left'
    ctx.textBaseline = 'middle'
    for (let i = 1; i < s.nodes.length; i++) {
      const n = s.nodes[i]!
      if (!n.label || n.labelAlpha < 0.02 || n.fadeIn < 0.1) continue
      ctx.globalAlpha = n.labelAlpha * n.fadeIn * 0.7
      ctx.fillStyle = tokens.accent
      ctx.fillText(n.label, n.x + n.r + 4, n.y)
    }
    ctx.globalAlpha = 1
  }

  // ── Simplex blob ────────────────────────────────────────

  private simplexBlobDraw(ctx: CanvasRenderingContext2D, t: number): void {
    const cx = this.w / 2, cy = this.h / 2
    const baseR = Math.min(this.w, this.h) * (0.12 + this.actSmooth * 0.22)
    const a = this.actSmooth
    const N = 10

    ctx.save()
    // 'lighter' is additive — looks great on dark surfaces, washes out
    // on light. 'multiply' darkens, which gives the same volumetric
    // glow feel against a light background. Switch by surface luminance.
    // Outlines-only mode prefers source-over so overlapping strokes don't
    // blow out to white where instances cross.
    ctx.globalCompositeOperation = tokens.outlinesOnly
      ? 'source-over'
      : (tokens.surfaceIsDark ? 'lighter' : 'multiply')

    for (let ii = 0; ii < this.simplexInstances.length; ii++) {
      const inst = this.simplexInstances[ii]!
      const icx = cx + inst.offsetX
      const icy = cy + inst.offsetY
      const R = baseR * inst.radiusMul
      const breathe = 1 + 0.08 * Math.sin(t * 0.8 + inst.seed)
      // Intensity → more deformation (star-shaped), NOT more speed
      const amp = 0.15 + a * 1.2
      const speed = 0.1 * inst.speedMul

      const pts: Array<{ x: number; y: number }> = []
      for (let j = 0; j < N; j++) {
        const angle = (j / N) * Math.PI * 2
        const nv = noise2D(
          Math.cos(angle) * 1.5 + t * speed + inst.seed,
          Math.sin(angle) * 1.5 + t * speed * 0.7 + inst.seed,
        )
        const pr = R * breathe * (1 + nv * amp)
        pts.push({ x: icx + Math.cos(angle) * pr, y: icy + Math.sin(angle) * pr })
      }

      if (tokens.outlinesOnly) {
        // Single stroked path per simplex instance — skip the glow stack.
        // inst.alpha preserves the layered depth feel across instances.
        ctx.save()
        ctx.filter = 'none'
        ctx.globalAlpha = Math.min(1, inst.alpha * (0.7 + a * 0.3) * 2.2)
        ctx.beginPath()
        for (let j = 0; j < pts.length; j++) {
          const p0 = pts[(j - 1 + N) % N]!
          const p1 = pts[j]!
          const p2 = pts[(j + 1) % N]!
          const p3 = pts[(j + 2) % N]!
          const cp1x = p1.x + (p2.x - p0.x) / 4
          const cp1y = p1.y + (p2.y - p0.y) / 4
          const cp2x = p2.x - (p3.x - p1.x) / 4
          const cp2y = p2.y - (p3.y - p1.y) / 4
          if (j === 0) ctx.moveTo(p1.x, p1.y)
          ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y)
        }
        ctx.closePath()
        ctx.strokeStyle = tokens.accent
        ctx.lineWidth = tokens.outlineStrength
        ctx.lineJoin = 'round'
        ctx.stroke()
        ctx.restore()
        continue
      }

      // Glow layers
      const layers = [
        { blur: 40, alpha: (0.06 + a * 0.04) * inst.alpha, scale: 1.3 },
        { blur: 25, alpha: (0.1 + a * 0.06) * inst.alpha, scale: 1.15 },
        { blur: 12, alpha: (0.15 + a * 0.08) * inst.alpha, scale: 1.05 },
        { blur: 0, alpha: (0.7 + a * 0.3) * inst.alpha, scale: 1.0 },
      ]

      for (let li = 0; li < layers.length; li++) {
        const L = layers[li]!
        ctx.save()
        ctx.filter = L.blur > 0 ? 'blur(' + L.blur + 'px)' : 'none'
        ctx.globalAlpha = L.alpha

        const scaled: Array<{ x: number; y: number }> = []
        for (let j = 0; j < pts.length; j++) {
          scaled.push({
            x: icx + (pts[j]!.x - icx) * L.scale,
            y: icy + (pts[j]!.y - icy) * L.scale,
          })
        }

        // Catmull-Rom spline
        ctx.beginPath()
        for (let j = 0; j < scaled.length; j++) {
          const p0 = scaled[(j - 1 + N) % N]!
          const p1 = scaled[j]!
          const p2 = scaled[(j + 1) % N]!
          const p3 = scaled[(j + 2) % N]!
          const cp1x = p1.x + (p2.x - p0.x) / 4
          const cp1y = p1.y + (p2.y - p0.y) / 4
          const cp2x = p2.x - (p3.x - p1.x) / 4
          const cp2y = p2.y - (p3.y - p1.y) / 4
          if (j === 0) ctx.moveTo(p1.x, p1.y)
          ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y)
        }
        ctx.closePath()

        const grad = ctx.createRadialGradient(icx, icy, 0, icx, icy, R * 1.8)
        grad.addColorStop(0, tokens.accent)
        grad.addColorStop(0.6, tokens.accentHover)
        grad.addColorStop(1, tokens.surface)
        ctx.fillStyle = grad
        ctx.fill()
        ctx.restore()
      }
    }

    ctx.restore()
  }

  // ── Draw current mode (cortex bg + foreground layer) ────

  private drawCurrentMode(ctx: CanvasRenderingContext2D, t: number): void {
    if (!this.cortexState) return
    this.cortexUpdate(this.cortexState)
    this.cortexDraw(this.cortexState)

    // Only SIMPLEX is active — cycling is disabled in the UI.
    this.simplexBlobDraw(ctx, t)
  }

  // ── Main animation loop ─────────────────────────────────

  private loop = (): void => {
    if (!this.running) return

    maybeRefreshTokens()
    this.t = (Date.now() - this.t0) * 0.001

    // Global activity EMA — ~8s settle (0.002 at 60fps).
    // Intensity blends live-agent count + total tool-call rate upstream
    // (see usePulseData.computeIntensity). Old formula `rate/30` was flat
    // for sub-1/s sessions and ignored liveness entirely.
    //
    // Source precedence: setActivityOverride (brief playback) > the debug hook
    // (window.__pulseDebugActivity, 0..1) > real activity. An override settles
    // fast so the orb answers the click; releasing it settles fast too, so the
    // orb drops back to listening instead of coasting for 8s on a stale level.
    const _dbg = (typeof window !== 'undefined' && (window as any).__pulseDebugActivity) as number | undefined
    const forced = this.actOverride ?? _dbg
    const actRaw = forced != null ? forced : this.activity.intensity
    if (forced != null) this.actSettleFast = true
    else if (this.actSettleFast && Math.abs(actRaw - this.actSmooth) < 0.02) this.actSettleFast = false
    this.actSmooth += (actRaw - this.actSmooth) * (this.actSettleFast ? 0.03 : 0.002)

    // Smooth zoom
    this.zoom += (this.zoomTarget - this.zoom) * 0.08

    this.ctx.save()
    if (this.zoom > 1.01) {
      const ox = this.panX * this.w
      const oy = this.panY * this.h
      this.ctx.translate(ox, oy)
      this.ctx.scale(this.zoom, this.zoom)
      this.ctx.translate(-ox, -oy)
    }

    this.drawCurrentMode(this.ctx, this.t)

    this.ctx.restore()

    // Mirror to fullscreen canvas if active
    if (this.fsActive && this.fsCtx && this.fsCanvas) {
      const fw = window.innerWidth, fh = window.innerHeight
      const savedW = this.w, savedH = this.h, savedCtx = this.ctx

      this.w = fw; this.h = fh; this.ctx = this.fsCtx
      this.fsCtx.fillStyle = tokens.surface
      this.fsCtx.fillRect(0, 0, fw, fh)
      this.drawCurrentMode(this.fsCtx, this.t)

      this.w = savedW; this.h = savedH; this.ctx = savedCtx
    }

    this.animFrameId = requestAnimationFrame(this.loop)
  }

  // ── Public getters for React overlay positioning ────────

  get activityLevel(): number {
    return this.actSmooth
  }
}
