"use client";

// Per-host results for a configure run (#1928).
//
// Ansible's unit of outcome is the HOST, not the resource, so a configure's
// result is a table of hosts rather than a single status. This is the reading
// surface for the capture: the recap per host, the hosts the run never reached,
// and a drill-down to the failures.
//
// **Two things this panel must not misrepresent, both measured.**
//
// A host with `reached: false` is one the run TARGETED and ansible never
// reported on -- which happens under `serial:` with `any_errors_fatal:`,
// because only the batch that ran is reported. Its counters are null rather
// than zero, and the row says "not reached" rather than rendering dashes that
// read as nothing-to-do. Showing only what ansible reported would present a
// four-host run that aborted after one as a clean one-host success.
//
// And status must never be presented as a health check. A genuinely dead host
// reports `ok` for any task that needs no connection -- a controller-side
// `debug` never attempts one -- so reachability is a property of the first task
// that touched the host, not of the host. `saw-unreachable` is the honest
// signal, and it comes from the unreachable event rather than the counter:
// with `ignore_unreachable: true` ansible reports `unreachable=0 ignored=1`.
//
// The panel is DATA-GATED by its caller: the tab appears only when the run has
// host results, so a terraform or pulumi run -- which has no hosts -- never
// shows it and pays nothing. There is no flag (#1986).

