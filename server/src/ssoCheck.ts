/**
 * SSO 验活：用 sso token 作 cookie 请求 grok.com 的用户信息接口。
 * 200 = 存活并返回账户信息；401/403 = 失效。请求走 settings.proxy。
 * 额外解码 JWT 中的 bot_flag_source（只读）。
 */
import { proxiedRequest } from './httpClient.js';
import { readBotFlagFromToken } from './jwtBotFlag.js';

const GET_USER_URL = 'https://grok.com/rest/auth/get-user';
const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

export interface SsoCheckOutcome {
  alive: boolean;
  status: number;
  email?: string;
  givenName?: string;
  familyName?: string;
  emailConfirmed?: boolean;
  sessionTierId?: string;
  createTime?: string;
  error?: string;
  /** JWT claim bot_flag_source（可能为 null） */
  botFlagSource?: number | string | null;
  /** bot_flag_source === 1 */
  isBotFlag1?: boolean;
  /** get-user riskLevel 枚举，如 USER_RISK_LEVEL_HIGH */
  riskLevel?: string | null;
  /**
   * Castle 连续风险分 0.00~1.00（从 botFlagDetails 的 risk= 解析）。
   * 无则 null（UI 显示 None 风格）。
   */
  riskScore?: number | null;
  /** get-user botFlagDetails 原文（截断） */
  botFlagDetails?: string | null;
  /** 从 details 解析的 event，如 $registration / $login */
  riskEvent?: string | null;
}

/** 从 botFlagDetails 抽 risk=0.xx / event=$... */
export function parseCastleRiskDetails(details: unknown): {
  riskScore: number | null;
  riskEvent: string | null;
  botFlagDetails: string | null;
} {
  const raw = typeof details === 'string' ? details.trim() : '';
  if (!raw) {
    return { riskScore: null, riskEvent: null, botFlagDetails: null };
  }
  const clipped = raw.slice(0, 400);
  let riskScore: number | null = null;
  const rm = clipped.match(/\brisk\s*[=:]\s*([01](?:\.\d+)?|\.\d+)\b/i);
  if (rm) {
    const n = Number(rm[1]);
    if (Number.isFinite(n) && n >= 0 && n <= 1) {
      // 展示两位小数语义，内部仍保留数值
      riskScore = Math.round(n * 100) / 100;
    }
  }
  let riskEvent: string | null = null;
  const em = clipped.match(/\bevent\s*[=:]\s*(\$?[A-Za-z_][\w.]*)\b/i);
  if (em) {
    riskEvent = em[1].startsWith('$') ? em[1] : `$${em[1]}`;
  }
  return { riskScore, riskEvent, botFlagDetails: clipped };
}

export async function checkSso(sso: string, proxy?: string): Promise<SsoCheckOutcome> {
  const token = (sso || '').replace(/^sso=/, '').trim();
  if (!token) return { alive: false, status: 0, error: '缺少 sso token' };

  const flag = readBotFlagFromToken(token);

  try {
    const res = await proxiedRequest(GET_USER_URL, {
      headers: {
        Cookie: `sso=${token}; sso-rw=${token}`,
        'User-Agent': UA,
        Accept: 'application/json'
      },
      proxy
    });

    if (res.status === 200) {
      const u = res.data as Record<string, unknown>;
      const riskLevel =
        u.riskLevel != null && String(u.riskLevel).trim()
          ? String(u.riskLevel).trim()
          : null;
      const parsed = parseCastleRiskDetails(
        u.botFlagDetails ?? u.bot_flag_details ?? u.botFlagDetail
      );
      // 部分响应可能把数值 risk 放在顶层
      let riskScore = parsed.riskScore;
      if (riskScore == null) {
        const top =
          u.risk ??
          u.riskScore ??
          u.userRisk ??
          (u as { castleRisk?: unknown }).castleRisk;
        if (typeof top === 'number' && Number.isFinite(top) && top >= 0 && top <= 1) {
          riskScore = Math.round(top * 100) / 100;
        } else if (typeof top === 'string' && /^\d*\.?\d+$/.test(top.trim())) {
          const n = Number(top.trim());
          if (Number.isFinite(n) && n >= 0 && n <= 1) {
            riskScore = Math.round(n * 100) / 100;
          }
        }
      }
      return {
        alive: true,
        status: 200,
        email: typeof u.email === 'string' ? u.email : undefined,
        givenName: typeof u.givenName === 'string' ? u.givenName : undefined,
        familyName: typeof u.familyName === 'string' ? u.familyName : undefined,
        emailConfirmed: typeof u.emailConfirmed === 'boolean' ? u.emailConfirmed : undefined,
        sessionTierId: u.sessionTierId != null ? String(u.sessionTierId) : undefined,
        createTime: typeof u.createTime === 'string' ? u.createTime : undefined,
        botFlagSource: flag.botFlagSource,
        isBotFlag1: flag.isBotFlag1,
        riskLevel,
        riskScore,
        botFlagDetails: parsed.botFlagDetails,
        riskEvent: parsed.riskEvent
      };
    }

    if (res.status === 401 || res.status === 403) {
      return {
        alive: false,
        status: res.status,
        botFlagSource: flag.botFlagSource,
        isBotFlag1: flag.isBotFlag1,
        riskScore: null,
        riskLevel: null,
        riskEvent: null,
        botFlagDetails: null
      };
    }

    return {
      alive: false,
      status: res.status,
      error: `grok 返回 HTTP ${res.status}`,
      botFlagSource: flag.botFlagSource,
      isBotFlag1: flag.isBotFlag1,
      riskScore: null,
      riskLevel: null,
      riskEvent: null,
      botFlagDetails: null
    };
  } catch (e) {
    return {
      alive: false,
      status: 0,
      error: e instanceof Error ? e.message : String(e),
      botFlagSource: flag.botFlagSource,
      isBotFlag1: flag.isBotFlag1,
      riskScore: null,
      riskLevel: null,
      riskEvent: null,
      botFlagDetails: null
    };
  }
}
