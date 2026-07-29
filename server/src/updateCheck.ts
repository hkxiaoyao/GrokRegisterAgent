/**
 * 本地版本与检查更新：以 BUILD_ID（git short SHA）为准，便于对照镜像/注册机日志。
 * 优先级：REGISTER_BUILD / GIT_* 环境变量 → BUILD_ID 文件 → package.json version 兜底。
 * 远端对比：GitHub beta 分支最新 commit short SHA（开发主线在 beta）。
 *
 * 不在容器内执行 docker pull/recreate（需宿主机 Docker）；只返回命令与 host-override 提示。
 * 见 issue #10：强拉镜像仍旧版，多半是 ./register → /opt/register-host 覆盖。
 */
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import type { UpdateDeployMode, UpdateInfo } from '@shared/ipc';

const REPO = 'MurasameCyan/GrokRegisterAgent';
const BETA_REF = 'beta';
const IMAGE = 'ghcr.io/murasamecyan/grokregisteragent:beta';
const __dirname = dirname(fileURLToPath(import.meta.url));

let cachedBuildId: string | null = null;
let cachedDeploy: ReturnType<typeof detectDeployContext> | null = null;

function shortSha(raw: string): string {
  const v = (raw || '').trim();
  if (!v) return '';
  // full or short hex sha
  if (/^[0-9a-fA-F]{7,40}$/.test(v)) return v.slice(0, 7).toLowerCase();
  return v.slice(0, 32);
}

function readBuildIdFile(path: string): string | null {
  try {
    if (!existsSync(path)) return null;
    const line = readFileSync(path, 'utf-8').trim().split(/\r?\n/)[0]?.trim() || '';
    const s = shortSha(line);
    return s || null;
  } catch {
    return null;
  }
}

/** 解析当前运行构建号（short hash / BUILD_ID） */
export function currentBuildId(): string {
  if (cachedBuildId) return cachedBuildId;

  for (const key of [
    'REGISTER_BUILD',
    'GIT_COMMIT',
    'GIT_SHA',
    'SOURCE_COMMIT',
    'GITHUB_SHA',
    'BUILD_ID'
  ]) {
    const env = shortSha(process.env[key] || '');
    if (env) {
      cachedBuildId = env;
      return cachedBuildId;
    }
  }

  const fileCandidates = [
    join(process.cwd(), 'register', 'BUILD_ID'),
    join(process.cwd(), 'BUILD_ID'),
    '/app/register/BUILD_ID',
    '/app/BUILD_ID',
    join(__dirname, '..', '..', '..', '..', 'register', 'BUILD_ID'),
    join(__dirname, '..', '..', '..', '..', 'BUILD_ID')
  ];
  for (const p of fileCandidates) {
    const v = readBuildIdFile(p);
    if (v) {
      cachedBuildId = v;
      return cachedBuildId;
    }
  }

  // 最后兜底 package.json（非 hash 时仍显示，便于未注入 BUILD_ID 的开发态）
  const pkgCandidates = [
    join(__dirname, '..', '..', '..', '..', 'package.json'),
    join(process.cwd(), 'package.json')
  ];
  for (const path of pkgCandidates) {
    try {
      const pkg = JSON.parse(readFileSync(path, 'utf-8')) as { version?: string };
      if (pkg.version) {
        cachedBuildId = pkg.version;
        return cachedBuildId;
      }
    } catch {
      // next
    }
  }

  cachedBuildId = 'unknown';
  return cachedBuildId;
}

/** @deprecated 使用 currentBuildId；保留别名兼容旧 import */
export function currentVersion(): string {
  return currentBuildId();
}

function registerLooksComplete(dir: string): boolean {
  try {
    return (
      existsSync(join(dir, 'runner.py')) ||
      existsSync(join(dir, 'DrissionPage_example.py'))
    );
  } catch {
    return false;
  }
}

function isDockerLike(): boolean {
  if (existsSync('/.dockerenv')) return true;
  if ((process.env.DATA_DIR || '') === '/data') return true;
  if (existsSync('/app/server/dist') && existsSync('/app/register')) return true;
  return false;
}

