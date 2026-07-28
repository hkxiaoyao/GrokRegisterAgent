"""注册浏览器随机特征（UA / 语言 / 时区 / 平台）。

每轮注册调用 build_fingerprint() 得到一份配置，再 apply_to_options / inject_stealth。
"""
from __future__ import annotations

import random
import secrets
from dataclasses import dataclass, asdict
from typing import Any


# 贴近当前主流桌面 Chrome（仍随机，避免全员同一大版本）
# 有限规避：版本池越新越贴近真实用户分布；仍无保证不被 bot 模型命中
# 注意：UA 大版本应尽量贴近真实 Chromium（见 build_fingerprint(chrome_major=…)）
_CHROME_VERS = [128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150]


@dataclass
class BrowserFingerprint:
    user_agent: str
    platform: str  # Win32 / MacIntel / Linux x86_64
    languages: list[str]
    accept_lang: str
    timezone: str
    locale: str
    hardware_concurrency: int
    device_memory: int
    max_touch_points: int
    window_w: int
    window_h: int
    # WebGL 伪装（stealth 用；无保证）
    webgl_vendor: str = "Google Inc. (NVIDIA)"
    webgl_renderer: str = "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    # 逐号 canvas/audio 噪声种子（64-bit 无符号）。同一号内固定→canvas 自洽；
    # 号与号不同→打散「同机连续号 canvas/audio 哈希一致」这一强关联簇 key。
    noise_seed: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


_TZ_POOL = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Toronto",
    "America/Phoenix",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Amsterdam",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Asia/Hong_Kong",
    "Australia/Sydney",
    "Pacific/Auckland",
]

_LANG_POOL = [
    (["en-US", "en"], "en-US,en;q=0.9"),
    (["en-GB", "en"], "en-GB,en;q=0.9"),
    (["en-US", "en", "es"], "en-US,en;q=0.9,es;q=0.8"),
    (["en-CA", "en"], "en-CA,en;q=0.9"),
    (["en-AU", "en"], "en-AU,en;q=0.9"),
]

# 代理出口国家（ISO 3166-1 alpha-2）→ 该国合理的时区 + 语言子集。
# 目的：消除「IP 落在美国、时区却随机成 Asia/Tokyo」这类三方错配画像。
# 未命中的国家回退到全局 _TZ_POOL / _LANG_POOL 随机（保持旧行为）。
#
# 语言策略：时区按出口国对齐（风控关键），但 Accept-Language 第一项统一 en，
# 让 x.ai 渲染英文 UI（否则日/德/法页面按钮文案脚本匹配不到，注册第一步就失败）。
# 「身处德国/日本但浏览器用英文」是常见且自洽的真实用户画像，非本地语言不影响
# IP↔时区一致性。本地语言作为 q<en 的次选保留，兼顾画像真实度。
_GEO_PROFILE: dict[str, dict[str, list]] = {
    "US": {
        "tz": [
            "America/New_York",
            "America/Chicago",
            "America/Denver",
            "America/Los_Angeles",
            "America/Phoenix",
        ],
        "lang": [(["en-US", "en"], "en-US,en;q=0.9")],
    },
    "CA": {
        "tz": ["America/Toronto", "America/Vancouver", "America/Edmonton"],
        "lang": [
            (["en-CA", "en"], "en-CA,en;q=0.9"),
            (["en-CA", "fr-CA", "en"], "en-CA,fr-CA;q=0.9,en;q=0.8"),
        ],
    },
    "GB": {
        "tz": ["Europe/London"],
        "lang": [(["en-GB", "en"], "en-GB,en;q=0.9")],
    },
    "IE": {
        "tz": ["Europe/Dublin"],
        "lang": [(["en-IE", "en"], "en-IE,en;q=0.9")],
    },
    "AU": {
        "tz": ["Australia/Sydney", "Australia/Melbourne", "Australia/Perth"],
        "lang": [(["en-AU", "en"], "en-AU,en;q=0.9")],
    },
    "NZ": {
        "tz": ["Pacific/Auckland"],
        "lang": [(["en-NZ", "en"], "en-NZ,en;q=0.9")],
    },
    "DE": {
        "tz": ["Europe/Berlin"],
        "lang": [(["en-US", "en", "de"], "en-US,en;q=0.9,de;q=0.7")],
    },
    "FR": {
        "tz": ["Europe/Paris"],
        "lang": [(["en-US", "en", "fr"], "en-US,en;q=0.9,fr;q=0.7")],
    },
    "NL": {
        "tz": ["Europe/Amsterdam"],
        "lang": [(["en-US", "en", "nl"], "en-US,en;q=0.9,nl;q=0.7")],
    },
    "SG": {
        "tz": ["Asia/Singapore"],
        "lang": [(["en-SG", "en"], "en-SG,en;q=0.9")],
    },
    "HK": {
        "tz": ["Asia/Hong_Kong"],
        "lang": [(["en-HK", "en", "zh-HK"], "en-HK,en;q=0.9,zh-HK;q=0.7")],
    },
    "JP": {
        "tz": ["Asia/Tokyo"],
        "lang": [(["en-US", "en", "ja"], "en-US,en;q=0.9,ja;q=0.7")],
    },
}

