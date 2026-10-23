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
import { useTranslations } from "next-intl";
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

export function HostsPanel({ runId }: { runId: string }) {
  const t = useTranslations("runDetail.hosts");
  const [hosts, setHosts] = useState<HostResultAttrs[] | null>(null);
  const [events, setEvents] = useState<HostEventAttrs[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [error, setError] = useState("");
  // What `meta.detail.state` said. The panel must not render a complete-looking
  // summary over detail that retention has deleted (#1928), and it cannot tell
  // from the rows -- they outlive the artifact.
  const [detail, setDetail] = useState("");

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

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-400">{t("reachabilityNote")}</p>

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
                    <span
                      className={`rounded px-2 py-0.5 text-xs ${hostStatusBadge(h.status)}`}
                    >
                      {hostStatusKey(h.status)
                        ? t(hostStatusKey(h.status)!)
                        : h.status}
                    </span>
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
              <span
                className={`rounded px-2 py-0.5 text-xs ${hostStatusBadge(h.status)}`}
              >
                {hostStatusKey(h.status)
                  ? t(hostStatusKey(h.status)!)
                  : h.status}
              </span>
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
          {detail === "available" && (
            <a
              className="inline-block rounded-lg bg-slate-700 px-3 py-1.5 text-xs font-medium text-slate-100 hover:bg-slate-600"
              href={`/api/v1/runs/${runId}/host-events/stream?host=${encodeURIComponent(expanded)}`}
            >
              {t("detailDownload")}
            </a>
          )}
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
