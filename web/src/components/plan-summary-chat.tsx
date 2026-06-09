'use client'

/**
 * AI plan-summary chat thread (#463).
 *
 * Renders the conversational follow-up turns that build on top of
 * the initial PlanSummary. Modeled on GitHub Copilot's per-PR
 * conversation: one shared thread per run, visible to anyone with
 * workspace read.
 *
 * Sits inside PlanAiSummary's "ready" panel only — there's nothing
 * to chat about until the initial summary lands.
 *
 * Lifecycle:
 *   - Mount: GET /plan-summary/messages → list (may be empty).
 *   - `refreshKey` bump: refetch (SSE drives this when another
 *     browser posts).
 *   - Send: POST /plan-summary/messages with optimistic user-row;
 *     replace with assistant reply on response; rollback on error.
 *   - Disabled when chat is off, cap reached, or daily budget hit
 *     (each surface a distinct disabled message + 429/409/503).
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { MessageCircle, Send, User, Sparkles } from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { LoadingSpinner } from '@/components/loading-spinner'

interface ChatMessage {
  id: string
  type: 'plan-summary-messages'
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

export function PlanSummaryChat({ runId, refreshKey }: Props) {
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
        `/api/terrapod/v1/runs/run-${runId}/plan-summary/messages`,
      )
      if (res.status === 404) {
        // Parent summary missing — caller hides this component.
        return
      }
      if (!res.ok) {
        setError(`HTTP ${res.status}`)
        return
      }
      const body = await res.json()
      setMessages(body.data ?? [])
      setLoaded(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [runId])

  useEffect(() => {
    load()
  }, [load, refreshKey])

  // Auto-scroll to the latest turn whenever the list grows. `behavior:
  // smooth` only on a real change (not the initial load) so the page
  // doesn't auto-scroll the user out of the description if they open
  // a run with an existing thread.
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

    // Optimistic user row — replaced (re-listed) by the server reply.
    const optimistic: ChatMessage = {
      id: `optimistic-${Date.now()}`,
      type: 'plan-summary-messages',
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
      const res = await apiFetch(
        `/api/terrapod/v1/runs/run-${runId}/plan-summary/messages`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/vnd.api+json' },
          body: JSON.stringify({
            data: { attributes: { content: text } },
          }),
        },
      )

      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try {
          const body = await res.json()
          if (body?.detail) detail = body.detail
        } catch {
          /* ignore */
        }
        // 409 = cap reached; 429 = budget exhausted; 503 = chat off.
        // All three disable further input until the page reloads or
        // the situation changes — surface them as a disabled banner
        // rather than a transient error.
        if ([409, 429, 503].includes(res.status)) {
          setDisabled(detail)
        } else {
          setError(detail)
        }
        // Roll back the optimistic row so the user can retry once
        // the situation is fixed (or they can refresh).
        setMessages((prev) => prev.filter((m) => m.id !== optimistic.id))
        setInput(text)
        return
      }

      // Reload to pull both the persisted user row and the assistant
      // reply — server is the source of truth for IDs + tokens.
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setMessages((prev) => prev.filter((m) => m.id !== optimistic.id))
      setInput(text)
    } finally {
      setSending(false)
    }
  }, [input, runId, sending, load])

  if (!loaded && messages.length === 0) {
    // First load — silent until we know whether there's a thread.
    return null
  }

  return (
    <div className="mt-6 pt-5 border-t border-slate-700/50">
      <div className="flex items-center gap-2 mb-3">
        <MessageCircle className="w-3.5 h-3.5 text-slate-400" aria-hidden="true" />
        <h4 className="text-xs font-medium text-slate-400">Follow-up</h4>
        <span className="text-[0.65rem] text-slate-500 italic">
          One shared thread, visible to anyone with workspace read.
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
              // Cmd/Ctrl+Enter sends; bare Enter inserts a newline so
              // pasting log excerpts works naturally.
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault()
                send()
              }
            }}
            disabled={sending}
            rows={2}
            placeholder="Ask a follow-up question about this plan… (⌘/Ctrl + Enter to send)"
            className="flex-1 text-xs bg-slate-900/60 border border-slate-700 focus:border-brand-500 focus:outline-none rounded p-2 text-slate-200 placeholder-slate-500 resize-y min-h-[3rem] max-h-40"
          />
          <button
            type="button"
            onClick={send}
            disabled={sending || !input.trim()}
            className="inline-flex items-center gap-1.5 text-xs text-slate-200 bg-brand-600 hover:bg-brand-500 disabled:bg-slate-700 disabled:text-slate-500 disabled:cursor-not-allowed px-3 py-2 rounded"
          >
            {sending ? <LoadingSpinner /> : <Send className="w-3.5 h-3.5" />}
            {sending ? 'Thinking…' : 'Send'}
          </button>
        </div>
      )}
    </div>
  )
}

function ChatRow({ msg }: { msg: ChatMessage }) {
  const isUser = msg.attributes.role === 'user'
  const Icon = isUser ? User : Sparkles
  const hasError = !!msg.attributes['error-message']
  return (
    <li className="flex gap-3">
      <Icon
        className={`w-3.5 h-3.5 mt-1 flex-shrink-0 ${
          isUser ? 'text-slate-400' : 'text-brand-400'
        }`}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <div className="text-[0.65rem] text-slate-500 uppercase tracking-wide mb-0.5">
          {isUser ? 'You' : 'Assistant'}
        </div>
        {hasError ? (
          <div className="text-xs text-red-300 font-mono whitespace-pre-wrap">
            {msg.attributes['error-message']}
          </div>
        ) : (
          <div className="text-xs text-slate-300 leading-relaxed">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {msg.attributes.content}
            </ReactMarkdown>
          </div>
        )}
      </div>
    </li>
  )
}