/** 探测部署形态：决定更新提示，不执行任何升级动作 */
export function detectDeployContext(): {
  deployMode: UpdateDeployMode;
  hostRegisterOverride: boolean;
  hostRegisterSrc: string | null;
  updateCommands: string;
  updateHint: string;
} {
  if (cachedDeploy) return cachedDeploy;

  const hostSrc = (process.env.REGISTER_HOST_SRC || '/opt/register-host').trim();
  const hostOverride =
    Boolean(hostSrc) && registerLooksComplete(hostSrc);
  const docker = isDockerLike();

  let deployMode: UpdateDeployMode = 'unknown';
  if (docker && hostOverride) deployMode = 'docker-host-register';
  else if (docker) deployMode = 'docker-image';
  else deployMode = 'native';

  let updateCommands: string;
  let updateHint: string;

  if (deployMode === 'docker-host-register') {
    updateCommands = [
      '# 当前容器启用了宿主 ./register → /opt/register-host 覆盖。',
      '# 只 docker pull 镜像不够：entrypoint 仍会同步宿主旧脚本，Build 可能不变。',
      '# 在 compose 所在目录执行：',
      'git pull',
      'docker compose pull',
      `docker compose up -d --force-recreate --remove-orphans`,
      '# 若希望只吃镜像、不跟宿主 register：去掉 volumes 里的 ./register:/opt/register-host 后再 up'
    ].join('\n');
    updateHint =
      '检测到宿主 register 覆盖：请 git pull 宿主代码 + compose pull 且 --force-recreate；只强拉镜像常仍显示旧 Build（#10）';
  } else if (deployMode === 'docker-image') {
    updateCommands = [
      '# 纯镜像部署：在 docker-compose.yml 所在目录执行',
      'docker compose pull',
      'docker compose up -d --force-recreate --remove-orphans',
      `# 镜像: ${IMAGE}`,
      '# 不要只用 restart；需 recreate 才能换新层。确认日志 Build: 与 UI BUILD_ID 一致。'
    ].join('\n');
    updateHint =
      'Docker 镜像更新：宿主机 docker compose pull && up -d --force-recreate（应用内无法安全自重建容器）';
  } else {
    updateCommands = [
      '# 非 Docker / 源码运行：拉取 beta 后按你的进程管理重启服务',
      'git fetch origin beta && git checkout beta && git pull',
      'npm install && npm run server:build',
      '# 再重启 node / 面板进程'
    ].join('\n');
    updateHint = '源码部署：git pull beta 后重建并重启服务（无应用内一键替换）';
  }

  cachedDeploy = {
    deployMode,
    hostRegisterOverride: hostOverride,
    hostRegisterSrc: hostOverride ? hostSrc : null,
    updateCommands,
    updateHint
  };
  return cachedDeploy;
}

function withDeployFields(info: UpdateInfo): UpdateInfo {
  const d = detectDeployContext();
  return {
    ...info,
    deployMode: d.deployMode,
    hostRegisterOverride: d.hostRegisterOverride,
    updateCommands: d.updateCommands,
    updateHint: d.updateHint
  };
}

export async function checkForUpdate(): Promise<UpdateInfo> {
  const current = currentBuildId();
  const base = withDeployFields({
    current,
    latest: null,
    hasUpdate: false,
    htmlUrl: `https://github.com/${REPO}/commits/${BETA_REF}`,
    publishedAt: null,
    buildId: current
  });

  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    // beta 最新 commit
    const resp = await fetch(
      `https://api.github.com/repos/${REPO}/commits/${encodeURIComponent(BETA_REF)}`,
      {
        headers: {
          Accept: 'application/vnd.github+json',
          'User-Agent': 'grok-register-agent'
        },
        signal: controller.signal
      }
    );
    clearTimeout(timer);

    if (resp.status === 404) {
      return { ...base, error: `分支 ${BETA_REF} 不可用` };
    }
    if (!resp.ok) {
      return { ...base, error: `GitHub 返回 HTTP ${resp.status}` };
    }

    const data = (await resp.json()) as {
      sha?: string;
      html_url?: string;
      commit?: { committer?: { date?: string }; author?: { date?: string } };
    };
    const latestFull = (data.sha || '').trim();
    const latest = shortSha(latestFull) || null;
    const localNorm = shortSha(current);
    const remoteNorm = latest || '';
    const bothHash =
      /^[0-9a-f]{7,}$/i.test(localNorm) && /^[0-9a-f]{7,}$/i.test(remoteNorm);
    const hasUpdate = bothHash
      ? localNorm.toLowerCase() !== remoteNorm.toLowerCase()
      : Boolean(latest && latest !== current);

    return withDeployFields({
      current,
      latest,
      hasUpdate,
      htmlUrl: data.html_url || base.htmlUrl,
      publishedAt:
        data.commit?.committer?.date || data.commit?.author?.date || null,
      buildId: current
    });
  } catch (err) {
    return {
      ...base,
      error: err instanceof Error ? err.message : String(err)
    };
  }
}