# 常见桌面 GPU 字符串（仅降低「全员同一 WebGL」；无法对抗服务端 bot_flag 签发）
_WEBGL_POOL = [
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (AMD)",
        "ANGLE (AMD, AMD Radeon RX 580 Series Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (Apple)",
        "ANGLE (Apple, Apple M1, OpenGL 4.1)",
    ),
]


def _geo_profile(country: str | None) -> dict | None:
    """按 ISO alpha-2 国家码取地理画像；未命中/无效返回 None（回退全局随机）。

    返回 {"timezones": [...], "langs": [(langs, accept), ...]}。
    """
    if not country:
        return None
    cc = str(country).strip().upper()
    if len(cc) != 2:
        return None
    prof = _GEO_PROFILE.get(cc)
    if not prof:
        return None
    tzs = prof.get("tz") or []
    langs = prof.get("lang") or []
    if not tzs or not langs:
        return None
    return {"timezones": list(tzs), "langs": list(langs)}


def _chrome_ua(platform_token: str, chrome_major: int) -> str:
    return (
        f"Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Safari/537.36"
    )


def build_fingerprint(
    seed: str | None = None,
    *,
    chrome_major: int | None = None,
    prefer_native_os: bool = True,
    geo_country: str | None = None,
) -> BrowserFingerprint:
    """生成浏览器指纹。

    chrome_major: 若传入真实 Chromium 大版本，UA 将使用该版本（±0~1 微调），
    避免「二进制 150 + UA 137」被 Turnstile 直接判定异常。
    prefer_native_os: Linux 容器上提高 Linux UA 权重，减少 Win/Mac 错配。
    geo_country: 代理出口国家（ISO alpha-2）。命中 _GEO_PROFILE 时，时区与语言
        从该国子集抽取，消除 IP↔时区↔语言错配画像；未命中则回退全局随机池。
    """
    import platform as _plat

    rnd = random.Random(seed) if seed else random.Random(secrets.randbits(64))
    if chrome_major and 80 <= int(chrome_major) <= 200:
        # 已知真实 major 时禁止 jitter：UA 与二进制差 1 也会抬 600010
        chrome = int(chrome_major)
    else:
        # 未知版本时宁可用池中值，但 Windows 上尽量别太离谱
        chrome = rnd.choice(_CHROME_VERS)

    sys_name = (_plat.system() or "").lower()
    if prefer_native_os and sys_name == "linux":
        # 容器多为 Linux：70% Linux / 25% Win / 5% Mac（Mac 在 Linux 上最易穿帮）
        r = rnd.random()
        if r < 0.70:
            choice = 2
        elif r < 0.95:
            choice = 0
        else:
            choice = 1
    elif prefer_native_os and sys_name == "windows":
        # 手动 Chrome 就是 Win32；混 Mac UA 在真 Win 主机上抬 bot 分
        choice = 0
    elif prefer_native_os and sys_name == "darwin":
        r = rnd.random()
        choice = 1 if r < 0.80 else (0 if r < 0.95 else 2)
    else:
        choice = rnd.randrange(3)

    if choice == 0:
        # Windows
        platform = "Win32"
        token = "Windows NT 10.0; Win64; x64"
        max_touch = 0
    elif choice == 1:
        platform = "MacIntel"
        token = "Macintosh; Intel Mac OS X 10_15_7"
        max_touch = 0
    else:
        platform = "Linux x86_64"
        token = "X11; Linux x86_64"
        max_touch = 0

    # 时区/语言：优先按代理出口国家对齐，消除 IP↔时区↔语言错配
    geo = _geo_profile(geo_country)
    if geo is not None:
        tz = rnd.choice(geo["timezones"])
        langs, accept = rnd.choice(geo["langs"])
    else:
        langs, accept = rnd.choice(_LANG_POOL)
        tz = rnd.choice(_TZ_POOL)
    # 常见分辨率（含 Xvfb 常用）
    sizes = [
        (1920, 1080),
        (1680, 1050),
        (1600, 900),
        (1536, 864),
        (1440, 900),
        (1366, 768),
        (1280, 720),
        (2560, 1440),
    ]
    w, h = rnd.choice(sizes)
    # 平台与 WebGL 串尽量一致，避免 Linux+Apple / Win+Apple 等明显错配
    if platform == "MacIntel":
        mac_pool = [x for x in _WEBGL_POOL if "Apple" in x[0] or "Intel" in x[0]]
        wv, wr = rnd.choice(mac_pool or _WEBGL_POOL)
    else:
        non_apple = [x for x in _WEBGL_POOL if "Apple" not in x[0]]
        wv, wr = rnd.choice(non_apple or _WEBGL_POOL)
    return BrowserFingerprint(
        user_agent=_chrome_ua(token, chrome),
        platform=platform,
        languages=list(langs),
        accept_lang=accept,
        timezone=tz,
        locale=langs[0],
        hardware_concurrency=rnd.choice([4, 6, 8, 12, 16]),
        device_memory=rnd.choice([4, 8, 16]),
        max_touch_points=max_touch,
        window_w=w,
        window_h=h,
        webgl_vendor=wv,
        webgl_renderer=wr,
        # 噪声种子始终独立随机（即便复现 seed 也要每号不同的 canvas/audio）
        noise_seed=secrets.randbits(64),
    )


