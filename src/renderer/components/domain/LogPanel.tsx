import { useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from 'react';
import { ChevronDown, ChevronRight, Copy, Trash2 } from 'lucide-react';
import { useRunStore, type LogLine } from '@renderer/store/runStore';
import { useToastStore } from '@renderer/store/toastStore';
import { Button } from '@renderer/components/ui/Button';
import { cn } from '@renderer/lib/cn';
import { copyText } from '@renderer/lib/copyText';

const colorByLevel = {
  /** 注册机常规 stdout（[*] 状态行等）保持蓝色 */
  info: 'text-info',
  warn: 'text-warn',
  error: 'text-danger',
  tip: 'text-tip',
  plain: 'text-foreground',
  stderr: 'text-danger'
} as const;

/** 汇总行：成功绿 / 失败红 / 共计蓝 */
const SUMMARY_RE =
  /^(.*?)(成功\s*[:：]\s*\d+)(.*?)(失败\s*[:：]\s*\d+)(.*?)(共计\s*[:：]\s*\d+)(.*)$/u;

/** [plan] 本轮启用Plan: A:on B:off C:on → 显示「本轮启用Plan: A B C」启绿禁红 */
const PLAN_LINE_RE =
  /^\[plan\]\s*本轮启用Plan:\s*A:(on|off)\s+B:(on|off)\s+C:(on|off)\s*$/u;

/**
 * 授权流水线日志（SSO→G2A / mint / CPA / 队列背压等）。
 * 与注册主循环日志拆开，避免混在「注册日志」里。
 */
export function isAuthPipelineLogText(text: string): boolean {
  const t = String(text || '').trim();
  if (!t) return false;
  // 常见前缀（含 worker 名）
  if (/^\[auth-queue\]/i.test(t)) return true;
  if (/^\[mint-queue\]/i.test(t)) return true;
  if (/^\[auth\]/i.test(t)) return true;
  if (/^\[browser-mint\]/i.test(t)) return true;
  if (/^\[device-mint\]/i.test(t)) return true;
  if (/^\[oauth\]/i.test(t)) return true;
  // 入队提示（注册成功后交权）
  if (/授权已入队后台/i.test(t)) return true;
  if (/等待后台转换队列/i.test(t)) return true;
  if (/SSO\s*[→\-]\s*grok2api/i.test(t)) return true;
  if (/Auth\s*mint/i.test(t)) return true;
  if (/Auth\s*[→\-]\s*CPA/i.test(t)) return true;
  if (/mint\s*池/i.test(t)) return true;
  if (/流水线部分失败|流水线完成/i.test(t)) return true;
  if (/skip_bot_flag_on_mint/i.test(t)) return true;
  if (/BOT_FLAG_SOURCE/i.test(t)) return true;
  return false;
}

function planLetterClass(state: string) {
  return state === 'on'
    ? 'text-emerald-600 dark:text-emerald-400 font-semibold'
    : 'text-danger font-semibold';
}

function renderPlanLine(text: string, levelClass: string) {
  const m = String(text || '').match(PLAN_LINE_RE);
  if (!m) return null;
  const [, aS, bS, cS] = m;
  return (
    <span className={cn('break-all text-[12px] font-medium', levelClass)}>
      本轮启用Plan:{' '}
      <span className={planLetterClass(aS)}>A</span>{' '}
      <span className={planLetterClass(bS)}>B</span>{' '}
      <span className={planLetterClass(cS)}>C</span>
    </span>
  );
}

function renderLogText(text: string, levelClass: string) {
  const plan = renderPlanLine(text, levelClass);
  if (plan) return plan;

  const m = String(text || '').match(SUMMARY_RE);
  if (!m) {
    return <span className={cn('break-all text-[12px]', levelClass)}>{text}</span>;
  }
  const [, pre, okPart, mid1, failPart, mid2, totalPart, post] = m;
  return (
    <span className="break-all text-[12px] font-medium">
      {pre ? <span className={levelClass}>{pre}</span> : null}
      <span className="text-emerald-600 dark:text-emerald-400">{okPart}</span>
      {mid1 ? <span className="text-muted-foreground">{mid1}</span> : null}
      <span className="text-danger">{failPart}</span>
      {mid2 ? <span className="text-muted-foreground">{mid2}</span> : null}
      <span className="text-info">{totalPart}</span>
      {post ? <span className={levelClass}>{post}</span> : null}
    </span>
  );
}

type LogChannel = 'register' | 'auth';

function filterLogsByChannel(logs: LogLine[], channel: LogChannel): LogLine[] {
  if (channel === 'auth') {
    return logs.filter((l) => isAuthPipelineLogText(l.text));
  }
  return logs.filter((l) => !isAuthPipelineLogText(l.text));
}

function AuthQueueMetricsInline() {
  const [m, setM] = useState<{
    pending?: number;
    queue_size?: number;
    done_ok?: number;
    done_fail?: number;
    workers?: number;
    queue_max?: number;
    updated_iso?: string;
    stale?: boolean;
    fail_by_status?: Record<string, number>;
    fail_status_ratio_pct?: Record<string, number>;
    mint_retry_pending?: number;
    mint_max_attempts?: number;
    mint_budget_exhausted_total?: number;
    // 独立 mint 池：授权队列 done_ok 只是「转交完成」，真实出号数在 mint 池
    separate_pool?: boolean;
    mint_pending?: number;
    mint_done_ok?: number;
    mint_done_fail?: number;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const api = window.api as {
          getAuthQueueMetrics?: () => Promise<Record<string, unknown>>;
        };
        if (!api.getAuthQueueMetrics) return;
        const r = await api.getAuthQueueMetrics();
        if (!cancelled && r) setM(r as typeof m);
      } catch {
        /* ignore */
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 4000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const pending = m?.pending ?? m?.queue_size ?? 0;
  const workers = m?.workers ?? 0;
  // 独立 mint 池模式：授权队列 done_ok 只是「转交完成」，不代表真出号。
  // 真实成败在 mint 池的独立计数器（mint_done_ok/mint_done_fail）。
  const separatePool = m?.separate_pool === true;
  const handedOk = m?.done_ok ?? 0;
  const ok = separatePool ? (m?.mint_done_ok ?? 0) : handedOk;
  const fail = separatePool ? (m?.mint_done_fail ?? 0) : (m?.done_fail ?? 0);
  // 在途：已转交独立池但 mint 尚未跑完 = 转交数 − 真实成败之和（差值天然含池内 pending+running），钳到 ≥0
  const inflight = separatePool
    ? Math.max(0, handedOk - (m?.mint_done_ok ?? 0) - (m?.mint_done_fail ?? 0))
    : 0;
  const qmax = m?.queue_max ?? 0;
  const failBy = m?.fail_by_status || {};
  const ratio = m?.fail_status_ratio_pct || {};
  const focusKeys = [
    'mint_queue_full',
    'mint_denied_castle',
    'mint_oauth_fail'
  ] as const;
  const focusChips = focusKeys
    .map((k) => {
      const n = Number(failBy[k] || 0);
      const pct = Number(ratio[k] || 0);
      return { k, n, pct };
    })
    .filter((x) => x.n > 0 || x.pct > 0);
  const topFail = Object.entries(failBy)
    .map(([k, v]) => [k, Number(v) || 0] as const)
    .filter(([k, n]) => n > 0 && !focusKeys.includes(k as (typeof focusKeys)[number]))
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3);
  const retryPending = Number(m?.mint_retry_pending || 0);
  const budgetEx = Number(m?.mint_budget_exhausted_total || 0);
  const maxAtt = m?.mint_max_attempts;

  return (
    <div className="border-b border-border/60 px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="text-[12px] font-semibold tracking-[-0.02em]">授权队列</div>
        <span className="text-[10px] text-muted-foreground">
          {m?.stale
            ? '暂无运行中数据'
            : m?.updated_iso
              ? `更新 ${String(m.updated_iso).slice(11, 19)}`
              : '—'}
        </span>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div className="rounded-lg border border-border/50 bg-muted/50 px-2.5 py-2">
          <div className="text-[10px] text-muted-foreground">排队</div>
          <div className="text-[15px] font-semibold tabular-nums">{pending}</div>
        </div>
        <div className="rounded-lg border border-border/50 bg-muted/50 px-2.5 py-2">
          <div className="text-[10px] text-muted-foreground">Workers</div>
          <div className="text-[15px] font-semibold tabular-nums">
            {workers}
            {qmax ? (
              <span className="text-[11px] font-normal text-muted-foreground">
                {' '}
                / max{qmax}
              </span>
            ) : null}
          </div>
        </div>
        <div
          className="rounded-lg border border-border/50 bg-muted/50 px-2.5 py-2"
          title={
            separatePool
              ? '独立 mint 池模式：此处为真实出号数（mint 生成 auth 文件），非转交数'
              : undefined
          }
        >
          <div className="text-[10px] text-muted-foreground">
            {separatePool ? '成功·出号' : '成功'}
          </div>
          <div className="text-[15px] font-semibold tabular-nums text-emerald-600">
            {ok}
            {inflight > 0 ? (
              <span className="text-[11px] font-normal text-muted-foreground">
                {' '}
                +{inflight} 在途
              </span>
            ) : null}
          </div>
        </div>
        <div className="rounded-lg border border-border/50 bg-muted/50 px-2.5 py-2">
          <div className="text-[10px] text-muted-foreground">失败</div>
          <div className="text-[15px] font-semibold tabular-nums text-amber-600">{fail}</div>
        </div>
      </div>
      {(retryPending > 0 || budgetEx > 0 || maxAtt != null) && (
        <div className="mt-2 flex flex-wrap gap-1.5 text-[10px] text-muted-foreground">
          {maxAtt != null ? (
            <span className="rounded-full border border-border/60 bg-muted/40 px-2 py-0.5">
              mint预算:{String(maxAtt)}
            </span>
          ) : null}
          {retryPending > 0 ? (
            <span className="rounded-full border border-border/60 bg-muted/40 px-2 py-0.5">
              待重试:{retryPending}
            </span>
          ) : null}
          {budgetEx > 0 ? (
            <span className="rounded-full border border-border/60 bg-muted/40 px-2 py-0.5">
              预算用尽:{budgetEx}
            </span>
          ) : null}
        </div>
      )}
      {focusChips.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {focusChips.map(({ k, n, pct }) => (
            <span
              key={k}
              className="rounded-full border border-border/60 bg-muted/40 px-2 py-0.5 text-[10px] text-muted-foreground"
              title={`${k} count=${n} ratio=${pct}%`}
            >
              {k}:{n}
              {pct > 0 ? `(${pct}%)` : ''}
            </span>
          ))}
        </div>
      ) : null}
      {topFail.length > 0 ? (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {topFail.map(([k, n]) => (
            <span
              key={k}
              className="rounded-full border border-border/50 bg-muted/30 px-2 py-0.5 text-[10px] text-muted-foreground/90"
              title={k}
            >
              {k}:{n}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ChannelLogPanel({
  channel,
  title,
  emptyHint,
  headerExtra
}: {
  channel: LogChannel;
  title: string;
  emptyHint: string;
  headerExtra?: ReactNode;
}) {
  const logs = useRunStore((s) => s.logs);
  const focusRunId = useRunStore((s) => s.focusRunId);
  const clearLogs = useRunStore((s) => s.clearLogs);
  const clearLogsFor = useRunStore((s) => s.clearLogsFor);
  const clearLogsChannel = useRunStore((s) => s.clearLogsChannel);
  const clearLogsChannelFor = useRunStore((s) => s.clearLogsChannelFor);
  const pushToast = useToastStore((s) => s.push);
  const ref = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  /** all = 全部任务混显；focus = 仅当前聚焦 */
  const [scope, setScope] = useState<'focus' | 'all'>('focus');
  /** 默认折叠：点标题栏展开 */
  const [open, setOpen] = useState(false);

  const scoped = useMemo(() => {
    if (scope === 'all' || !focusRunId) return logs;
    return logs.filter((l) => l.runId === focusRunId);
  }, [logs, focusRunId, scope]);

  const visible = useMemo(
    () => filterLogsByChannel(scoped, channel),
    [scoped, channel]
  );

  useEffect(() => {
    const el = ref.current;
    if (!el || !autoScroll || !open) return;
    el.scrollTop = el.scrollHeight;
  }, [visible, autoScroll, open]);

  const onScroll = () => {
    const el = ref.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    setAutoScroll(atBottom);
  };

  const copyAll = async (e?: MouseEvent) => {
    e?.preventDefault?.();
    e?.stopPropagation?.();
    const text = visible
      .map((l) => {
        const t = new Date(l.ts).toLocaleTimeString('zh-CN', {
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: false
        });
        const prefix = scope === 'all' ? `#${l.runId.slice(0, 6)} ` : '';
        return `${t} ${prefix}${l.text}`;
      })
      .join('\n');
    if (!text.trim()) {
      pushToast({ tone: 'warn', title: '没有可复制的日志' });
      return;
    }
    try {
      await copyText(text);
      pushToast({
        tone: 'ok',
        title: `已复制 ${visible.length} 行${title}`
      });
    } catch (err) {
      pushToast({
        tone: 'danger',
        title: '复制失败',
        description: String(err || 'clipboard unavailable')
      });
    }
  };

  const doClear = () => {
    if (clearLogsChannelFor && clearLogsChannel) {
      if (scope === 'focus' && focusRunId) clearLogsChannelFor(focusRunId, channel);
      else clearLogsChannel(channel);
      return;
    }
    // 兼容旧 store（不应走到）
    if (scope === 'focus' && focusRunId) clearLogsFor(focusRunId);
    else clearLogs();
  };

  return (
    <div
      className={cn(
        'ios-group flex flex-col overflow-hidden',
        open ? 'h-[min(520px,60vh)]' : 'h-auto'
      )}
    >
      <div
        className={cn(
          'flex items-center justify-between px-4 py-3.5',
          open && 'border-b border-border/70'
        )}
      >
        <button
          type="button"
          className="flex min-w-0 flex-1 items-start gap-2 text-left"
          onClick={() => setOpen((v) => !v)}
          title={open ? `折叠${title}` : `展开${title}`}
        >
          {open ? (
            <ChevronDown className="mt-1 h-4 w-4 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight className="mt-1 h-4 w-4 shrink-0 text-muted-foreground" />
          )}
          <div className="min-w-0">
            <h2 className="text-[20px] font-bold tracking-[-0.02em]">{title}</h2>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              {open
                ? scope === 'focus' && focusRunId
                  ? `仅 #${focusRunId.slice(0, 8)} · ${visible.length} 行`
                  : `全部任务 · ${visible.length} 行`
                : `${visible.length} 行 · 点击展开`}
            </p>
          </div>
        </button>
        {open ? (
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex rounded-full border border-border bg-muted/50 p-0.5 text-[11px]">
              <button
                type="button"
                className={cn(
                  'rounded-full px-2.5 py-1 font-medium transition-colors',
                  scope === 'focus'
                    ? 'bg-card text-foreground shadow-sm'
                    : 'text-muted-foreground'
                )}
                onClick={() => setScope('focus')}
              >
                聚焦
              </button>
              <button
                type="button"
                className={cn(
                  'rounded-full px-2.5 py-1 font-medium transition-colors',
                  scope === 'all'
                    ? 'bg-card text-foreground shadow-sm'
                    : 'text-muted-foreground'
                )}
                onClick={() => setScope('all')}
              >
                全部
              </button>
            </div>
            <span className={cn('pill', autoScroll ? 'pill-ok' : 'pill-warn')}>
              {autoScroll ? '自动滚动' : '已暂停'}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={(ev) => void copyAll(ev)}
              title={`复制当前可见${title}`}
            >
              <Copy className="h-3.5 w-3.5" />
              复制
            </Button>
            <Button variant="ghost" size="sm" onClick={doClear}>
              <Trash2 className="h-3.5 w-3.5" />
              清空
            </Button>
          </div>
        ) : null}
      </div>
      {/* 授权队列 metrics 等：折叠时也保留，避免主页丢指标 */}
      {headerExtra}
      {open ? (
        <div
          ref={ref}
          onScroll={onScroll}
          className="log-surface m-3 flex-1 overflow-y-auto px-3 py-2.5 leading-6"
        >
          {visible.length === 0 ? (
            <div className="mt-16 text-center font-sans text-[13px] text-muted-foreground">
              {emptyHint}
            </div>
          ) : (
            visible.map((l) => (
              <div
                key={l.id}
                className="grid grid-cols-[72px_minmax(0,1fr)] gap-3 border-b border-border/40 py-1.5 last:border-b-0"
              >
                <span className="text-[11px] text-muted-foreground">
                  {new Date(l.ts).toLocaleTimeString('zh-CN', {
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit',
                    hour12: false
                  })}
                </span>
                <div className="min-w-0">
                  {scope === 'all' && (
                    <span className="mr-2 font-mono text-[10px] text-muted-foreground">
                      #{l.runId.slice(0, 6)}
                    </span>
                  )}
                  {renderLogText(l.text, colorByLevel[l.level] || 'text-foreground')}
                </div>
              </div>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

/** 注册主循环日志（排除授权/mint 流水线） */
export function RegisterLogPanel() {
  return (
    <ChannelLogPanel
      channel="register"
      title="注册日志"
      emptyHint="尚无注册日志。开始注册后将实时显示输出。"
    />
  );
}

/** 授权流水线日志 + 授权队列 metrics */
export function AuthLogPanel() {
  return (
    <ChannelLogPanel
      channel="auth"
      title="授权日志"
      emptyHint="尚无授权日志。注册成功入队后显示 SSO 推送 / mint / 背压等。"
      headerExtra={<AuthQueueMetricsInline />}
    />
  );
}

/** 兼容旧引用：默认展示注册日志 */
export function LogPanel() {
  return <RegisterLogPanel />;
}
