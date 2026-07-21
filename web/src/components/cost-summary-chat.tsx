'use client'

/**
 * AI cost-estimate chat thread (#871).
 *
 * The cost analogue of PlanSummaryChat: conversational follow-ups grounded in
 * the run's cost estimate, one shared thread per run (anyone with workspace
 * read). Sits inside CostAiSummary's "ready" panel. Reuses the generic
 * `planSummary.chat.*` catalog strings (heading/placeholder/send/you/assistant)
 * — no new i18n keys.
 *
 * Lifecycle mirrors the plan chat: mount → GET messages; `refreshKey` bump →
 * refetch (SSE `cost_summary_message_posted` drives it); send → optimistic user
 * row, reload on reply, rollback on error (409 cap / 429 budget / 503 off →
 * disabled banner).
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslations, useLocale } from 'next-intl'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { MessageCircle, Send, User, Sparkles } from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { LoadingSpinner } from '@/components/loading-spinner'

interface ChatMessage {
  id: string
  type: 'cost-summary-messages'
  attributes: {
    role: 'user' | 'assistant'
    content: string
    model: string
    'input-tokens': number
    'output-tokens': number
    'error-message': string
    'created-at': string
  }
}

interface Props {
  runId: string
  refreshKey: number
}

export function CostSummaryChat({ runId, refreshKey }: Props) {
  // Generic chat labels (heading/send/you/assistant) are reused from the plan
  // summary; only the placeholder is cost-specific (`runDetail.costAi`).
  const t = useTranslations('planSummary')
  const tc = useTranslations('runDetail')
  const locale = useLocale()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loaded, setLoaded] = useState(false)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [disabled, setDisabled] = useState<string | null>(null)
  const listEndRef = useRef<HTMLDivElement | null>(null)

  const load = useCallback(async () => {
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/runs/run-${runId}/cost-summary/messages?locale=${encodeURIComponent(locale)}`,
      )
      if (res.status === 404) return // parent summary missing — caller hides this
      if (!res.ok) {
        setError(t('errors.status', { status: res.status }))
        return
      }
      const body = await res.json()
      setMessages(body.data ?? [])
      setLoaded(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [runId, t, locale])

  useEffect(() => {
    load()
  }, [load, refreshKey])

  const prevLen = useRef(0)
  useEffect(() => {
    if (messages.length > prevLen.current && prevLen.current > 0) {
      listEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
    prevLen.current = messages.length
  }, [messages.length])

  const send = useCallback(async () => {
    const text = input.trim()
    if (!text || sending) return
    setSending(true)
    setError(null)

    const optimistic: ChatMessage = {
      id: `optimistic-${Date.now()}`,
      type: 'cost-summary-messages',
      attributes: {
        role: 'user',
        content: text,
        model: '',
        'input-tokens': 0,
        'output-tokens': 0,
        'error-message': '',
        'created-at': new Date().toISOString(),
      },
    }
    setMessages((prev) => [...prev, optimistic])
    setInput('')

    try {
      const res = await apiFetch(`/api/terrapod/v1/runs/run-${runId}/cost-summary/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { attributes: { content: text, locale } } }),
      })

      if (!res.ok) {
        let detail = t('errors.status', { status: res.status })
        try {
          const body = await res.json()
          if (body?.detail) detail = body.detail
        } catch {
          /* ignore */
        }
        if ([409, 429, 503].includes(res.status)) {
          setDisabled(detail)
        } else {
          setError(detail)
        }
        setMessages((prev) => prev.filter((m) => m.id !== optimistic.id))
        setInput(text)
        return
      }

      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setMessages((prev) => prev.filter((m) => m.id !== optimistic.id))
      setInput(text)
    } finally {
      setSending(false)
    }
  }, [input, runId, sending, load, t, locale])

  if (!loaded && messages.length === 0) return null

  return (
    <div className="mt-5 border-t border-slate-700/50 pt-5">
      <div className="mb-3 flex items-center gap-2">
        <MessageCircle className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
        <h4 className="text-xs font-medium text-slate-400">{t('chat.heading')}</h4>
        <span className="text-[0.65rem] italic text-slate-500">{t('chat.sharedThreadNote')}</span>
      </div>

      {messages.length > 0 && (
        <ul className="mb-4 space-y-3">
          {messages.map((msg) => (
            <ChatRow key={msg.id} msg={msg} />
          ))}
          <div ref={listEndRef} />
        </ul>
      )}

      {error && (
        <div className="mb-3 rounded border border-red-800/50 bg-red-900/20 p-2 text-xs text-red-300">
          {error}
        </div>
      )}

      {disabled ? (
        <div className="rounded border border-slate-700/50 bg-slate-900/40 p-3 text-xs italic text-slate-500">
          {disabled}
        </div>
      ) : (
        <div className="flex items-end gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault()
                send()
              }
            }}
            disabled={sending}
            rows={2}
            placeholder={tc('costAi.chatPlaceholder')}
            className="max-h-40 min-h-[3rem] flex-1 resize-y rounded border border-slate-700 bg-slate-900/60 p-2 text-sm text-slate-200 placeholder-slate-500 focus:border-brand-500 focus:outline-none"
          />
          <button
            type="button"
            onClick={send}
            disabled={sending || !input.trim()}
            className="inline-flex items-center gap-1.5 rounded bg-brand-600 px-3 py-2 text-xs text-slate-200 hover:bg-brand-500 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-500"
          >
            {sending ? <LoadingSpinner /> : <Send className="h-3.5 w-3.5" />}
            {sending ? t('chat.thinking') : t('chat.send')}
          </button>
        </div>
      )}
    </div>
  )
}

function ChatRow({ msg }: { msg: ChatMessage }) {
  const t = useTranslations('planSummary')
  const isUser = msg.attributes.role === 'user'
  const Icon = isUser ? User : Sparkles
  const hasError = !!msg.attributes['error-message']
  return (
    <li className="flex gap-3">
      <Icon
        className={`mt-1 h-3.5 w-3.5 flex-shrink-0 ${isUser ? 'text-slate-400' : 'text-brand-400'}`}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <div className="mb-0.5 text-[0.65rem] uppercase tracking-wide text-slate-500">
          {isUser ? t('chat.you') : t('chat.assistant')}
        </div>
        {hasError ? (
          <div className="whitespace-pre-wrap font-mono text-xs text-red-300">
            {msg.attributes['error-message']}
          </div>
        ) : (
          <div className="text-sm leading-relaxed text-slate-300">
            {/* singleTilde:false — cost prose is full of "~$X" (approximately);
                without this, GFM pairs the tildes into strikethrough spans. */}
            <ReactMarkdown remarkPlugins={[[remarkGfm, { singleTilde: false }]]}>
              {msg.attributes.content}
            </ReactMarkdown>
          </div>
        )}
      </div>
    </li>
  )
}
