import crypto from 'crypto';
import { proxiedRequest } from '../httpClient.js';

// Constants removed, now passed dynamically

// 人名前缀：禁止 . _ -（. 会成别名；CF 默认正则会剥非 [a-z0-9]）
const EMAIL_FIRST = [
    'aaron', 'adam', 'adrian', 'alex', 'alice', 'amy', 'andrew', 'anna', 'ben',
    'brian', 'chris', 'daniel', 'david', 'emily', 'eric', 'ethan', 'grace',
    'hannah', 'jack', 'james', 'jane', 'jason', 'john', 'julia', 'kevin',
    'laura', 'leo', 'linda', 'lucas', 'mark', 'mary', 'mike', 'nathan', 'noah',
    'olivia', 'paul', 'rachel', 'ryan', 'sam', 'sarah', 'sophia', 'steve',
    'thomas', 'victor', 'will', 'william', 'zoe',
];
const EMAIL_LAST = [
    'adams', 'allen', 'baker', 'brown', 'chen', 'clark', 'davis', 'garcia',
    'hall', 'harris', 'jackson', 'johnson', 'jones', 'kim', 'lee', 'martin',
    'miller', 'moore', 'nguyen', 'smith', 'taylor', 'thomas', 'walker', 'wang',
    'white', 'williams', 'wilson', 'young', 'zhang',
];

function generateLocalPart(minLen = 8, maxLen = 16): string {
    const pick = <T,>(arr: T[]) => arr[Math.floor(Math.random() * arr.length)];
    for (let attempt = 0; attempt < 8; attempt++) {
        const first = pick(EMAIL_FIRST);
        const last = pick(EMAIL_LAST);
        const style = Math.random();
        const digits =
            style < 0.55
                ? String(Math.floor(Math.random() * 9900) + 10)
                : style < 0.8
                  ? String(Math.floor(Math.random() * 90) + 10)
                  : String(Math.floor(Math.random() * 31) + 1975);
        const mode = Math.random();
        let local: string;
        if (mode < 0.4) local = `${first}${digits}`;
        else if (mode < 0.65) local = `${first}${last[0]}${digits}`;
        else if (mode < 0.88) local = `${first}${last.slice(0, Math.min(6, last.length))}${digits}`;
        else local = `${first[0]}${last}${digits}`;
        // 禁止 . _ - 等；只保留 a-z0-9
        local = local.toLowerCase().replace(/[^a-z0-9]/g, '');
        if (!local || !/^[a-z]/.test(local)) local = `${first}${digits}`;
        if (local.length > maxLen) {
            const core = local.replace(/\d+$/, '').slice(0, Math.max(4, maxLen - digits.length));
            local = `${core || first.slice(0, 4)}${digits}`.slice(0, maxLen);
        }
        while (local.length < minLen) local += String(Math.floor(Math.random() * 10));
        local = local.slice(0, maxLen);
        if (/^[a-z]{3,}[a-z]*\d{1,4}$/.test(local) && !/[._-]/.test(local)) {
            return local;
        }
    }
    return `${pick(EMAIL_FIRST)}${Math.floor(Math.random() * 9000) + 1000}`.slice(0, maxLen);
}

function normalizeMailApiBase(raw: string): string {
    let base = String(raw || '').trim().replace(/\/+$/, '');
    for (const suffix of ['/admin/new_address', '/admin', '/api/mails', '/api']) {
        if (base.toLowerCase().endsWith(suffix)) {
            base = base.slice(0, -suffix.length).replace(/\/+$/, '');
        }
    }
    return base;
}

