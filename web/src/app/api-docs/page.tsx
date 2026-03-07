'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import NavBar from '@/components/nav-bar'
import { getAuthState } from '@/lib/auth'

type DocView = 'redoc' | 'swagger'

export default function ApiDocsPage() {
  const router = useRouter()
  const [view, setView] = useState<DocView>('redoc')

  useEffect(() => {
    if (!getAuthState()) {
      router.push('/login')
    }
  }, [router])

  const src = view === 'swagger' ? '/api/docs' : '/api/redoc'

  return (
    <>
      <NavBar />
      <div className="flex flex-col" style={{ height: 'calc(100dvh - 57px)' }}>
        <div className="flex items-center gap-2 px-4 py-2 border-b border-slate-800 bg-slate-900/60">
          <span className="text-sm font-medium text-slate-400 mr-2">View:</span>
          <button
            onClick={() => setView('redoc')}
            className={`px-3 py-1 rounded text-sm font-medium transition-colors ${
              view === 'redoc'
                ? 'bg-brand-600/20 text-brand-400'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
            }`}
          >
            ReDoc
          </button>
          <button
            onClick={() => setView('swagger')}
            className={`px-3 py-1 rounded text-sm font-medium transition-colors ${
              view === 'swagger'
                ? 'bg-brand-600/20 text-brand-400'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
            }`}
          >
            Swagger UI
          </button>
          <a
            href="/api/openapi.json"
            target="_blank"
            rel="noopener noreferrer"
            className="ml-auto px-3 py-1 rounded text-sm font-medium text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors"
          >
            OpenAPI JSON
          </a>
        </div>
        <iframe
          key={src}
          src={src}
          className="flex-1 w-full border-0"
          title={view === 'swagger' ? 'Swagger UI' : 'ReDoc API Documentation'}
        />
      </div>
    </>
  )
}
