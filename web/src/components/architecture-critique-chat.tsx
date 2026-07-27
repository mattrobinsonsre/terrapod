'use client'

/**
 * Architecture-critique follow-up chat (#1036).
 *
 * The conversational thread hanging off a workspace's current-state
 * architecture critique — modelled on the plan-summary chat (#463): one shared
 * thread per critique, prose in / prose out, grounded in the critique on screen
 * ("how would I make the data tier HA?").
 *
 * Sits inside ArchitectureCritiquePanel's "ready" state only — there's nothing
 * to chat about until the critique lands. Refetches on `refreshKey` bumps (the
 * `architecture_critique_message_posted` SSE event drives this cross-tab).
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
  type: 'architecture-critique-messages'
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
  workspaceId: string
  refreshKey: number
}

export function ArchitectureCritiqueChat({ workspaceId, refreshKey }: Props) {
  const t = useTranslations('runDetail')
  const locale = useLocale()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loaded, setLoaded] = useState(false)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [disabled, setDisabled] = useState<string | null>(null)
  const listEndRef = useRef<HTMLDivElement | null>(null)

  const base = `/api/terrapod/v1/workspaces/${workspaceId}/architecture-critique/messages`

  const load = useCallback(async () => {
    try {
      const res = await apiFetch(`${base}?locale=${encodeURIComponent(locale)}`)
      if (res.status === 404) return // parent critique missing — panel hides us
      if (!res.ok) {
        setError(t('architecture.chat.loadError'))
        return
      }
      const body = await res.json()
      setMessages(body.data ?? [])
      setLoaded(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [base, locale, t])

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
      type: 'architecture-critique-messages',
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
      const res = await apiFetch(base, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        // locale rides in the body: the server normalises the question into the
        // system language before it joins the thread, then translates the reply
        // back for display (#767).
        body: JSON.stringify({ data: { attributes: { content: text, locale } } }),
      })
      if (!res.ok) {
        let detail = t('architecture.chat.sendError')
        try {
          const body = await res.json()
          if (body?.detail) detail = body.detail
        } catch {
          /* ignore */
        }
        // 409 cap / 429 budget / 503 off → disable input until state changes.
        if ([409, 429, 503].includes(res.status)) setDisabled(detail)
        else setError(detail)
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
  }, [input, base, sending, load, t, locale])

  if (!loaded && messages.length === 0) return null

  return (
    <div className="mt-6 pt-5 border-t border-slate-700/50">
      <div className="flex items-center gap-2 mb-3">
        <MessageCircle className="w-3.5 h-3.5 text-slate-400" aria-hidden="true" />
        <h4 className="text-xs font-medium text-slate-400">{t('architecture.chat.heading')}</h4>
        <span className="text-[0.65rem] text-slate-500 italic">
          {t('architecture.chat.sharedThreadNote')}
        </span>
      </div>

      {messages.length > 0 && (
        <ul className="space-y-3 mb-4">
          {messages.map((msg) => (
            <ChatRow key={msg.id} msg={msg} />
          ))}
          <div ref={listEndRef} />
        </ul>
      )}

      {error && (
        <div className="mb-3 text-xs text-red-300 bg-red-900/20 border border-red-800/50 rounded p-2">
          {error}
        </div>
      )}

      {disabled ? (
        <div className="text-xs text-slate-500 italic bg-slate-900/40 border border-slate-700/50 rounded p-3">
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
            placeholder={t('architecture.chat.placeholder')}
            className="flex-1 text-sm bg-slate-900/60 border border-slate-700 focus:border-brand-500 focus:outline-none rounded p-2 text-slate-200 placeholder-slate-500 resize-y min-h-[3rem] max-h-40"
          />
          <button
            type="button"
            onClick={send}
            disabled={sending || !input.trim()}
            className="inline-flex items-center gap-1.5 text-xs text-slate-200 bg-brand-600 hover:bg-brand-500 disabled:bg-slate-700 disabled:text-slate-500 disabled:cursor-not-allowed px-3 py-2 rounded"
          >
            {sending ? <LoadingSpinner /> : <Send className="w-3.5 h-3.5" />}
            {sending ? t('architecture.chat.thinking') : t('architecture.chat.send')}
          </button>
        </div>
      )}
    </div>
  )
}

function ChatRow({ msg }: { msg: ChatMessage }) {
  const t = useTranslations('runDetail')
  const isUser = msg.attributes.role === 'user'
  const Icon = isUser ? User : Sparkles
  const hasError = !!msg.attributes['error-message']
  return (
    <li className="flex gap-3">
      <Icon
        className={`w-3.5 h-3.5 mt-1 flex-shrink-0 ${isUser ? 'text-slate-400' : 'text-brand-400'}`}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <div className="text-[0.65rem] text-slate-500 uppercase tracking-wide mb-0.5">
          {isUser ? t('architecture.chat.you') : t('architecture.chat.assistant')}
        </div>
        {hasError ? (
          <div className="text-xs text-red-300 font-mono whitespace-pre-wrap">
            {msg.attributes['error-message']}
          </div>
        ) : (
          <div className="text-sm text-slate-300 leading-relaxed">
            <ReactMarkdown remarkPlugins={[[remarkGfm, { singleTilde: false }]]}>
              {msg.attributes.content}
            </ReactMarkdown>
          </div>
        )}
      </div>
    </li>
  )
}