export async function createTempEmail(mailConfig: { apiBase: string, adminAuth: string, domain: string }): Promise<{ address: string; jwt: string; password?: string }> {
    const apiBase = normalizeMailApiBase(mailConfig.apiBase);
    if (!apiBase) throw new Error('mail apiBase 为空（需 Worker 根地址，如 https://xxx.workers.dev）');
    const url = `${apiBase}/admin/new_address`;
    const domain = String(mailConfig.domain || '').trim().replace(/^@+/, '');
    
    // Attempt 3 times（每次换新人名前缀，避免冲突/随机串）
    for (let i = 0; i < 3; i++) {
        const local = generateLocalPart();
        try {
            const res = await fetch(url, {
                method: 'POST',
                headers: {
                    'x-admin-auth': mailConfig.adminAuth,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    name: local,
                    domain,
                    enablePrefix: false
                })
            });
            
            if (res.ok) {
                const data = await res.json() as any;
                const address = String(data.address || `${local}@${domain}` || '').trim();
                const addrLocal = address.split('@')[0]?.toLowerCase() || '';
                if (/[._-]/.test(addrLocal)) {
                    console.warn(`unsafe local chars in address: ${address}, retry`);
                    continue;
                }
                if (data.jwt && address) {
                    return {
                        address,
                        jwt: data.jwt,
                        password: data.password
                    };
                }
            } else if (res.status === 400 || res.status === 409) {
                // maybe collision, try again
                continue;
            } else {
                const body = (await res.text()).slice(0, 200);
                let hint = '';
                if (res.status === 405) {
                    hint = '（405 多为填了前端 Pages 地址而非 Worker API 根）';
                } else if (res.status === 401 || res.status === 403) {
                    hint = '（管理密码 x-admin-auth 可能不对）';
                }
                throw new Error(`HTTP ${res.status}: ${body} url=${url}${hint}`);
            }
        } catch (e) {
            console.error(`Attempt ${i+1} failed to create email:`, e);
            if (i === 2) throw e;
        }
    }
    throw new Error("Failed to create email after retries.");
}

export async function fetchEmails(jwt: string, apiBase: string, limit = 20): Promise<any[]> {
    try {
        const url = `${apiBase}/api/mails?limit=${limit}&offset=0`;
        const res = await fetch(url, {
            headers: {
                'Authorization': `Bearer ${jwt}`
            }
        });
        if (res.ok) {
            const data = await res.json() as any;
            return data.results || [];
        }
    } catch (e) {
        // ignore
    }
    return [];
}

// removed old fetchEmails

export async function fetchEmailDetail(jwt: string, msgId: string, apiBase: string): Promise<any> {
    try {
        const url = `${apiBase}/api/mail/${msgId}`;
        const res = await fetch(url, {
            headers: {
                'Authorization': `Bearer ${jwt}`
            }
        });
        if (res.ok) {
            return await res.json();
        }
    } catch (e) {
        // ignore
    }
    return null;
}

