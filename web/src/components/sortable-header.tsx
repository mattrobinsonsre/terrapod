import { ArrowUp, ArrowDown, ChevronsUpDown } from 'lucide-react'
import type { SortState } from '@/lib/use-sortable'

interface SortableHeaderProps<K extends string> {
  label: string
  sortKey: K
  sortState: SortState<K>
  onSort: (key: K) => void
  className?: string
  align?: 'left' | 'right'
}

export function SortableHeader<K extends string>({
  label,
  sortKey,
  sortState,
  onSort,
  className = '',
  align = 'left',
}: SortableHeaderProps<K>) {
  const isActive = sortState.key === sortKey
  const Icon = isActive
    ? sortState.direction === 'asc'
      ? ArrowUp
      : ArrowDown
    : ChevronsUpDown

  return (
    <th
      onClick={() => onSort(sortKey)}
      className={`px-4 py-3 text-${align} text-xs font-medium uppercase tracking-wider cursor-pointer hover:text-slate-200 select-none ${
        isActive ? 'text-slate-300' : 'text-slate-400'
      } ${className}`}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        <Icon className={`w-3 h-3 ${isActive ? 'text-brand-400' : 'text-slate-600'}`} />
      </span>
    </th>
  )
}