import { useEffect, useState } from "react";
import { useConfirm } from '@/lib/use-confirm'
import { useTranslations } from "next-intl";
import { useParams, useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { MobileCard, MobileCardList } from "@/components/mobile-card-list";
import { hostStatusBadge, hostStatusKey } from "@/lib/host-status";

export interface HostResultAttrs {
  host: string;
  status: string;
  ok: number | null;
  changed: number | null;
  failures: number | null;
  unreachable: number | null;
  skipped: number | null;
  rescued: number | null;
  ignored: number | null;
  "saw-unreachable": boolean;
  reached: boolean;
}

interface HostEventAttrs {
  host: string;
  kind: string;
  "task-name": string;
  "task-action": string;
  "play-name": string;
  ignored: boolean;
  message: string;
  "payload-ref": number | null;
  sequence: number;
}

/**
 * A host's status, and the way to that host's output (#2413 F16).
 *
 * One producer because the panel renders the same badge twice -- a `<td>` at
 * desktop width and a `MobileCard` badge at phone width -- and a link added to
 * one of those is a link half the operators never see.
 *
 * A `<button>` only when there is somewhere to go, so the standalone panel
 * renders exactly the span it always did rather than a dead control. The
 * accessible name is `Configure log — <host>`: as a button its text alone
 * is the status word, which says what the host did and not what clicking does.
 */
function HostStatusBadge({
  host,
  status,
  label,
  linkLabel,
  onOpen,
}: {
  host: string;
  status: string;
  label: string;
  linkLabel: string;
  onOpen?: (host: string) => void;
}) {
  const cls = `rounded px-2 py-0.5 text-xs ${hostStatusBadge(status)}`;
  if (!onOpen) return <span className={cls}>{label}</span>;
  return (
    <button
      type="button"
      onClick={() => onOpen(host)}
      aria-label={`${linkLabel} — ${host}`}
      className={`${cls} underline decoration-dotted underline-offset-2 hover:brightness-125 focus:outline-none focus:ring-1 focus:ring-brand-500`}
    >
      {label}
    </button>
  );
}

export function HostsPanel({
  runId,
  onOpenHostLog,
}: {
  runId: string;
  /**
   * Open the configure log filtered to one host (#2413 F16).
   *
   * A callback rather than an `<a href>` because the run page owns its view in
   * `useState` and writes the URL with `replaceState`: a client-side
   * navigation to `?view=configure&host=…` would not re-run the state
   * initialiser, so the URL would change and the pane would not. Every other
   * view switch on that page is a callback for the same reason.
   *
   * Optional, so the panel still renders standalone -- and the links are
   * data-gated on it rather than on anything about the run.
   */
  onOpenHostLog?: (host: string) => void;
}) {
  const t = useTranslations("runDetail.hosts");
  const { confirmIrreversible } = useConfirm();
  // `Configure log`, which already exists in every catalogue as the view's own
  // tab label. Deliberately NOT a new string: a per-link label would be one
  // more value to keep true in 32 catalogues, and this one is both accurate
  // and already translated. The host name is composed into the accessible
  // name rather than written into a sentence, so no locale has to carry it.
  const tTabs = useTranslations("runDetail.tabs");
  const router = useRouter();
  const params = useParams();
  const [hosts, setHosts] = useState<HostResultAttrs[] | null>(null);
  const [events, setEvents] = useState<HostEventAttrs[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [error, setError] = useState("");
  // What `meta.detail.state` said. The panel must not render a complete-looking
  // summary over detail that retention has deleted (#1928), and it cannot tell
  // from the rows -- they outlive the artifact.
  const [detail, setDetail] = useState("");
  // What `meta.outstanding` said: the hosts this configure has not configured
  // (#1973). Server-side rather than filtered from the rows here, because the
  // same predicate decides what a retry covers -- two readers of the counters
  // is two answers to "did this host configure".
  const [outstanding, setOutstanding] = useState<string[]>([]);
  const [retrying, setRetrying] = useState(false);

  // The fetch lives INSIDE the effect, with a cancellation flag, rather than in
  // a `useCallback` the effect calls. Two reasons: `react-hooks/
  // set-state-in-effect` refuses the callback form here, and the flag fixes a
  // real latent bug the callback form has -- a response arriving after the tab
  // has been switched away would set state on an unmounted panel.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [resultsRes, eventsRes] = await Promise.all([
          apiFetch(`/api/v1/runs/${runId}/host-results`),
          apiFetch(`/api/v1/runs/${runId}/host-events`),
        ]);
        if (cancelled) return;
        if (!resultsRes.ok) {
          setError(t("loadFailed"));
          return;
        }
        const results = await resultsRes.json();
        if (cancelled) return;
        setDetail(results.meta?.detail?.state ?? "");
        setOutstanding(results.meta?.outstanding?.hosts ?? []);
        setHosts(
          (results.data || []).map(
            (d: { attributes: HostResultAttrs }) => d.attributes,
          ),
        );
        if (eventsRes.ok) {
          const payload = await eventsRes.json();
          if (cancelled) return;
          setEvents(
            (payload.data || []).map(
              (d: { attributes: HostEventAttrs }) => d.attributes,
            ),
          );
        }
      } catch {
        if (!cancelled) setError(t("loadFailed"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, t]);

  if (error) {
    return <p className="text-sm text-red-300">{error}</p>;
  }
  if (hosts === null) {
    return <p className="text-sm text-slate-400">{t("loading")}</p>;
  }
  if (hosts.length === 0) {
    return <p className="text-sm text-slate-400">{t("empty")}</p>;
  }

  const count = (n: number | null) => (n === null ? "—" : String(n));
  const eventsFor = (host: string) => events.filter((e) => e.host === host);

  async function retryOutstanding() {
    // Confirmed in BOTH pointer modes, not touch-only like the plain retry on
    // the run page. This queues a configure, and queueing a configure already
    // confirms unconditionally everywhere else (`queueConfigureConfirm*`):
    // running a playbook changes real hosts and there is no undo. That it is a
    // NARROWER action than the full retry does not make it reversible.
    // BOTH modes: this queues a configure against real hosts and cannot be
    // undone, which is the tier's axis rather than the word "delete" (#2413 F13).
    if (!confirmIrreversible(t("retryOutstandingConfirm", { hosts: outstanding.join(", ") })))
      return;
    setRetrying(true);
    try {
      const res = await apiFetch(`/api/v1/runs/${runId}/actions/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ "only-outstanding-hosts": true }),
      });
      if (!res.ok) {
        setError(t("retryOutstandingFailed"));
        return;
      }
      const payload = await res.json();
      const newId = payload?.data?.id;
      if (newId && params?.id) {
        router.push(`/workspaces/${params.id}/runs/${newId}`);
      }
    } catch {
      setError(t("retryOutstandingFailed"));
    } finally {
      setRetrying(false);
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-400">{t("reachabilityNote")}</p>

      {/* The remainder, and the one action that acts on it. Rendered only when
          there IS one, so a workspace that never configures -- and a configure
          that finished cleanly -- shows nothing extra: the data-gated pattern
          #1973 asks for instead of a flag. */}
      {outstanding.length > 0 && (
        <div className="rounded-lg border border-amber-900/60 bg-amber-950/30 p-3 space-y-2">
          <p className="text-sm font-medium text-amber-200">
            {t("outstandingTitle", { count: outstanding.length })}
          </p>
          <p className="text-xs text-amber-300/80">{t("outstandingNote")}</p>
          {/* Each name goes to its own output (#2413 F16). These are the
              hosts a configure did not reach, so they are the ones someone
              most wants to read -- and plain text was the one thing standing
              between the banner and the answer. Still a plain run of names
              when there is nowhere to go. */}
          <p className="break-words font-mono text-xs text-slate-300">
            {onOpenHostLog
              ? outstanding.map((host, i) => (
                  <span key={host}>
                    {i > 0 && ", "}
                    <button
                      type="button"
                      onClick={() => onOpenHostLog(host)}
                      aria-label={`${tTabs("configureFull")} — ${host}`}
                      className="underline decoration-dotted underline-offset-2 hover:text-slate-100 focus:outline-none focus:ring-1 focus:ring-brand-500 rounded"
                    >
                      {host}
                    </button>
                  </span>
                ))
              : outstanding.join(", ")}
          </p>
          <button
            type="button"
            onClick={retryOutstanding}
            disabled={retrying}
            className="rounded-lg bg-amber-900/50 px-3 py-1.5 text-xs font-medium text-amber-200 hover:bg-amber-900/70 disabled:opacity-50"
          >
            {retrying ? t("retryOutstandingBusy") : t("retryOutstanding")}
          </button>
        </div>
      )}

      {/* The per-task detail, and whether it is still there. `reaped` is the
          case the acceptance names: without this the panel looks complete over
          a stream retention has deleted. `unknown` is NOT folded into it --
          "we could not check" and "it is gone" are different claims. */}
      {detail === "reaped" && (
        <p className="rounded-lg bg-amber-900/30 px-3 py-2 text-xs text-amber-200">
          {t("detailReaped")}
        </p>
      )}
      {detail === "unknown" && (
        <p className="rounded-lg bg-slate-800 px-3 py-2 text-xs text-slate-300">
          {t("detailUnknown")}
        </p>
      )}
      {detail === "available" && (
        <p className="text-xs text-slate-400">
          {/* A plain download rather than a fetch-and-render: the stream is
              unbounded (one event per task per host, payloads inline) and
              "sluggish is acceptable; absent is not". An anchor also follows
              the 302 to storage as a navigation, which a cross-origin `fetch`
              could not do without CORS on the bucket. */}
          <a
            className="rounded-lg bg-slate-700 px-3 py-1.5 font-medium text-slate-100 hover:bg-slate-600"
            href={`/api/v1/runs/${runId}/host-events/stream`}
          >
            {t("detailDownload")}
          </a>{" "}
          <span className="ml-1">{t("detailDownloadNote")}</span>
        </p>
      )}

      {/* Desktop table. Both renders are in the DOM at every width, so an e2e
          locator needs `.filter({ visible: true })`. */}
      <div className="hidden md:block overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-slate-400">
              <th className="px-3 py-2">{t("columnHost")}</th>
              <th className="px-3 py-2">{t("columnStatus")}</th>
              <th className="px-3 py-2 text-right tabular-nums">
                {t("columnOk")}
              </th>
              <th className="px-3 py-2 text-right tabular-nums">
                {t("columnChanged")}
              </th>
              <th className="px-3 py-2 text-right tabular-nums">
                {t("columnFailures")}
              </th>
              <th className="px-3 py-2 text-right tabular-nums">
                {t("columnSkipped")}
              </th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {hosts.map((h) => {
              const rows = eventsFor(h.host);
              return (
                <tr key={h.host} className="border-t border-slate-800">
                  <td className="px-3 py-2 font-mono text-xs">{h.host}</td>
                  <td className="px-3 py-2">
                    <HostStatusBadge
                      host={h.host}
                      status={h.status}
                      label={
                        hostStatusKey(h.status)
                          ? t(hostStatusKey(h.status)!)
                          : h.status
                      }
                      linkLabel={tTabs("configureFull")}
                      onOpen={onOpenHostLog}
                    />
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {count(h.ok)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {count(h.changed)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {count(h.failures)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {count(h.skipped)}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {rows.length > 0 && (
                      <button
                        type="button"
                        onClick={() =>
                          setExpanded(expanded === h.host ? null : h.host)
                        }
                        className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600"
                      >
                        {t("showFailures", { count: rows.length })}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Phone width. `min-w-0` so a long host name cannot push the page wide. */}
      <MobileCardList>
        {hosts.map((h) => (
          <MobileCard
            key={h.host}
            title={
              <span className="font-mono text-xs min-w-0 break-all">
                {h.host}
              </span>
            }
            badge={
              <HostStatusBadge
                host={h.host}
                status={h.status}
                label={
                  hostStatusKey(h.status) ? t(hostStatusKey(h.status)!) : h.status
                }
                linkLabel={tTabs("configureFull")}
                onOpen={onOpenHostLog}
              />
            }
            fields={[
              { label: t("columnOk"), value: count(h.ok) },
              { label: t("columnChanged"), value: count(h.changed) },
              { label: t("columnFailures"), value: count(h.failures) },
              { label: t("columnSkipped"), value: count(h.skipped) },
            ]}
            actions={
              eventsFor(h.host).length > 0 ? (
                <button
                  type="button"
                  onClick={() =>
                    setExpanded(expanded === h.host ? null : h.host)
                  }
                  className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700 hover:bg-slate-600"
                >
                  {t("showFailures", { count: eventsFor(h.host).length })}
                </button>
              ) : undefined
            }
          />
        ))}
      </MobileCardList>

      {expanded && (
        <div className="rounded-lg border border-slate-800 p-3 space-y-2">
          <h4 className="text-sm font-medium">
            {t("failuresFor", { host: expanded })}
          </h4>
          {/* The drill-down #1928 asks for: this host's OWN object, not a
              filter of the run's, so it is one fetch rather than a scan of a
              stream that is tens of MB at fleet scale. Shown only when the
              detail still exists; `reaped` and `unknown` are announced at the
              top of the panel and a link here would offer bytes that are gone.

              The existing `detailDownload` label is reused deliberately rather
              than adding a per-host string: it reads correctly under a heading
              that already names the host, and a near-duplicate key would be 31
              more values to keep true for no gain a reader can see. */}
          <div className="flex flex-wrap gap-2">
            {detail === "available" && (
              <a
                className="inline-block rounded-lg bg-slate-700 px-3 py-1.5 text-xs font-medium text-slate-100 hover:bg-slate-600"
                href={`/api/v1/runs/${runId}/host-events/stream?host=${encodeURIComponent(expanded)}`}
              >
                {t("detailDownload")}
              </a>
            )}
            {/* #2413 F16. The expansion stays in place -- it answers WHICH
                tasks failed, which is a different question from what the host
                printed -- and this is the way out of it to the output. Not
                gated on `detail`: the log is a separate artifact from the
                per-host events, so it can be there when they have been
                reaped. */}
            {onOpenHostLog && (
              <button
                type="button"
                onClick={() => onOpenHostLog(expanded)}
                className="inline-block rounded-lg bg-slate-700 px-3 py-1.5 text-xs font-medium text-slate-100 hover:bg-slate-600"
              >
                {tTabs("configureFull")}
              </button>
            )}
          </div>
          {eventsFor(expanded).map((e) => (
            <div key={`${e.sequence}-${e.kind}`} className="text-xs space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className={`rounded px-2 py-0.5 ${hostStatusBadge(e.kind)}`}
                >
                  {hostStatusKey(e.kind) ? t(hostStatusKey(e.kind)!) : e.kind}
                </span>
                <span className="font-mono break-all">{e["task-name"]}</span>
                {e.ignored && (
                  <span className="text-amber-300">{t("wasIgnored")}</span>
                )}
              </div>
              <p className="whitespace-pre-wrap break-words text-slate-300">
                {e.message}
              </p>
              {e["payload-ref"] !== null && (
                <p className="text-slate-500">{t("payloadSpilled")}</p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