export function extractVerificationCode(content: string): string | null {
    if (!content) return null;
    
    // Pattern 1: Grok XXX-XXX
    let m = content.match(/(?<![A-Z0-9-])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9-])/);
    if (m) return m[1];
    
    // Pattern 2: labeled
    m = content.match(/(?:verification code|验证码|your code)[:\s]*[<>\s]*([A-Z0-9]{3}-[A-Z0-9]{3})\b/i);
    if (m) return m[1];
    
    // Pattern 3: HTML styled
    m = content.match(/background-color:\s*#F3F3F3[^>]*>[\s\S]*?([A-Z0-9]{3}-[A-Z0-9]{3})[\s\S]*?<\/p>/);
    if (m) return m[1];
    
    // Pattern 4: Subject 6 digits
    m = content.match(/Subject:.*?(\d{6})/);
    if (m && m[1] !== "177010") return m[1];
    
    // Pattern 5: HTML tags 6 digits
    const p5 = />\s*(\d{6})\s*</g;
    let match;
    while ((match = p5.exec(content)) !== null) {
        if (match[1] !== "177010") return match[1];
    }
    
    // Pattern 6: Standalone 6 digits
    const p6 = /(?<![&#\d])(\d{6})(?![&#\d])/g;
    while ((match = p6.exec(content)) !== null) {
        if (match[1] !== "177010") return match[1];
    }
    
    return null;
}

export async function waitForVerificationCode(jwt: string, apiBase: string, timeoutS = 120): Promise<string | null> {
    const start = Date.now();
    const seenIds = new Set<string>();
    
    while (Date.now() - start < timeoutS * 1000) {
        const msgs = await fetchEmails(jwt, apiBase);
        for (const msg of msgs) {
            if (!msg || !msg.id || seenIds.has(msg.id)) continue;
            seenIds.add(msg.id);
            
            let content = msg.raw || msg.text || msg.html || msg.body || '';
            if (!content) {
                const detail = await fetchEmailDetail(jwt, msg.id, apiBase);
                if (detail) {
                    content = detail.raw || detail.text || detail.html || detail.body || '';
                }
            }
            
            if (msg.subject) {
                content = `Subject: ${msg.subject}\n${content}`;
            }
            
            const code = extractVerificationCode(content);
            if (code) {
                return code.replace('-', ''); // Return purely alphanumeric
            }
        }
        await new Promise(r => setTimeout(r, 3000));
    }
    return null;
}

/** 从邮件原文里提取 Subject 行（admin 接口的邮件对象不带独立 subject 字段） */
function extractSubject(raw: string): string {
    if (!raw) return '';
    const m = raw.match(/^Subject:\s*(.+)$/im);
    if (!m) return '';
    let s = m[1].trim();
    // 解码常见的 RFC2047 base64 编码主题，如 =?UTF-8?B?xxxx?=
    const enc = s.match(/=\?[^?]+\?B\?([^?]+)\?=/i);
    if (enc) {
        try {
            s = Buffer.from(enc[1], 'base64').toString('utf-8');
        } catch {
            // 解码失败就用原文
        }
    }
    return s;
}

export interface LatestCodeResult {
    code: string | null;
    subject: string | null;
    receivedAt: string | null;
    hasMail: boolean;
    error?: string;
}

/**
 * 通过 admin 接口按邮箱地址取最新一封邮件并提取验证码。
 * 账号记录里没有每个地址的 JWT，所以用 admin 的 x-admin-auth 头按地址查。
 */
export async function fetchLatestCodeByAddress(
    address: string,
    mailConfig: { apiBase: string; adminAuth: string },
    proxy?: string
): Promise<LatestCodeResult> {
    const empty: LatestCodeResult = {
        code: null,
        subject: null,
        receivedAt: null,
        hasMail: false
    };

    if (!address) return { ...empty, error: '缺少邮箱地址' };
    if (!mailConfig.apiBase || !mailConfig.adminAuth) {
        return { ...empty, error: '缺少邮箱后端配置' };
    }

    const base = mailConfig.apiBase.replace(/\/+$/, '');
    const url = `${base}/admin/mails?address=${encodeURIComponent(address)}&limit=10&offset=0`;

    try {
        const res = await proxiedRequest(url, {
            headers: { 'x-admin-auth': mailConfig.adminAuth },
            proxy
        });
        if (res.status !== 200) {
            return { ...empty, error: `邮箱后端返回 HTTP ${res.status}` };
        }

        const data = res.data as { results?: any[] } | null;
        const results = Array.isArray(data?.results) ? data!.results! : [];
        if (results.length === 0) return empty;

        // 按 created_at 倒序，最新在前
        results.sort((a, b) => {
            const ta = Date.parse(a?.created_at ?? '') || 0;
            const tb = Date.parse(b?.created_at ?? '') || 0;
            return tb - ta;
        });

        // 逐封从新到旧找出第一条能解析出验证码的
        for (const mail of results) {
            const content =
                (mail?.raw as string) ||
                (mail?.text as string) ||
                (mail?.html as string) ||
                '';
            const subject = (mail?.subject as string) || extractSubject(content);
            const merged = subject ? `Subject: ${subject}\n${content}` : content;
            const code = extractVerificationCode(merged);
            if (code) {
                return {
                    code,
                    subject: subject || null,
                    receivedAt: mail?.created_at ?? null,
                    hasMail: true
                };
            }
        }

        // 有邮件但没解析到验证码：回最新一封的信息
        const latest = results[0];
        const latestContent =
            (latest?.raw as string) || (latest?.text as string) || (latest?.html as string) || '';
        return {
            code: null,
            subject: (latest?.subject as string) || extractSubject(latestContent) || null,
            receivedAt: latest?.created_at ?? null,
            hasMail: true
        };
    } catch (e) {
        return { ...empty, error: e instanceof Error ? e.message : String(e) };
    }
}
