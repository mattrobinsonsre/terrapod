'use client'

// ResourceUsage — surface the runner Job's actual memory + CPU usage
// alongside its configured requests and limits (#430). Renders only
// when peak data is available (post-#430 runs); silent for older runs.
//
// Hard rule: peak is ALWAYS shown next to requested + limit. Standalone
// "Peak: 3.8 Gi" with no anchor is meaningless to an operator — the
// whole point is "how close is peak to the cliff?". The bar chart's
// 100% marker is the limit so proximity is visible at a glance.

interface ResourceUsageProps {
  resourceMemory: string // "2Gi", "4Gi", "64Mi", etc.
  peakMemoryBytes: number | null
  runnerExitStatus: string // "" | "clean" | "oom" | "killed" | "error"
}

// CPU is intentionally NOT rendered here. peak_cpu_usec is cumulative
// core-time over the whole run; comparing it to the cores-allocated
// limit requires dividing by phase wall-clock, and even then the
// resulting *average* utilisation can hide bursts that briefly peg
// the limit. A proper CPU panel needs instantaneous sampling, not
// cumulative-counter math — tracked as a follow-up to #430. The
// backend still records peak_cpu_usec so the data is there when the
// sampling layer lands.

// Parse a K8s memory quantity string to bytes. Supports Ei/Pi/Ti/Gi/Mi/Ki
// (binary) and E/P/T/G/M/K (decimal). Returns NaN on parse failure.
function parseMemoryToBytes(s: string): number {
  const m = /^([0-9]+(?:\.[0-9]+)?)([EPTGMK]i?)?$/.exec(s.trim())
  if (!m) return NaN
  const n = parseFloat(m[1])
  const unit = m[2] ?? ''
  const factors: Record<string, number> = {
    '': 1,
    K: 1e3,
    M: 1e6,
    G: 1e9,
    T: 1e12,
    P: 1e15,
    E: 1e18,
    Ki: 1 << 10,
    Mi: 1 << 20,
    Gi: 1 << 30,
    Ti: 2 ** 40,
    Pi: 2 ** 50,
    Ei: 2 ** 60,
  }
  return n * (factors[unit] ?? NaN)
}

// Render a byte count using the smallest binary unit that gives a value ≥ 1.
function humanBytes(n: number): string {
  if (!Number.isFinite(n)) return '?'
  for (const [unit, scale] of [
    ['Gi', 1 << 30],
    ['Mi', 1 << 20],
    ['Ki', 1 << 10],
  ] as const) {
    if (n >= scale) return `${(n / scale).toFixed(2)} ${unit}`
  }
  return `${n} B`
}

export function ResourceUsage({
  resourceMemory,
  peakMemoryBytes,
  runnerExitStatus,
}: ResourceUsageProps) {
  // Only render when we have anything to show — peak from runner, OR an
  // abnormal exit signal from the listener (oom / killed). For runs that
  // pre-date #430, both are null/empty and the panel stays hidden.
  if (peakMemoryBytes === null && !runnerExitStatus) {
    return null
  }

  const reqMem = parseMemoryToBytes(resourceMemory)
  const limitMem = Number.isFinite(reqMem) ? reqMem * 2 : NaN

  const memPct =
    peakMemoryBytes !== null && Number.isFinite(limitMem) && limitMem > 0
      ? Math.min(100, (peakMemoryBytes / limitMem) * 100)
      : null

  // Bar colour: red ≥95% (OOM cliff), amber 80–95%, green <80%.
  // Forced red regardless of memPct when the listener observed OOM.
  const isOom = runnerExitStatus === 'oom' || runnerExitStatus === 'killed'
  const barColour = isOom
    ? 'bg-red-500'
    : memPct === null
      ? 'bg-slate-500'
      : memPct >= 95
        ? 'bg-red-500'
        : memPct >= 80
          ? 'bg-amber-500'
          : 'bg-emerald-500'

  return (
    <div className="rounded-lg border border-slate-700 bg-slate-900 p-4">
      <div className="mb-3 flex items-baseline justify-between">
        <h3 className="text-sm font-semibold text-slate-200">Resource usage</h3>
        {isOom && (
          <span
            className="rounded bg-red-500/20 px-2 py-0.5 text-xs font-medium text-red-300"
            data-testid="resource-oom-indicator"
          >
            {runnerExitStatus === 'oom' ? 'OOM-killed' : 'Killed (likely OOM)'}
          </span>
        )}
      </div>

      <div data-testid="resource-usage-memory">
        <div className="mb-1 flex items-baseline justify-between text-xs text-slate-400">
          <span>Memory</span>
          <span className="font-mono">
            {peakMemoryBytes !== null ? humanBytes(peakMemoryBytes) : '—'}
            {memPct !== null && (
              <span className="ml-2 text-slate-500">{memPct.toFixed(0)}%</span>
            )}
          </span>
        </div>
        <div className="h-2 overflow-hidden rounded bg-slate-800">
          {memPct !== null && (
            <div
              className={`h-full ${barColour}`}
              style={{ width: `${memPct}%` }}
              data-testid="resource-usage-memory-bar"
            />
          )}
        </div>
        <div className="mt-1 flex justify-between font-mono text-[10px] text-slate-500">
          <span>Requested {resourceMemory}</span>
          <span>
            Limit{' '}
            {Number.isFinite(limitMem) ? humanBytes(limitMem) : `${resourceMemory} × 2`}
          </span>
        </div>
      </div>
    </div>
  )
}
