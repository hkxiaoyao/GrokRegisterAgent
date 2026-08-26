import { cn } from '@renderer/lib/cn';

/**
 * BFS tag：access/id/sso JWT payload 里存在 `bfs` key 即已标记。
 * 仅 flagged 显示红徽章（带原值 tooltip）；clean / unknown 不显示。
 */
export function BfsBadge({
  status,
  value,
  source,
  className
}: {
  status?: 'flagged' | 'clean' | 'unknown' | null;
  /** claim 原值（常见 2），tooltip 展示 */
  value?: number | string | null;
  /** 判定依据 token：access_token / id_token / sso */
  source?: string | null;
  className?: string;
}) {
  if (status !== 'flagged') return null;

  const tip =
    `BFS 已标记（payload 含 bfs key${source ? ` · ${source}` : ''}）` +
    (value != null && value !== '' ? ` · 值 ${value}` : '') +
    '；与 bot_flag_source 是独立信号';

  return (
    <span
      title={tip}
      className={cn(
        'inline-flex h-5 shrink-0 items-center rounded-full bg-red-500/15 px-2 text-[10px] font-medium leading-none text-red-600 dark:text-red-400',
        className
      )}
    >
      BFS
    </span>
  );
}
