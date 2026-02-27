interface ErrorBannerProps {
  message: string
}

export function ErrorBanner({ message }: ErrorBannerProps) {
  return (
    <div className="mb-4 p-3 bg-red-900/30 text-red-400 rounded-lg text-sm border border-red-800/50">
      {message}
    </div>
  )
}