def apply_to_chromium_options(co: Any, fp: BrowserFingerprint) -> None:
    """写入 ChromiumOptions（DrissionPage）。"""
    try:
        co.set_user_agent(fp.user_agent)
    except Exception:
        try:
            co.set_argument(f"--user-agent={fp.user_agent}")
        except Exception:
            pass
    try:
        co.set_argument(f"--window-size={fp.window_w},{fp.window_h}")
        co.set_argument(f"--lang={fp.locale}")
        co.set_argument(f"--accept-lang={fp.accept_lang}")
    except Exception:
        pass


def stealth_js(fp: BrowserFingerprint) -> str:
    """返回注入页面的 stealth JS（有限规避，无法改服务端 bot_flag_source）。"""
    langs_js = json_dumps(fp.languages)
    noise_seed = int(getattr(fp, "noise_seed", 0) or 0) & 0xFFFFFFFF
    return f"""
(() => {{
  try {{
    Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
  }} catch (e) {{}}
  try {{
    if (!window.chrome) window.chrome = {{ runtime: {{}}, loadTimes: function() {{}}, csi: function() {{}}, app: {{}} }};
    else {{
      try {{ if (!window.chrome.runtime) window.chrome.runtime = {{}}; }} catch (e) {{}}
    }}
  }} catch (e) {{}}
  try {{
    Object.defineProperty(navigator, 'languages', {{ get: () => {langs_js} }});
  }} catch (e) {{}}
  try {{
    Object.defineProperty(navigator, 'language', {{ get: () => {json_dumps(fp.locale)} }});
  }} catch (e) {{}}
  try {{
    Object.defineProperty(navigator, 'platform', {{ get: () => {json_dumps(fp.platform)} }});
  }} catch (e) {{}}
  try {{
    Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {fp.hardware_concurrency} }});
  }} catch (e) {{}}
  try {{
    Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {fp.device_memory} }});
  }} catch (e) {{}}
  try {{
    Object.defineProperty(navigator, 'maxTouchPoints', {{ get: () => {fp.max_touch_points} }});
  }} catch (e) {{}}
  // 不再伪造 plugins：假 PluginArray 比真 Chrome 插件列表更容易被 600010 打中
  try {{
    const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (originalQuery) {{
      window.navigator.permissions.query = (parameters) => (
        parameters && parameters.name === 'notifications'
          ? Promise.resolve({{ state: Notification.permission }})
          : originalQuery.call(window.navigator.permissions, parameters)
      );
    }}
  }} catch (e) {{}}
  try {{
    const tz = {json_dumps(fp.timezone)};
    const orig = Intl.DateTimeFormat.prototype.resolvedOptions;
    Intl.DateTimeFormat.prototype.resolvedOptions = function () {{
      const r = orig.apply(this, arguments);
      try {{ r.timeZone = tz; }} catch (e) {{}}
      return r;
    }};
  }} catch (e) {{}}
  // 默认不伪装 WebGL unmasked 串：假 NVIDIA 盖真 Intel 会被 Turnstile 交叉校验打成
  // Verification failed。仅当显式 window.__fp_force_webgl_spoof=1 时才覆盖。
  try {{
    if (window.__fp_force_webgl_spoof) {{
      const vendor = {json_dumps(fp.webgl_vendor)};
      const renderer = {json_dumps(fp.webgl_renderer)};
      const patchGetParam = (proto) => {{
        if (!proto || !proto.getParameter) return;
        const orig = proto.getParameter;
        proto.getParameter = function (param) {{
          const UNMASKED_VENDOR = 0x9245;
          const UNMASKED_RENDERER = 0x9246;
          if (param === UNMASKED_VENDOR) return vendor;
          if (param === UNMASKED_RENDERER) return renderer;
          return orig.apply(this, arguments);
        }};
      }};
      try {{ patchGetParam(WebGLRenderingContext && WebGLRenderingContext.prototype); }} catch (e) {{}}
      try {{ patchGetParam(WebGL2RenderingContext && WebGL2RenderingContext.prototype); }} catch (e) {{}}
    }}
  }} catch (e) {{}}
  try {{
    // 弱化 AutomationControlled / cdc_ 痕迹（尽力）
    const clean = (obj) => {{
      if (!obj) return;
      for (const k of Object.getOwnPropertyNames(obj)) {{
        if (/^cdc_|^\\$cdc_|^__driver|^__webdriver|^__selenium|^__fxdriver/i.test(k)) {{
          try {{ delete obj[k]; }} catch (e) {{}}
        }}
      }}
    }};
    clean(window);
    clean(document);
  }} catch (e) {{}}
  // ── 逐号 canvas / audio 噪声 ──────────────────────────────────────
  // 目的：同机连续注册时，canvas/audio 哈希天生逐号相同，是风控把同批号
  // 聚成一簇的强 key。这里按 noise_seed 注入人眼/人耳不可感的微扰，使每号
  // 哈希不同、单号内自洽。不撒类别谎（不同于 WebGL vendor 伪装），隐私插件
  // 亦用此法，Turnstile 无外部真值可交叉校验，故安全。
  try {{
    const SEED = {noise_seed} >>> 0;
    if (SEED) {{
      // mulberry32：确定性 PRNG，同 seed → 同噪声序列 → 单号内 canvas 自洽
      const mkRand = (s) => {{
        let a = (s >>> 0) || 1;
        return () => {{
          a |= 0; a = (a + 0x6D2B79F5) | 0;
          let t = Math.imul(a ^ (a >>> 15), 1 | a);
          t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
          return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        }};
      }};
      // Canvas：对 getImageData 的像素做 ±1 微扰（逐 seed 固定偏移图案）
      try {{
        const proto = CanvasRenderingContext2D && CanvasRenderingContext2D.prototype;
        if (proto && proto.getImageData) {{
          const origGID = proto.getImageData;
          proto.getImageData = function () {{
            const img = origGID.apply(this, arguments);
            try {{
              const r = mkRand(SEED);
              const d = img.data;
              // 稀疏扰动：约 1/13 采样点 ±1，足以改哈希、不改视觉
              for (let i = 0; i < d.length; i += 4) {{
                if (r() < 0.08) {{
                  const dv = r() < 0.5 ? -1 : 1;
                  d[i] = Math.max(0, Math.min(255, d[i] + dv));
                  d[i + 1] = Math.max(0, Math.min(255, d[i + 1] + dv));
                  d[i + 2] = Math.max(0, Math.min(255, d[i + 2] + dv));
                }}
              }}
            }} catch (e) {{}}
            return img;
          }};
        }}
      }} catch (e) {{}}
      // Canvas：toDataURL / toBlob 走上面被 hook 的读路径即可，
      // 但部分实现直读底层缓冲，故对 HTMLCanvasElement 亦做一层包裹。
      try {{
        const cproto = HTMLCanvasElement && HTMLCanvasElement.prototype;
        if (cproto && cproto.toDataURL) {{
          const origTDU = cproto.toDataURL;
          cproto.toDataURL = function () {{
            try {{
              const ctx = this.getContext && this.getContext('2d');
              if (ctx && this.width && this.height) {{
                const r = mkRand(SEED ^ 0x9E3779B9);
                // 在右下角落一个 alpha=254/255 的近乎透明微点，扰动最终哈希
                const x = this.width - 1, y = this.height - 1;
                const px = ctx.getImageData(x, y, 1, 1);
                px.data[3] = px.data[3] > 0 ? px.data[3] - (r() < 0.5 ? 0 : 1) : px.data[3];
                ctx.putImageData(px, x, y);
              }}
            }} catch (e) {{}}
            return origTDU.apply(this, arguments);
          }};
        }}
      }} catch (e) {{}}
      // Audio：对 getChannelData / getFloatFrequencyData 加极小噪声（~1e-7 量级）
      try {{
        const ap = (window.AudioBuffer && AudioBuffer.prototype) || null;
        if (ap && ap.getChannelData) {{
          const origGCD = ap.getChannelData;
          ap.getChannelData = function () {{
            const buf = origGCD.apply(this, arguments);
            try {{
              const r = mkRand(SEED ^ 0x85EBCA6B);
              for (let i = 0; i < buf.length; i += 100) {{
                buf[i] = buf[i] + (r() - 0.5) * 1e-7;
              }}
            }} catch (e) {{}}
            return buf;
          }};
        }}
      }} catch (e) {{}}
      try {{
        const anp = (window.AnalyserNode && AnalyserNode.prototype) || null;
        if (anp && anp.getFloatFrequencyData) {{
          const origFFD = anp.getFloatFrequencyData;
          anp.getFloatFrequencyData = function (arr) {{
            origFFD.apply(this, arguments);
            try {{
              const r = mkRand(SEED ^ 0xC2B2AE35);
              for (let i = 0; i < arr.length; i += 50) {{
                arr[i] = arr[i] + (r() - 0.5) * 1e-4;
              }}
            }} catch (e) {{}}
          }};
        }}
      }} catch (e) {{}}
    }}
  }} catch (e) {{}}
}})();
"""


def human_pause(min_ms: int = 120, max_ms: int = 480) -> float:
    """步骤间随机停顿（秒）。有限行为随机，无法事后抹掉已签发 bot_flag。"""
    import time

    lo = max(0, int(min_ms))
    hi = max(lo, int(max_ms))
    secs = (lo + secrets.randbelow(hi - lo + 1)) / 1000.0
    time.sleep(secs)
    return secs


def json_dumps(v: Any) -> str:
    import json

    return json.dumps(v, ensure_ascii=False)
