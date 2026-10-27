# -*- coding: utf-8 -*-
"""
AI 사용량 스티커 위젯 — Claude / ChatGPT(Codex) 멀티 계정
- 계정마다 Claude 또는 ChatGPT를 선택해서 추가 (탭의 색 점으로 구분: 주황=Claude, 초록=ChatGPT)
  · Claude  : claude.ai 5시간 세션 / 주간 사용량
  · ChatGPT : chatgpt.com Codex 사용 한도 (5시간 / 주간)
- 계정마다 독립된 브라우저 프로필 폴더 사용 → 로그인/로그아웃 충돌 없음
  (Edge/Chrome은 실행 파일만 빌려 쓰고, 평소 쓰는 브라우저 프로필은 건드리지 않음)
- 모든 계정 사용량을 백그라운드에서 함께 갱신 → 탭 전환 시 즉시 표시
- 둥근 스티커 디자인, 우하단 모서리 드래그 또는 Ctrl+휠로 크기 조절(70~200%)
- Claude 팀 계정은 개인/팀 중 어느 플랜 사용량을 볼지 선택 (우클릭 → 조직 선택)
- 드래그로 위치 이동, ✕=종료 · –=작업표시줄로 축소(작업표시줄 아이콘 클릭 시 복귀)
- 세션 만료 시 프로필 폴더에서 조용히 자동 복원 시도 → 안 되면 "재로그인 필요" 표시

실행:   python ai_usage_sticker.py
진단:   python ai_usage_sticker.py --dump
Edge로: python ai_usage_sticker.py --browser edge   (로그인 창을 Edge로)
구방식: python ai_usage_sticker.py --profile work   (인스턴스 자체를 분리 실행)
필요:   pip install curl_cffi playwright pillow
        playwright install chromium   (Edge/Chrome이 설치된 PC라면 생략 가능)
"""

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

BASE = "https://claude.ai"
GPT_BASE = "https://chatgpt.com"
GPT_USAGE_URL = f"{GPT_BASE}/backend-api/wham/usage"
GPT_SESSION_COOKIE = "__Secure-next-auth.session-token"   # 길면 .0/.1 로 쪼개져 저장됨

# exe(PyInstaller --windowed)로 실행될 때 대응
if getattr(sys, "frozen", False):
    # 콘솔이 없어 stdout/stderr가 None이면 일부 라이브러리가 죽는 것 방지
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    # 설정/로그인 프로필을 임시 압축해제 폴더가 아닌 exe가 있는 폴더에 저장
    DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(DIR, "ai_usage_config.json")
LEGACY_CONFIG_FILE = os.path.join(DIR, "claude_sticker_config.json")   # 구버전 설정 자동 이전
BROWSER_CHANNEL = None   # None=내장 크로미움, "msedge"=Edge, "chrome"=설치된 크롬
PROFILE_LABEL = ""       # --profile 이름 (인스턴스 분리 실행용, 제목에 표시)
ICON_NAME = "ai_usage_sticker.ico"
THEMES_DIRNAME = "themes"   # exe/스크립트 옆의 테마 폴더

# 서비스별 표시 정보
PROVIDERS = {
    "claude": {
        "label": "Claude",
        "color": "#d97757",
        "login_url": f"{BASE}/login",
        "rows": {"session": "5시간 세션", "weekly": "주간 · 전체"},
    },
    "gpt": {
        "label": "ChatGPT",
        "color": "#19c37d",
        "login_url": f"{GPT_BASE}/auth/login",
        "rows": {"session": "Codex · 5시간", "weekly": "Codex · 주간"},
    },
}


def provider_of(acc):
    p = (acc or {}).get("provider", "claude")
    return p if p in PROVIDERS else "claude"


def resource_path(name):
    """exe(onefile) 내부에 번들된 리소스 또는 스크립트 옆 파일 경로."""
    cand = os.path.join(getattr(sys, "_MEIPASS", DIR), name)
    return cand if os.path.exists(cand) else os.path.join(DIR, name)


def themes_root():
    """테마 폴더 위치. exe 옆(DIR)을 우선 보고, 없으면 번들 내부도 확인."""
    d = os.path.join(DIR, THEMES_DIRNAME)
    if os.path.isdir(d):
        return d
    bundled = os.path.join(getattr(sys, "_MEIPASS", DIR), THEMES_DIRNAME)
    return bundled if os.path.isdir(bundled) else d


def list_themes():
    """themes 폴더 안의 하위 폴더(각각 하나의 테마) 이름 목록."""
    root = themes_root()
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        sub = os.path.join(root, name)
        if os.path.isdir(sub) and any(
                f.lower().endswith((".png", ".gif")) for f in os.listdir(sub)):
            out.append(name)
    return out


DEFAULT_CONFIG = {
    # 계정이름 -> {"provider": "claude", "session_key": "", "org_uuid": ""}
    #           또는 {"provider": "gpt", "cookies": {...}, "user_agent": ""}
    "accounts": {},
    "active_account": "",
    "usage_url": "",       # (Claude) 비워두면 후보 URL 자동 시도. {org} 플레이스홀더 사용 가능
    "poll_seconds": 60,
    "scale": 1.0,          # 위젯 크기 배율 (SCALE_MIN ~ SCALE_MAX)
    "theme": "",           # "" = 기본 막대 게이지, 그 외 = themes/<이름> 폴더 사용
    "win_x": 100,
    "win_y": 100,
}

SCALE_MIN, SCALE_MAX = 0.7, 2.0

# 모던 다크 팔레트
BG = "#1c1c21"          # 스티커 카드
MENU_BG = "#202026"     # 메뉴/대화상자 카드
FG, SUB = "#ededf2", "#8e8e9a"
TRACK = "#33333b"       # 게이지 바탕
HOVER = "#2e2e36"       # 메뉴 항목 hover
BORDER = "#34343d"
SEG_BG, SEG_ON = "#2a2a31", "#41414c"
FIELD = "#16161a"
WARN, DANGER = "#e8b04a", "#ef5f5f"
KEY = "#010203"         # 창 투명 처리용 색 (둥근 모서리 바깥 부분)
ALPHA = 0.97            # 창 투명도 (낮을수록 뒷화면이 비쳐 글씨가 안 보일 수 있음)
UI_FONT = "Malgun Gothic"
NUM_FONT = "Segoe UI Semibold"

ROW_KEYS = ("session", "weekly")

EXPIRED = "__EXPIRED__"   # 내부용: 세션 만료 표식


# ---------------- 계정별 프로필 폴더 ----------------
def safe_name(name):
    """폴더 이름에 못 쓰는 문자 제거 (한글은 그대로 사용 가능)."""
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", (name or "").strip())
    return s or "계정"


def profile_dir_for(name):
    label = f"{PROFILE_LABEL}_" if PROFILE_LABEL else ""
    path = os.path.join(DIR, f"ai_login_profile_{label}" + safe_name(name))
    legacy = os.path.join(DIR, f"claude_login_profile_{label}" + safe_name(name))
    if not os.path.isdir(path) and os.path.isdir(legacy):
        try:
            os.rename(legacy, path)   # 구버전 이름의 로그인 프로필 재활용
        except Exception:
            return legacy             # 사용 중이라 못 옮기면 옛 폴더 그대로 사용
    return path


# ---------------- 설정 ----------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    src = CONFIG_FILE if os.path.exists(CONFIG_FILE) else LEGACY_CONFIG_FILE
    if os.path.exists(src):
        try:
            with open(src, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass

    # 구버전(단일 계정) 설정 자동 이전
    if not cfg.get("accounts") and cfg.get("session_key"):
        name = "계정1"
        cfg["accounts"] = {name: {"session_key": cfg.get("session_key", ""),
                                  "org_uuid": cfg.get("org_uuid", "")}}
        cfg["active_account"] = name
        legacy = os.path.join(DIR, "claude_login_profile"
                              + (f"_{PROFILE_LABEL}" if PROFILE_LABEL else ""))
        target = profile_dir_for(name)
        if os.path.isdir(legacy) and not os.path.exists(target):
            try:
                os.rename(legacy, target)   # 기존 로그인 프로필 재활용
            except Exception:
                pass
    cfg.pop("session_key", None)
    cfg.pop("org_uuid", None)

    if not isinstance(cfg.get("accounts"), dict):
        cfg["accounts"] = {}
    for acc in cfg["accounts"].values():
        acc.setdefault("provider", "claude")   # 구버전 계정은 모두 Claude
    if cfg["accounts"] and cfg.get("active_account") not in cfg["accounts"]:
        cfg["active_account"] = next(iter(cfg["accounts"]))
    if src != CONFIG_FILE:
        save_config(cfg)
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------- HTTP ----------------
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")


def _request(url, headers=None, cookies=None, ua=None):
    """GET 요청. (status, json 또는 텍스트, 응답이 새로 준 쿠키 dict) 반환."""
    h = {"User-Agent": ua or DEFAULT_UA, "Accept": "application/json"}
    h.update(headers or {})
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, headers=h, cookies=cookies or {},
                     impersonate="chrome", timeout=15)
    except ImportError:
        import requests
        r = requests.get(url, headers=h, cookies=cookies or {}, timeout=15)
    try:
        new_cookies = dict(r.cookies)
    except Exception:
        new_cookies = {}
    try:
        return r.status_code, r.json(), new_cookies
    except Exception:
        return r.status_code, getattr(r, "text", "")[:500], new_cookies


def http_get(url, session_key):
    """Claude API 요청 (sessionKey 쿠키)."""
    status, data, _ = _request(url, headers={"Referer": "https://claude.ai/"},
                               cookies={"sessionKey": session_key})
    return status, data


def key_alive(session_key):
    if not session_key:
        return False
    status, _ = http_get(f"{BASE}/api/organizations", session_key)
    return status == 200


def list_orgs(session_key):
    """계정이 속한 조직 목록 [{'uuid','name'}]. 개인 플랜과 팀이 각각 별도 조직."""
    status, data = http_get(f"{BASE}/api/organizations", session_key)
    if status == 401:
        return None, EXPIRED
    if status != 200 or not isinstance(data, list) or not data:
        return None, f"조직 조회 실패 (HTTP {status})"
    orgs = [{"uuid": o.get("uuid"), "name": (o.get("name") or "(이름 없음)").strip()}
            for o in data if o.get("uuid")]
    return (orgs, None) if orgs else (None, "조직 없음")


def get_org_uuid(cfg, name):
    acc = cfg["accounts"][name]
    if acc.get("org_uuid"):
        return acc["org_uuid"], None
    orgs, err = list_orgs(acc.get("session_key", ""))
    if err:
        return None, err
    # 조직이 하나뿐이면 확정 저장. 여러 개(개인+팀 등)면 UI의 선택 대화상자로
    # 확정하기 전까지 첫 번째를 임시로만 쓰고 저장하지 않음.
    if len(orgs) == 1:
        acc["org_uuid"] = orgs[0]["uuid"]
        save_config(cfg)
    return orgs[0]["uuid"], None


def usage_url_candidates(cfg, org):
    if cfg.get("usage_url"):
        return [cfg["usage_url"].replace("{org}", org or "")]
    return [
        f"{BASE}/api/organizations/{org}/usage",
        f"{BASE}/api/organizations/{org}/rate_limits",
    ]


# ----- ChatGPT -----
def gpt_has_session(cookies):
    return any(k.startswith(GPT_SESSION_COOKIE) for k in (cookies or {}))


def gpt_session(acc):
    """chatgpt.com 로그인 쿠키로 access token 발급. (token, account_id, err) 반환.
    응답이 갱신해 준 쿠키는 acc["cookies"]에 반영함(호출 측에서 저장)."""
    cookies = acc.get("cookies") or {}
    if not gpt_has_session(cookies):
        return None, None, EXPIRED
    status, data, new_cookies = _request(
        f"{GPT_BASE}/api/auth/session", headers={"Referer": f"{GPT_BASE}/"},
        cookies=cookies, ua=acc.get("user_agent"))
    if new_cookies:
        cookies.update(new_cookies)
        acc["cookies"] = cookies
    if status == 200 and isinstance(data, dict):
        token = data.get("accessToken")
        if token:
            return token, (data.get("account") or {}).get("id"), None
        return None, None, EXPIRED   # 로그아웃 상태면 빈 {}가 옴
    if status == 401:
        return None, None, EXPIRED
    if status == 403:
        return None, None, "403"
    return None, None, f"HTTP {status}"


def gpt_usage_request(acc):
    """(status, data) — Codex 사용량 API 직접 호출."""
    token, account_id, err = gpt_session(acc)
    if err == EXPIRED:
        return 401, None
    if err == "403":
        return 403, None
    if err:
        return 0, err
    headers = {"Authorization": f"Bearer {token}", "Referer": f"{GPT_BASE}/codex"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    status, data, _ = _request(GPT_USAGE_URL, headers=headers,
                               cookies=acc.get("cookies"), ua=acc.get("user_agent"))
    return status, data


def gpt_fetch_via_browser(profile_dir):
    """Cloudflare가 직접 요청을 막을 때: 저장된 프로필로 브라우저를 조용히 띄워
    페이지 안에서 fetch. (status, data) 반환."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return 0, None
    try:
        with sync_playwright() as p:
            ctx = _launch_ctx(p, headless=True, profile_dir=profile_dir)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(f"{GPT_BASE}/", timeout=45000, wait_until="domcontentloaded")
            res = page.evaluate("""async (url) => {
                const s = await fetch('/api/auth/session', {credentials: 'include'})
                    .then(r => r.json()).catch(() => ({}));
                if (!s.accessToken) return {status: 401, data: null};
                const h = {'Authorization': 'Bearer ' + s.accessToken};
                if (s.account && s.account.id) h['ChatGPT-Account-Id'] = s.account.id;
                const r = await fetch(url, {headers: h, credentials: 'include'});
                let d = null;
                try { d = await r.json(); } catch (e) {}
                return {status: r.status, data: d};
            }""", GPT_USAGE_URL)
            ctx.close()
            return (res or {}).get("status", 0), (res or {}).get("data")
    except Exception:
        return 0, None


def account_alive(acc):
    if provider_of(acc) == "gpt":
        return gpt_session(acc)[0] is not None
    return key_alive(acc.get("session_key"))


def fetch_usage(cfg, name):
    """지정 계정의 사용량 조회. (parsed, 상태메시지) 반환. 만료면 (None, EXPIRED)."""
    acc = cfg["accounts"].get(name) or {}
    if provider_of(acc) == "gpt":
        return fetch_gpt_usage(cfg, name)
    key = acc.get("session_key", "")
    if not key:
        return None, "로그인 필요 — 우클릭 → 재로그인"
    org, err = get_org_uuid(cfg, name)
    if err:
        return None, err
    last = "응답 없음"
    for url in usage_url_candidates(cfg, org):
        status, data = http_get(url, key)
        if status == 200 and isinstance(data, (dict, list)):
            parsed = parse_usage(data)
            if parsed:
                return parsed, "갱신 " + datetime.now().strftime("%H:%M:%S")
            return None, "파싱 실패 — --dump로 확인"
        if status == 401:
            return None, EXPIRED
        if status == 403:
            last = "차단됨(403) — curl_cffi 설치 확인"
        else:
            last = f"HTTP {status}"
    return None, last


def fetch_gpt_usage(cfg, name):
    acc = cfg["accounts"].get(name) or {}
    if not acc.get("cookies"):
        return None, "로그인 필요 — 우클릭 → 재로그인"
    status, data = gpt_usage_request(acc)
    save_config(cfg)   # 갱신된 세션 쿠키 저장
    if status == 403:
        status, data = gpt_fetch_via_browser(profile_dir_for(name))
    if status == 200 and isinstance(data, dict):
        parsed = parse_gpt_usage(data)
        if parsed:
            return parsed, "갱신 " + datetime.now().strftime("%H:%M:%S")
        return None, "Codex 사용 기록 없음/파싱 실패 — --dump로 확인"
    if status == 401:
        return None, EXPIRED
    if status == 403:
        return None, "차단됨(403) — 재로그인 해보세요"
    if isinstance(data, str) and status == 0:
        return None, data
    return None, f"HTTP {status}" if status else "응답 없음"


# ---------------- 응답 파싱 ----------------
def collect_limit_blocks(node, key_hint="", out=None):
    if out is None:
        out = []
    if isinstance(node, dict):
        if "utilization" in node or "resets_at" in node:
            out.append((key_hint, node))
        for k, v in node.items():
            collect_limit_blocks(v, k, out)
    elif isinstance(node, list):
        for item in node:
            collect_limit_blocks(item, key_hint, out)
    return out


def parse_resets_at(v):
    try:
        if isinstance(v, (int, float)):
            if v > 1e12:
                v /= 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc)
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def normalize_block(label, block):
    ratio, detail = None, ""
    u = block.get("utilization")
    if isinstance(u, (int, float)):
        ratio = u / 100.0 if u > 1 else float(u)
    elif isinstance(block.get("remaining"), (int, float)) and isinstance(block.get("limit"), (int, float)):
        limit = block["limit"]
        used = limit - block["remaining"]
        ratio = used / limit if limit else 0
        detail = f"{int(used)}/{int(limit)}"
    if ratio is None:
        return None
    return {
        "label": label or "",
        "ratio": max(0.0, min(1.0, ratio)),
        "detail": detail,
        "resets_at": parse_resets_at(block.get("resets_at")),
    }


def classify(label):
    l = (label or "").lower()
    if any(m in l for m in ("fable", "opus", "sonnet", "haiku", "model")):
        return "weekly_model"
    if any(s in l for s in ("five", "5h", "5_h", "session")):
        return "session"
    if any(s in l for s in ("seven", "7d", "7_d", "week")):
        return "weekly"
    return None


def parse_usage(data):
    blocks = collect_limit_blocks(data)
    result, extras = {}, []
    for label, block in blocks:
        norm = normalize_block(label, block)
        if not norm:
            continue
        kind = classify(label)
        if kind and kind not in result:
            result[kind] = norm
        else:
            extras.append(norm)
    for key in ROW_KEYS:
        if key not in result and extras:
            result[key] = extras.pop(0)
    return result


def parse_gpt_usage(data):
    """Codex 사용량 응답: rate_limit.primary_window / secondary_window 의
    used_percent, reset_at(또는 reset_after_seconds), limit_window_seconds."""
    rl = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else data
    now = datetime.now(timezone.utc)
    result = {}
    for wkey, fallback in (("primary_window", "session"), ("secondary_window", "weekly")):
        w = rl.get(wkey)
        if not isinstance(w, dict) or not isinstance(w.get("used_percent"), (int, float)):
            continue
        secs = w.get("limit_window_seconds")
        kind = fallback
        if isinstance(secs, (int, float)) and secs > 0:
            kind = "session" if secs <= 6 * 3600 else "weekly"
        resets = parse_resets_at(w.get("reset_at")) if w.get("reset_at") else None
        if resets is None and isinstance(w.get("reset_after_seconds"), (int, float)):
            resets = now + timedelta(seconds=w["reset_after_seconds"])
        if kind not in result:
            result[kind] = {
                "label": wkey,
                "ratio": max(0.0, min(1.0, w["used_percent"] / 100.0)),
                "detail": "",
                "resets_at": resets,
            }
    return result


# ---------------- 브라우저 로그인 ----------------
def _find_cred(ctx, provider):
    """브라우저 쿠키에서 로그인 정보 추출. 로그인 전이면 None."""
    try:
        cookies = ctx.cookies()
    except Exception:
        return None
    if provider == "gpt":
        jar = {c["name"]: c["value"] for c in cookies
               if "chatgpt.com" in c.get("domain", "")}
        return {"cookies": jar} if gpt_has_session(jar) else None
    for c in cookies:
        if c.get("name") == "sessionKey" and "claude.ai" in c.get("domain", ""):
            return {"session_key": c.get("value")}
    return None


def _launch_ctx(p, headless, profile_dir):
    """지정 채널로 브라우저 실행. 실패하면 Edge → Chrome → 내장 크로미움 순 폴백.
    profile_dir은 이 앱 전용 폴더라서, 평소 쓰는 Edge/Chrome 프로필과는 완전히 분리됨."""
    channels = [BROWSER_CHANNEL] + [c for c in ("msedge", "chrome", None)
                                    if c != BROWSER_CHANNEL]
    last_err = None
    for ch in channels:
        try:
            kwargs = {"headless": headless, "channel": ch}
            if not headless:
                kwargs["args"] = ["--disable-blink-features=AutomationControlled"]
            return p.chromium.launch_persistent_context(profile_dir, **kwargs)
        except Exception as e:
            last_err = e
    raise last_err


def read_cred_from_profile(profile_dir, provider):
    """저장된 브라우저 프로필에서 조용히(headless) 세션 복원 시도."""
    if not os.path.isdir(profile_dir):
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            ctx = _launch_ctx(p, headless=True, profile_dir=profile_dir)
            cred = _find_cred(ctx, provider)
            ctx.close()
            return cred
    except Exception:
        return None


def interactive_login(profile_dir, provider="claude", timeout_sec=600):
    """브라우저 창을 띄워 사용자가 직접 로그인. 완료되면 (로그인정보 dict, None) 반환."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "Playwright 미설치 → pip install playwright && playwright install chromium"
    try:
        with sync_playwright() as p:
            ctx = _launch_ctx(p, headless=False, profile_dir=profile_dir)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(PROVIDERS[provider]["login_url"], timeout=60000)
            except Exception:
                pass
            cred, t0 = None, time.time()
            while time.time() - t0 < timeout_sec:
                cred = _find_cred(ctx, provider)
                if cred:
                    break
                if not ctx.pages:  # 사용자가 창을 닫음
                    return None, "브라우저가 닫혔어요 (로그인 미완료)"
                time.sleep(1)
            if cred and provider == "gpt":
                time.sleep(2)   # 로그인 직후 나머지 쿠키(cf_clearance 등)가 붙을 때까지
                cred = _find_cred(ctx, provider) or cred
                try:
                    # Cloudflare 쿠키는 브라우저 UA와 묶여 있어 같은 UA로 요청해야 함
                    cred["user_agent"] = page.evaluate("navigator.userAgent")
                except Exception:
                    pass
            ctx.close()
            return (cred, None) if cred else (None, "로그인 시간 초과")
    except Exception as e:
        return None, f"브라우저 실행 실패: {e}"


def try_silent_restore(cfg, name):
    """프로필 폴더에서 새 로그인 정보를 조용히 복원. 성공 시 True."""
    acc = cfg["accounts"].get(name)
    if acc is None:
        return False
    cred = read_cred_from_profile(profile_dir_for(name), provider_of(acc))
    if not cred:
        return False
    candidate = dict(acc)
    candidate.update(cred)
    if account_alive(candidate):
        acc.update(candidate)   # org_uuid(조직 선택)·user_agent는 유지
        save_config(cfg)
        return True
    return False


# ---------------- 진단 모드 ----------------
def dump_mode():
    cfg = load_config()
    name = cfg.get("active_account") or "계정1"
    if name not in cfg["accounts"]:
        cfg["accounts"][name] = {"provider": "claude", "session_key": "", "org_uuid": ""}
        cfg["active_account"] = name
    acc = cfg["accounts"][name]
    provider = provider_of(acc)

    if not account_alive(acc):
        if not try_silent_restore(cfg, name):
            cred, err = interactive_login(profile_dir_for(name), provider)
            if not cred:
                print(f"[로그인 실패] {err}")
                return
            acc.update(cred)
            acc["org_uuid"] = ""
            save_config(cfg)
    print(f"[계정] {name} ({PROVIDERS[provider]['label']})")

    if provider == "gpt":
        print(f"\n[1] GET {GPT_USAGE_URL}")
        status, data = gpt_usage_request(acc)
        if status == 403:
            print("    403 → 브라우저 경유 재시도")
            status, data = gpt_fetch_via_browser(profile_dir_for(name))
        save_config(cfg)
        print(f"    HTTP {status}")
        text = json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, (dict, list)) else str(data)
        print(text[:3000])
        if status == 200 and isinstance(data, dict):
            print("\n    → 파싱 결과:", parse_gpt_usage(data))
        return

    print("\n[1] /api/organizations")
    status, data = http_get(f"{BASE}/api/organizations", acc["session_key"])
    print(f"    HTTP {status}")
    if isinstance(data, list):
        for o in data:
            print(f"    - {o.get('name')}  ({o.get('uuid')})")
    org, err = get_org_uuid(cfg, name)
    print(f"[2] org = {org or err}")
    for url in usage_url_candidates(cfg, org or ""):
        print(f"\n[3] GET {url}")
        status, data = http_get(url, acc["session_key"])
        print(f"    HTTP {status}")
        text = json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, (dict, list)) else str(data)
        print(text[:3000])
        if status == 200 and isinstance(data, (dict, list)):
            print("\n    → 파싱 결과:", parse_usage(data))
    print("\n모두 실패하면: claude.ai 설정→사용량 화면을 연 상태에서 F12→Network에서 'usage' 검색,")
    print("Request URL을 config JSON의 \"usage_url\"에 붙여넣으세요.")


# ---------------- UI ----------------
def round_rect(cv, x1, y1, x2, y2, r, **kw):
    """캔버스에 둥근 사각형(부드러운 다각형). r을 높이의 절반으로 주면 알약 모양."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1,
           x2, y1 + r, x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2,
           x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2, x1, y2,
           x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]
    kw.setdefault("outline", "")
    return cv.create_polygon(pts, smooth=True, **kw)


def mouse_button_down():
    """왼쪽 마우스 버튼이 눌려 있는지 (크기 조절 드래그 추적용)."""
    if sys.platform == "win32":
        try:
            import ctypes
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
        except Exception:
            pass
    return False


class ThemeImages:
    """themes/<이름> 폴더의 단계별 이미지를 불러와 잔여량에 맞는 프레임을 제공.
    - 파일 정렬 순서대로 0%→100%(잔여량) 단계로 간주 (예: stage00~stage10).
    - 실제 표시 크기(가로폭 w, scale)에 맞춰 리사이즈해서 캐시.
    - remaining_ratio(잔여량 0~1)를 주면 가장 가까운 단계 이미지를 돌려줌.
    Pillow(PIL)가 없으면 조용히 비활성(기본 막대로 폴백)."""

    def __init__(self):
        self._frames = []        # 원본 PIL 이미지들 (0%→100% 순)
        self._cache = {}         # (단계index, 표시폭) -> PhotoImage
        self._ok = False
        self.name = ""

    def load(self, theme_name):
        self._frames = []
        self._cache = {}
        self._ok = False
        self.name = theme_name or ""
        if not theme_name:
            return False
        try:
            from PIL import Image
        except Exception:
            return False
        folder = os.path.join(themes_root(), theme_name)
        if not os.path.isdir(folder):
            return False
        files = sorted(f for f in os.listdir(folder)
                       if f.lower().endswith((".png", ".gif")))
        if not files:
            return False
        for f in files:
            try:
                im = Image.open(os.path.join(folder, f)).convert("RGBA")
                self._frames.append(im)
            except Exception:
                pass
        self._ok = len(self._frames) > 0
        return self._ok

    @property
    def ok(self):
        return self._ok and len(self._frames) > 0

    def photo_for(self, remaining_ratio, disp_w):
        """잔여량(0~1)에 맞는 단계 이미지를 disp_w 폭의 PhotoImage로 반환."""
        if not self.ok:
            return None
        n = len(self._frames)
        idx = int(round(max(0.0, min(1.0, remaining_ratio)) * (n - 1)))
        key = (idx, int(disp_w))
        if key in self._cache:
            return self._cache[key]
        try:
            from PIL import Image, ImageTk
            src = self._frames[idx]
            ratio = disp_w / src.width
            disp_h = max(1, int(src.height * ratio))
            im = src.resize((int(disp_w), disp_h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(im)
            self._cache[key] = photo
            return photo
        except Exception:
            return None


class Pill:
    """알약 모양 버튼/탭/배지 (Canvas). 선택적으로 왼쪽에 색 점."""

    def __init__(self, app, parent, text, bg, fill=None, fg=FG, font=None,
                 padx=8, pady=2, dot=None, hover_fill=None):
        import tkinter.font as tkfont
        self.app, self.text, self.fill, self.fg, self.dot = app, text, fill, fg, dot
        self.hover_fill = hover_fill
        self.font = font or app.fnt(8)
        f = tkfont.Font(root=app.root, font=self.font)
        self.padx = app.px(padx)
        self.dotw = app.px(11) if dot else 0
        self.w = f.measure(text) + 2 * self.padx + self.dotw
        self.h = f.metrics("linespace") + 2 * app.px(pady)
        self.cv = app.tk.Canvas(parent, width=self.w, height=self.h, bg=bg,
                                highlightthickness=0, bd=0, cursor="hand2")
        self._hover = False
        if hover_fill:
            self.cv.bind("<Enter>", lambda e: self._set_hover(True), add="+")
            self.cv.bind("<Leave>", lambda e: self._set_hover(False), add="+")
        self.draw()

    def _set_hover(self, v):
        self._hover = v
        self.draw()

    def draw(self):
        cv, w, h = self.cv, self.w, self.h
        cv.delete("all")
        fill = self.hover_fill if (self._hover and self.hover_fill) else self.fill
        if fill:
            round_rect(cv, 0, 0, w - 1, h - 1, h / 2, fill=fill)
        x = self.padx
        if self.dot:
            r = self.app.px(3)
            cv.create_oval(x, h / 2 - r, x + 2 * r, h / 2 + r, fill=self.dot, outline="")
            x += self.dotw
        cv.create_text(x + (w - self.padx - x) / 2, h / 2, text=self.text,
                       fill=self.fg, font=self.font)

    def bind(self, seq, fn):
        self.cv.bind(seq, fn, add="+")

    def pack(self, **kw):
        self.cv.pack(**kw)


class LiveSticker:
    def __init__(self, cfg):
        import tkinter as tk
        self.tk = tk
        self.cfg = cfg
        self.usage = {}        # 계정이름 -> parse_usage 결과
        self.acc_status = {}   # 계정이름 -> 상태 문구
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()   # 갱신 루프 중복 방지
        self._logging_in = False
        self._menu_win = None
        self._dragging = False
        self._drag_ox = self._drag_oy = 0
        self._drag_active = False
        self._tip_win = None
        self._resize = None
        self._wheel_save = None

        try:
            self.scale = float(cfg.get("scale", 1.0)) or 1.0
        except Exception:
            self.scale = 1.0
        self.scale = max(SCALE_MIN, min(SCALE_MAX, self.scale))
        self._minimized = False

        self.theme = ThemeImages()
        self.theme.load(cfg.get("theme", ""))

        # 작업표시줄이 파이썬 아이콘 대신 우리 아이콘을 쓰도록 앱 ID 등록
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "ai.usage.sticker")
            except Exception:
                pass

        self.root = tk.Tk()
        self.root.title("AI 사용량")          # 축소 시 작업표시줄에 표시될 이름
        self._set_icon()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", ALPHA)
        self._make_transparent(self.root)
        self.root.geometry(f"+{cfg['win_x']}+{cfg['win_y']}")   # 크기는 내용에 맞춰 자동
        self.root.bind("<Map>", self._on_map)
        self.root.bind_all("<Control-MouseWheel>", self._on_ctrl_wheel)

        self.build_ui()
        self.rebuild_tabs()
        self.rebuild_menu()
        threading.Thread(target=self.startup_worker, daemon=True).start()
        self.tick()
        self.root.mainloop()

    # ----- 아이콘 -----
    def _set_icon(self):
        """작업표시줄/제목표시줄 아이콘 설정.
        iconbitmap(윈도우 네이티브 .ico 처리)과 iconphoto(여러 해상도를 직접 제공)를
        함께 사용 — 한쪽 방식이 특정 환경에서 흐리게 나오더라도 다른 쪽이 보완하도록 함."""
        ico = resource_path(ICON_NAME)
        if not os.path.exists(ico):
            return
        try:
            self.root.iconbitmap(default=ico)
        except Exception:
            pass
        try:
            from PIL import Image, ImageTk
            photos = []
            for sz in (256, 128, 64, 48, 32, 24, 16):
                img = Image.open(ico)   # .copy() 금지: IcoImageFile 프레임 선택에 필요
                img.size = (sz, sz)
                img.load()
                photos.append(ImageTk.PhotoImage(img))
            self._icon_imgs = photos    # GC 방지용으로 보관
            self.root.iconphoto(True, *photos)
        except Exception:
            pass

    # ----- 크기/폰트 헬퍼 -----
    def px(self, v):
        return max(1, int(round(v * self.scale)))

    def fnt(self, size, bold=False):
        s = max(7, int(round(size * self.scale)))
        return (UI_FONT, s, "bold") if bold else (UI_FONT, s)

    def num(self, size):
        return (NUM_FONT, max(7, int(round(size * self.scale))))

    def _prov(self, name=None):
        name = self.cfg.get("active_account", "") if name is None else name
        return PROVIDERS[provider_of(self.cfg["accounts"].get(name))]

    def set_scale(self, s, save=True):
        s = round(max(SCALE_MIN, min(SCALE_MAX, s)) * 20) / 20   # 5% 단위
        if abs(s - self.scale) < 0.001:
            return
        self.scale = s
        self.cfg["scale"] = s
        if save:
            save_config(self.cfg)
        self._rebuild_all()

    def set_theme(self, theme_name):
        """테마 전환. ""=기본 막대. UI를 다시 그려 게이지 표현을 바꿈."""
        ok = self.theme.load(theme_name)
        if theme_name and not ok:
            self.set_status_now(f"테마 '{theme_name}' 불러오기 실패 (Pillow 필요/폴더 확인)")
            self.cfg["theme"] = ""
        else:
            self.cfg["theme"] = theme_name
        save_config(self.cfg)
        self._rebuild_all()

    def _rebuild_all(self):
        self._hide_tip()
        for w in self.root.winfo_children():
            if not isinstance(w, self.tk.Toplevel):
                w.destroy()
        self.build_ui()
        self.rebuild_tabs()
        self.rebuild_menu()
        self.render()

    # ----- 크기 조절: 우하단 그립 드래그 / Ctrl+휠 -----
    def _start_resize(self, e):
        self._resize = {"x0": e.x_root, "y0": e.y_root, "s0": self.scale,
                        "w0": max(1, self.root.winfo_width())}
        self._resize_poll()

    def _resize_poll(self):
        """그립 위젯은 크기 변경 때마다 다시 만들어져 드래그 이벤트가 끊기므로,
        마우스 위치/버튼 상태를 직접 읽어서 추적."""
        st = self._resize
        if not st:
            return
        mx, my = self.root.winfo_pointerxy()
        d = ((mx - st["x0"]) + (my - st["y0"])) / 2
        self.set_scale(st["s0"] * (st["w0"] + d) / st["w0"], save=False)
        if mouse_button_down():
            self.root.after(30, self._resize_poll)
        else:
            self._resize = None
            save_config(self.cfg)

    def _on_ctrl_wheel(self, e):
        self.set_scale(self.scale + (0.05 if e.delta > 0 else -0.05), save=False)
        if self._wheel_save:
            self.root.after_cancel(self._wheel_save)
        self._wheel_save = self.root.after(600, lambda: save_config(self.cfg))

    # ----- 축소(작업표시줄) / 복귀 -----
    def minimize(self):
        """테두리를 잠깐 되살려 작업표시줄 아이콘으로 축소. 아이콘 클릭 시 복귀."""
        self._minimized = True
        self.root.overrideredirect(False)
        self.root.iconify()

    def _on_map(self, e=None):
        if e is not None and e.widget is not self.root:
            return
        if self._minimized and self.root.state() == "normal":
            self._minimized = False
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
            self.root.attributes("-alpha", ALPHA)

    # ----- 둥근 창 껍데기 -----
    @staticmethod
    def _make_transparent(win):
        win.configure(bg=KEY)
        try:
            win.attributes("-transparentcolor", KEY)   # 둥근 모서리 바깥을 투명하게
        except Exception:
            pass

    def _round_shell(self, win, radius, fill, pad):
        """win 안에 둥근 카드(Canvas)를 깔고, 내용을 담을 Frame을 돌려줌.
        내용 크기가 바뀌면 카드도 자동으로 다시 그림. win._refresh()로 즉시 갱신 가능."""
        tk = self.tk
        cv = tk.Canvas(win, bg=KEY, highlightthickness=0, bd=0)
        cv.pack()
        body = tk.Frame(cv, bg=fill)
        p, r = self.px(pad), self.px(radius)
        cv.create_window(p, p, window=body, anchor="nw")
        last = {"size": None}

        def refresh(e=None):
            w = body.winfo_reqwidth() + 2 * p
            h = body.winfo_reqheight() + 2 * p
            if last["size"] == (w, h):
                return
            last["size"] = (w, h)
            cv.config(width=w, height=h)
            cv.delete("shape")
            round_rect(cv, 0, 0, w - 1, h - 1, r, fill=fill, outline=BORDER, tags="shape")
            cv.tag_lower("shape")

        body.bind("<Configure>", refresh)
        win._refresh = refresh
        return cv, body

    def _popup(self, radius=14, pad=8):
        win = self.tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        self._make_transparent(win)
        _, body = self._round_shell(win, radius, MENU_BG, pad)
        return win, body

    # ----- UI 구성 -----
    def build_ui(self):
        tk = self.tk
        self.shell, self.body = self._round_shell(self.root, 18, BG, 10)
        body = self.body
        self.make_draggable(self.shell)
        self.make_draggable(body)

        head = tk.Frame(body, bg=BG)
        head.pack(fill="x", padx=self.px(4), pady=(self.px(2), 0))
        title_text = "AI 사용량" + (f" · {PROFILE_LABEL}" if PROFILE_LABEL else "")
        title = tk.Label(head, text=title_text, bg=BG, fg=FG, font=self.fnt(9, True))
        title.pack(side="left")
        self.prov_lbl = tk.Label(head, text="", bg=BG, fg=SUB, font=self.fnt(8, True))
        self.prov_lbl.pack(side="left", padx=(self.px(6), 0))

        # 우상단 ✕(종료) / –(작업표시줄로 축소) — 동그란 hover 버튼
        for glyph, cmd, hov in (("✕", self.quit, DANGER), ("–", self.minimize, SEG_ON)):
            b = Pill(self, head, glyph, bg=BG, fg=SUB, font=self.fnt(8, True),
                     padx=5, pady=1, hover_fill=hov)
            b.pack(side="right", padx=(self.px(2), 0))
            b.bind("<Button-1>", lambda e, c=cmd: c())

        self.make_draggable(head)
        self.make_draggable(title)
        self.make_draggable(self.prov_lbl)

        # 계정 탭 바
        self.tabbar = tk.Frame(body, bg=BG)
        self.tabbar.pack(fill="x", padx=self.px(2), pady=(self.px(6), self.px(6)))
        self.make_draggable(self.tabbar)

        self.rows = {}
        if self.theme.ok:
            self._build_tree_row()
        else:
            self._build_bar_rows()

        foot = tk.Frame(body, bg=BG)
        foot.pack(fill="x", padx=self.px(4))
        self.status_lbl = tk.Label(foot, text="시작 중…", bg=BG, fg=SUB, font=self.fnt(8),
                                   anchor="w", justify="left", wraplength=self.px(205))
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self.make_draggable(foot)
        self.make_draggable(self.status_lbl)

        # 우하단 크기 조절 그립
        g = self.px(12)
        grip = tk.Canvas(foot, width=g, height=g, bg=BG, highlightthickness=0,
                         cursor="size_nw_se")
        grip.pack(side="right", anchor="se")
        r = max(1, self.px(1.2))
        for cx, cy in ((g - 3 * r, g - 3 * r), (g - 3 * r, g - 7 * r), (g - 7 * r, g - 3 * r)):
            grip.create_oval(cx - r, cy - r, cx + r, cy + r, fill=SUB, outline="")
        grip.bind("<Button-1>", self._start_resize)
        grip.bind("<Button-3>", lambda e: self.show_menu(e.x_root, e.y_root))

    def _build_bar_rows(self):
        """기본 막대 게이지 (세로로 세션/주간 2줄)."""
        tk = self.tk
        for key in ROW_KEYS:
            line = tk.Frame(self.body, bg=BG)
            line.pack(fill="x", padx=self.px(4))
            name_lbl = tk.Label(line, text="", bg=BG, fg=SUB, font=self.fnt(8))
            name_lbl.pack(side="left")
            pct_lbl = tk.Label(line, text="--%", bg=BG, fg=FG, font=self.num(9))
            pct_lbl.pack(side="right")
            bar = tk.Canvas(self.body, width=self.px(232), height=self.px(10),
                            bg=BG, highlightthickness=0)
            bar.pack(padx=self.px(4), pady=(self.px(1), self.px(7)))
            for w in (line, name_lbl, pct_lbl):
                self.make_draggable(w)
            self.make_draggable(bar)
            self.rows[key] = {"name": name_lbl, "pct": pct_lbl, "bar": bar}

    def _build_tree_row(self):
        """나무 테마: 세션/주간 나무를 가로로 나란히. 이름/퍼센트는 툴팁으로."""
        tk = self.tk
        tree_h = self._tree_height()
        tree_w = self._tree_width()
        holder = tk.Frame(self.body, bg=BG)
        holder.pack(pady=(self.px(2), self.px(6)))
        self.make_draggable(holder)
        for key in ROW_KEYS:
            cv = tk.Canvas(holder, width=tree_w, height=tree_h,
                           bg=BG, highlightthickness=0)
            cv.pack(side="left", padx=self.px(6))
            self.make_draggable(cv)
            self._attach_tooltip(cv, key)
            self.rows[key] = {"bar": cv}

    def _tree_width(self):
        if self.theme.ok and self.theme._frames:
            # 두 그루가 위젯 폭(약 232) 안에 나란히 들어가도록
            return self.px(int(232 / 2) - 12)
        return self.px(96)

    def _tree_height(self):
        if self.theme.ok and self.theme._frames:
            src = self.theme._frames[0]
            return max(1, int(src.height * (self._tree_width() / src.width)))
        return self.px(104)

    # ----- 툴팁 -----
    def _attach_tooltip(self, widget, key):
        widget.bind("<Enter>", lambda e, k=key: self._show_tip(e, k), add="+")
        widget.bind("<Leave>", lambda e: self._hide_tip(), add="+")

    def _show_tip(self, e, key):
        self._hide_tip()
        win, body = self._popup(radius=10, pad=4)
        self.tk.Label(body, text=self._tip_text(key), bg=MENU_BG, fg=FG, font=self.fnt(8),
                      padx=self.px(6), pady=self.px(2), justify="left").pack()
        self.place_popup(win, e.x_root + 12, e.y_root + 12)
        self._tip_win = win

    def _hide_tip(self):
        win = getattr(self, "_tip_win", None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
            self._tip_win = None

    def _tip_text(self, key):
        """툴팁에 표시할 '이름 + 퍼센트 + 리셋' 문자열."""
        active = self.cfg.get("active_account", "")
        with self._lock:
            usage = dict(self.usage.get(active) or {})
        info = usage.get(key)
        name = self._prov()["rows"].get(key, key)
        if not info:
            return f"{name}: --"
        txt = f"{name}: {info['ratio']*100:.0f}% 사용"
        if info.get("detail"):
            txt += f" ({info['detail']})"
        if info.get("resets_at"):
            remain = int((info["resets_at"] - datetime.now(timezone.utc)).total_seconds())
            if remain > 0:
                d, rem = divmod(remain, 86400)
                h, mnt = rem // 3600, (rem % 3600) // 60
                txt += f"\n리셋까지 {d}일 {h}시간" if d > 0 else f"\n리셋까지 {h}:{mnt:02d}"
        return txt

    def rebuild_tabs(self):
        x, y = self.root.winfo_x(), self.root.winfo_y()
        for w in self.tabbar.winfo_children():
            w.destroy()
        for name in self.cfg["accounts"]:
            active = (name == self.cfg.get("active_account"))
            tab = Pill(self, self.tabbar, name, bg=BG,
                       fill=SEG_ON if active else None,
                       fg=FG if active else SUB, font=self.fnt(8, active),
                       padx=8, pady=2, dot=self._prov(name)["color"],
                       hover_fill=SEG_ON if active else HOVER)
            tab.pack(side="left", padx=(0, self.px(4)))
            tab.bind("<Button-1>", lambda e, n=name: self.switch_account(n))
            tab.bind("<Double-Button-1>", lambda e, n=name: self.rename_account(n))
            tab.bind("<Button-3>", lambda e: self.show_menu(e.x_root, e.y_root))
        plus = Pill(self, self.tabbar, "＋", bg=BG, fg=SUB, font=self.fnt(8, True),
                    padx=6, pady=2, hover_fill=HOVER)
        plus.pack(side="left")
        plus.bind("<Button-1>", lambda e: self.add_account())
        plus.bind("<Button-3>", lambda e: self.show_menu(e.x_root, e.y_root))
        self._pin_geometry(x, y)

    def _pin_geometry(self, x, y):
        """탭 전환/계정 추가삭제/크기변경 등으로 내용 폭이 바뀌어도
        창 위치(좌상단)는 그대로 두고 크기만 새로 맞춤.
        단, 사용자가 지금 드래그로 창을 옮기는 중이면 건드리지 않음
        (드래그 도중 매초 갱신이 끼어들어 창이 튀던 문제 방지)."""
        if getattr(self, "_dragging", False):
            return
        self.root.update_idletasks()
        self.root._refresh()
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_reqheight()
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def rebuild_menu(self):
        # 커스텀 메뉴는 열 때마다 새로 그리므로, 열려 있는 메뉴만 닫음
        self._close_menu()

    # ----- 팝업 위치 -----
    def _monitor_workarea(self, x, y):
        """(x, y) 지점이 속한 모니터의 작업 영역 (left, top, right, bottom).
        듀얼/멀티 모니터 대응. 실패 시 주 모니터 기준으로 폴백."""
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                MonitorFromPoint = user32.MonitorFromPoint
                MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
                MonitorFromPoint.restype = ctypes.c_void_p

                class MONITORINFO(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.DWORD),
                                ("rcMonitor", wintypes.RECT),
                                ("rcWork", wintypes.RECT),
                                ("dwFlags", wintypes.DWORD)]

                hmon = MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)  # NEAREST
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    r = mi.rcWork          # 작업표시줄 제외한 실제 사용 영역
                    return r.left, r.top, r.right, r.bottom
            except Exception:
                pass
        return (0, 0, self.root.winfo_screenwidth(),
                self.root.winfo_screenheight() - 48)

    def place_popup(self, win, x, y):
        """팝업이 '해당 모니터' 밖으로 나가지 않게 위치 보정 (멀티 모니터 대응)."""
        win.update_idletasks()
        if hasattr(win, "_refresh"):
            win._refresh()
            win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        left, top, right, bottom = self._monitor_workarea(x, y)
        x = max(left, min(x, right - w - 4))
        y = max(top, min(y, bottom - h - 4))
        win.geometry(f"+{x}+{y}")

    def _below_sticker(self):
        return self.root.winfo_x(), self.root.winfo_y() + self.root.winfo_height() + 6

    # ----- 메뉴 부품 -----
    def _close_menu(self):
        win = getattr(self, "_menu_win", None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
            self._menu_win = None

    def _menu_row(self, parent, width, text, cmd, icon="", fg=FG, hint="",
                  dot=None, bold=False):
        """메뉴 한 줄: 왼쪽 아이콘(또는 색 점) · 글자 · 오른쪽 보조 문구.
        마우스를 올리면 둥근 하이라이트."""
        tk = self.tk
        h = self.px(30)
        cv = tk.Canvas(parent, width=width, height=h, bg=MENU_BG,
                       highlightthickness=0, cursor="hand2")
        cv.pack()

        def draw(hover=False):
            cv.delete("all")
            if hover:
                round_rect(cv, 0, 1, width - 1, h - 2, self.px(8), fill=HOVER)
            cx = self.px(16)
            if dot:
                r = self.px(4)
                cv.create_oval(cx - r, h / 2 - r, cx + r, h / 2 + r, fill=dot, outline="")
            elif icon:
                cv.create_text(cx, h / 2, text=icon, fill=fg if fg != FG else SUB,
                               font=self.fnt(9))
            cv.create_text(self.px(32), h / 2, text=text, anchor="w", fill=fg,
                           font=self.fnt(9, bold))
            if hint:
                cv.create_text(width - self.px(12), h / 2, text=hint, anchor="e",
                               fill=SUB, font=self.fnt(8))

        draw()
        cv.bind("<Enter>", lambda e: draw(True))
        cv.bind("<Leave>", lambda e: draw(False))
        cv.bind("<Button-1>", lambda e: cmd())
        return cv

    def _menu_section(self, parent, text, hint=""):
        tk = self.tk
        row = tk.Frame(parent, bg=MENU_BG)
        row.pack(fill="x", padx=self.px(10), pady=(self.px(8), self.px(2)))
        tk.Label(row, text=text, bg=MENU_BG, fg=SUB, font=self.fnt(7, True)).pack(side="left")
        if hint:
            tk.Label(row, text=hint, bg=MENU_BG, fg=SUB, font=self.fnt(7)).pack(side="right")

    def _menu_sep(self, parent):
        self.tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=self.px(8),
                                                        pady=self.px(5))

    def _segmented(self, parent, width, options, current, on_pick, colors=None, bg=MENU_BG):
        """세그먼트 선택 컨트롤 (iOS/Win11 스타일). options = [(표시, 값), ...]."""
        tk = self.tk
        h, n = self.px(28), max(1, len(options))
        cv = tk.Canvas(parent, width=width, height=h, bg=bg,
                       highlightthickness=0, cursor="hand2")
        state = {"cur": current}

        def draw():
            cv.delete("all")
            round_rect(cv, 0, 0, width - 1, h - 1, self.px(9), fill=SEG_BG)
            sw = width / n
            for i, (label, val) in enumerate(options):
                on = (val == state["cur"])
                x1 = i * sw
                if on:
                    round_rect(cv, x1 + 3, 3, x1 + sw - 3, h - 4, self.px(7),
                               fill=(colors or {}).get(val, SEG_ON))
                cv.create_text(x1 + sw / 2, h / 2, text=label,
                               fill=FG if on else SUB, font=self.fnt(8, on))

        def click(e):
            i = min(n - 1, max(0, int(e.x // (width / n))))
            state["cur"] = options[i][1]
            draw()
            on_pick(state["cur"])

        cv.bind("<Button-1>", click)
        draw()
        return cv

    def _button_row(self, parent, specs):
        """둥근 알약 버튼들 (오른쪽 정렬). specs = [(글자, 콜백, 채움색), ...]."""
        row = self.tk.Frame(parent, bg=MENU_BG)
        row.pack(fill="x", padx=self.px(10), pady=(self.px(6), self.px(6)))
        for text, cmd, color in reversed(specs):
            primary = color not in (SEG_BG, SEG_ON)
            b = Pill(self, row, text, bg=MENU_BG, fill=color,
                     fg="white" if primary else FG, font=self.fnt(9, primary),
                     padx=14, pady=4, hover_fill=SEG_ON if not primary else None)
            b.pack(side="right", padx=(self.px(6), 0))
            b.bind("<Button-1>", cmd)

    def _entry(self, parent, width, initial=""):
        """둥근 입력칸. 포커스 시 테두리가 강조색으로."""
        tk = self.tk
        h = self.px(32)
        cv = tk.Canvas(parent, width=width, height=h, bg=MENU_BG, highlightthickness=0)
        cv.pack(padx=self.px(10), pady=(self.px(2), self.px(4)))
        frame = round_rect(cv, 0, 0, width - 1, h - 1, self.px(9), fill=FIELD, outline=BORDER)
        ent = tk.Entry(cv, bg=FIELD, fg=FG, insertbackground=FG, relief="flat", bd=0,
                       highlightthickness=0, font=self.fnt(9))
        cv.create_window(self.px(12), h // 2, window=ent, anchor="w",
                         width=width - self.px(24))
        ent.insert(0, initial)
        ent.select_range(0, "end")
        ent.bind("<FocusIn>", lambda e: cv.itemconfig(frame, outline=self._prov()["color"]))
        ent.bind("<FocusOut>", lambda e: cv.itemconfig(frame, outline=BORDER))
        return ent

    def _dialog_title(self, body, title, sub=""):
        tk = self.tk
        tk.Label(body, text=title, bg=MENU_BG, fg=FG, font=self.fnt(10, True), anchor="w"
                 ).pack(fill="x", padx=self.px(10), pady=(self.px(6), 0))
        if sub:
            tk.Label(body, text=sub, bg=MENU_BG, fg=SUB, font=self.fnt(8), anchor="w",
                     justify="left", wraplength=self.px(240)
                     ).pack(fill="x", padx=self.px(10), pady=(self.px(2), self.px(4)))

    # ----- 우클릭 메뉴 -----
    def show_menu(self, x, y):
        """둥근 카드형 우클릭 메뉴."""
        import tkinter.font as tkfont
        self._close_menu()
        self._hide_tip()
        active = self.cfg.get("active_account", "")
        win, body = self._popup(radius=14, pad=6)
        self._menu_win = win

        def act(cmd):
            return lambda: (self._close_menu(), cmd())

        # 메뉴 폭: 가장 긴 항목 기준
        f = tkfont.Font(root=self.root, font=self.fnt(9))
        texts = ["계정 추가…", "작업표시줄로 축소"] + [n + "  ChatGPT ✓" for n in self.cfg["accounts"]]
        if active:
            texts.append(f"'{active}' 조직 선택…")
        W = max(self.px(232), max(f.measure(t) for t in texts) + self.px(60))

        self._menu_row(body, W, "지금 새로고침", act(self.schedule_fetch), icon="↻")
        if self.cfg["accounts"]:
            self._menu_section(body, "계정")
            for n in self.cfg["accounts"]:
                hint = self._prov(n)["label"] + ("  ✓" if n == active else "")
                self._menu_row(body, W, n, act(lambda n=n: self.switch_account(n)),
                               dot=self._prov(n)["color"], hint=hint, bold=(n == active))
        self._menu_row(body, W, "계정 추가…", act(self.add_account), icon="＋")
        if active:
            self._menu_sep(body)
            self._menu_row(body, W, "이름 변경…", act(lambda: self.rename_account(active)),
                           icon="✎", hint=active)
            if provider_of(self.cfg["accounts"].get(active)) == "claude":
                self._menu_row(body, W, "조직 선택…",
                               act(lambda: self.maybe_choose_org(active, force=True)),
                               icon="⇄", hint="개인 / 팀")
            self._menu_row(body, W, "재로그인", act(self.relogin), icon="⟲")
            self._menu_row(body, W, "계정 삭제", act(self.remove_account), icon="✕", fg=DANGER)

        self._menu_section(body, "크기", f"{self.scale*100:.0f}%  ·  모서리 드래그 / Ctrl+휠")
        sizes = [(str(p), p / 100) for p in (85, 100, 125, 150, 175)]
        cur = next((v for _, v in sizes if abs(v - self.scale) < 0.01), None)
        seg = self._segmented(body, W - self.px(16), sizes, cur,
                              lambda v: (self._close_menu(), self.set_scale(v)))
        seg.pack(padx=self.px(8))

        themes = list_themes()
        self._menu_section(body, "테마", "" if themes else "themes 폴더 비어있음")
        opts = [("기본", "")] + [(t, t) for t in themes]
        seg = self._segmented(body, W - self.px(16), opts, self.cfg.get("theme", ""),
                              lambda v: (self._close_menu(), self.set_theme(v)))
        seg.pack(padx=self.px(8))

        self._menu_sep(body)
        self._menu_row(body, W, "작업표시줄로 축소", act(self.minimize), icon="–")
        self._menu_row(body, W, "종료", act(self.quit), icon="⏻", fg=DANGER)

        self.place_popup(win, x, y)
        win.after(10, win.focus_force)
        win.bind("<FocusOut>", lambda e: self._close_menu())
        win.bind("<Escape>", lambda e: self._close_menu())

    # ----- 대화상자 -----
    def _run_modal(self, win, ent=None):
        self.place_popup(win, *self._below_sticker())
        win.grab_set()
        if ent is not None:
            ent.focus_set()
        else:
            win.focus_force()
        self.root.wait_window(win)

    def ask_text(self, title, prompt, initial=""):
        """텍스트 입력 대화상자. 취소 시 None."""
        win, body = self._popup()
        self._dialog_title(body, title, prompt)
        ent = self._entry(body, self.px(240), initial)
        result = {"v": None}

        def ok(e=None):
            result["v"] = ent.get()
            win.destroy()

        def cancel(e=None):
            win.destroy()

        self._button_row(body, [("취소", cancel, SEG_BG), ("확인", ok, self._prov()["color"])])
        win.bind("<Return>", ok)
        win.bind("<Escape>", cancel)
        self._run_modal(win, ent)
        return result["v"]

    def ask_account(self):
        """계정 추가 대화상자: 서비스(Claude/ChatGPT) + 이름. 취소 시 None."""
        tk = self.tk
        win, body = self._popup()
        self._dialog_title(body, "계정 추가", "어떤 서비스의 사용량을 볼까요?")
        width = self.px(240)
        state = {"prov": "claude", "edited": False}

        def default_name(prov):
            return self._unique_name(PROVIDERS[prov]["label"])

        seg = self._segmented(body, width, [("Claude", "claude"), ("ChatGPT", "gpt")],
                              "claude", lambda v: pick(v),
                              colors={k: v["color"] for k, v in PROVIDERS.items()})
        seg.pack(padx=self.px(10), pady=(self.px(2), self.px(6)))
        note = tk.Label(body, text="", bg=MENU_BG, fg=SUB, font=self.fnt(7), anchor="w",
                        justify="left", wraplength=width)
        note.pack(fill="x", padx=self.px(10))
        tk.Label(body, text="이름", bg=MENU_BG, fg=SUB, font=self.fnt(7, True), anchor="w"
                 ).pack(fill="x", padx=self.px(10), pady=(self.px(6), 0))
        ent = self._entry(body, width, default_name("claude"))
        ent.bind("<Key>", lambda e: state.update(edited=True), add="+")
        result = {"v": None}

        def pick(prov):
            state["prov"] = prov
            note.config(text=("chatgpt.com 로그인 → Codex 5시간/주간 사용 한도를 표시해요"
                              if prov == "gpt" else
                              "claude.ai 로그인 → 5시간 세션/주간 사용량을 표시해요"))
            if not state["edited"]:
                ent.delete(0, "end")
                ent.insert(0, default_name(prov))
                ent.select_range(0, "end")

        def ok(e=None):
            result["v"] = (ent.get(), state["prov"])
            win.destroy()

        def cancel(e=None):
            win.destroy()

        pick("claude")
        self._button_row(body, [("취소", cancel, SEG_BG), ("로그인", ok, "#5b6cff")])
        win.bind("<Return>", ok)
        win.bind("<Escape>", cancel)
        self._run_modal(win, ent)
        return result["v"]

    def ask_confirm(self, title, prompt):
        """삭제 확인 대화상자."""
        win, body = self._popup()
        self._dialog_title(body, title, prompt)
        result = {"v": False}

        def yes(e=None):
            result["v"] = True
            win.destroy()

        def no(e=None):
            win.destroy()

        self._button_row(body, [("취소", no, SEG_BG), ("삭제", yes, DANGER)])
        win.bind("<Return>", yes)
        win.bind("<Escape>", no)
        self._run_modal(win)
        return result["v"]

    def make_draggable(self, w):
        """드래그로 창 이동. 위젯마다 독립된 시작좌표를 쓰고(공유 _dx/_dy로 인한
        좌표 점프 방지), 3px 미만 미세 이동은 클릭으로 간주해 무시(탭을 누를 때
        손이 살짝 흔들려도 창이 안 움직이도록)."""
        def start(e):
            # 창 좌상단 대비 마우스 위치(스크린 좌표)를 기준점으로 저장
            self._drag_ox = e.x_root - self.root.winfo_x()
            self._drag_oy = e.y_root - self.root.winfo_y()
            self._drag_active = False

        def drag(e):
            nx = e.x_root - self._drag_ox
            ny = e.y_root - self._drag_oy
            if not self._drag_active:
                # 시작점에서 충분히 벗어나야 실제 드래그로 인정
                if abs(nx - self.root.winfo_x()) < 3 and abs(ny - self.root.winfo_y()) < 3:
                    return
                self._drag_active = True
            self._dragging = True   # 드래그 중엔 render의 위치 고정을 잠시 멈춤
            self.root.geometry(f"+{nx}+{ny}")

        def end(e):
            if getattr(self, "_dragging", False):
                self._dragging = False
                self.cfg["win_x"] = self.root.winfo_x()
                self.cfg["win_y"] = self.root.winfo_y()
                save_config(self.cfg)

        w.bind("<Button-1>", start, add="+")
        w.bind("<B1-Motion>", drag, add="+")
        w.bind("<ButtonRelease-1>", end, add="+")
        w.bind("<Button-3>", lambda e: self.show_menu(e.x_root, e.y_root))

    # ----- 계정 관리 -----
    def switch_account(self, name):
        if name not in self.cfg["accounts"]:
            return
        if name == self.cfg.get("active_account"):
            return   # 이미 활성 탭 → 재빌드 생략 (더블클릭 이름 변경 판정 보존)
        self.cfg["active_account"] = name
        save_config(self.cfg)
        self.rebuild_tabs()
        self.rebuild_menu()
        self.render()
        if name not in self.usage:   # 아직 데이터 없으면 즉시 조회
            self.schedule_fetch()

    def _unique_name(self, base):
        name, i = base, 2
        while name in self.cfg["accounts"]:
            name = f"{base}-{i}"
            i += 1
        return name

    def add_account(self):
        if self._logging_in:
            return
        res = self.ask_account()
        if res is None:
            return
        name, provider = res
        name = self._unique_name(name.strip() or PROVIDERS[provider]["label"])

        self._logging_in = True
        self.set_status_now(f"'{name}' 로그인: 브라우저 창에서 "
                            f"{PROVIDERS[provider]['label']}에 로그인해 주세요…")

        def worker():
            cred, err = interactive_login(profile_dir_for(name), provider)
            with self._lock:
                if cred:
                    acc = {"provider": provider}
                    acc.update(cred)
                    if provider == "claude":
                        acc["org_uuid"] = ""
                    self.cfg["accounts"][name] = acc
                    self.cfg["active_account"] = name
                    save_config(self.cfg)
                    self.acc_status[name] = "로그인 완료"
                else:
                    self.acc_status[self.cfg.get("active_account", "")] = err or "로그인 실패"
            self._logging_in = False
            self.root.after(0, self.rebuild_tabs)
            self.root.after(0, self.rebuild_menu)
            if provider == "claude":
                self.root.after(0, lambda: self.maybe_choose_org(name))
            self.schedule_fetch()

        threading.Thread(target=worker, daemon=True).start()

    def relogin(self):
        name = self.cfg.get("active_account")
        if not name or self._logging_in:
            return
        provider = provider_of(self.cfg["accounts"].get(name))
        self._logging_in = True
        self.set_status_now(f"'{name}' 재로그인: 브라우저 창에서 로그인해 주세요…")

        def worker():
            cred, err = interactive_login(profile_dir_for(name), provider)
            with self._lock:
                if cred:
                    acc = self.cfg["accounts"].get(name)
                    if acc is not None:
                        acc.update(cred)   # 조직 선택 유지(다른 계정이면 아래서 재확인)
                        save_config(self.cfg)
                    self.acc_status[name] = "로그인 완료"
                else:
                    self.acc_status[name] = err or "로그인 실패"
            self._logging_in = False
            if provider == "claude":
                self.root.after(0, lambda: self.maybe_choose_org(name))
            self.schedule_fetch()

        threading.Thread(target=worker, daemon=True).start()

    def remove_account(self):
        name = self.cfg.get("active_account")
        if not name:
            return
        if not self.ask_confirm("계정 삭제",
                                f"'{name}' 계정을 삭제할까요?\n저장된 로그인 프로필도 함께 삭제돼요."):
            return
        with self._lock:
            self.cfg["accounts"].pop(name, None)
            self.usage.pop(name, None)
            self.acc_status.pop(name, None)
            self.cfg["active_account"] = next(iter(self.cfg["accounts"]), "")
            save_config(self.cfg)
        threading.Thread(target=lambda: shutil.rmtree(profile_dir_for(name), ignore_errors=True),
                         daemon=True).start()
        self.rebuild_tabs()
        self.rebuild_menu()
        self.render()

    # ----- 계정 이름 변경 -----
    def rename_account(self, name=None):
        name = name or self.cfg.get("active_account")
        if not name or name not in self.cfg["accounts"] or self._logging_in:
            return
        new = self.ask_text("이름 변경", f"'{name}'의 새 이름", name)
        if not new:
            return
        new = new.strip()
        if not new or new == name:
            return
        if new in self.cfg["accounts"]:
            self.set_status_now(f"'{new}' 이름이 이미 있어요")
            return
        src, dst = profile_dir_for(name), profile_dir_for(new)
        if os.path.isdir(src):
            if os.path.exists(dst):
                self.set_status_now("프로필 폴더 이름 충돌 — 다른 이름을 써주세요")
                return
            try:
                os.rename(src, dst)   # 로그인 프로필 폴더도 함께 이동
            except Exception:
                self.set_status_now("프로필 폴더 이동 실패 — 잠시 후 다시 시도해 주세요")
                return
        with self._lock:
            self.cfg["accounts"] = {(new if k == name else k): v
                                    for k, v in self.cfg["accounts"].items()}
            if self.cfg.get("active_account") == name:
                self.cfg["active_account"] = new
            if name in self.usage:
                self.usage[new] = self.usage.pop(name)
            if name in self.acc_status:
                self.acc_status[new] = self.acc_status.pop(name)
            save_config(self.cfg)
        self.rebuild_tabs()
        self.rebuild_menu()
        self.render()

    # ----- 조직(개인/팀) 선택 — Claude 전용 -----
    def maybe_choose_org(self, name, force=False):
        """계정이 여러 조직(개인 플랜 + 팀 등)에 속하면 어느 쪽 사용량을 볼지 선택.
        팀에 초대된 계정이 개인 사용량만 보이던 문제의 해결 지점."""
        acc = self.cfg["accounts"].get(name)
        if not acc or not acc.get("session_key") or provider_of(acc) != "claude":
            return

        def worker():
            orgs, err = list_orgs(acc.get("session_key", ""))
            self.root.after(0, lambda: self._org_dialog(name, orgs, err, force))

        threading.Thread(target=worker, daemon=True).start()

    def _org_dialog(self, name, orgs, err, force):
        acc = self.cfg["accounts"].get(name)
        if acc is None:
            return
        if err or not orgs:
            if force:
                with self._lock:
                    self.acc_status[name] = err or "조직 목록을 못 가져왔어요"
            return
        cur = acc.get("org_uuid", "")
        uuids = [o["uuid"] for o in orgs]
        if cur and cur not in uuids:      # 다른 계정으로 재로그인한 경우 등
            acc["org_uuid"] = cur = ""
            save_config(self.cfg)
        if cur and not force:             # 이미 유효한 선택이 있음
            return
        if len(orgs) == 1 and not force:  # 선택지가 하나뿐 → 자동 확정
            acc["org_uuid"] = orgs[0]["uuid"]
            save_config(self.cfg)
            return

        win, body = self._popup()
        self._dialog_title(body, "플랜(조직) 선택", f"'{name}' — 어느 플랜의 사용량을 볼까요?")
        W = self.px(250)
        for o in orgs:
            def pick(u=o["uuid"], n=o["name"]):
                a = self.cfg["accounts"].get(name)
                if a is not None:
                    a["org_uuid"] = u
                    save_config(self.cfg)
                with self._lock:
                    self.usage.pop(name, None)
                    self.acc_status[name] = f"플랜 전환: {n}"
                win.destroy()
                self.schedule_fetch()

            self._menu_row(body, W, o["name"], pick, icon="●" if o["uuid"] == cur else "○",
                           bold=(o["uuid"] == cur))
        self._button_row(body, [("나중에", lambda e: win.destroy(), SEG_BG)])
        self.place_popup(win, *self._below_sticker())

    # ----- 데이터 -----
    def set_status_now(self, text):
        name = self.cfg.get("active_account", "")
        with self._lock:
            self.acc_status[name] = text

    def startup_worker(self):
        """시작 시: 계정이 없으면 계정 추가 창, 있으면 조용히 세션 점검 후 갱신."""
        if not self.cfg["accounts"]:
            with self._lock:
                self.acc_status[""] = "＋ 를 눌러 Claude / ChatGPT 계정을 추가하세요"
            self.root.after(300, self.add_account)
            return
        for name in list(self.cfg["accounts"]):
            acc = self.cfg["accounts"][name]
            if not account_alive(acc):
                try_silent_restore(self.cfg, name)
        self.schedule_fetch()

    def schedule_fetch(self):
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self):
        """모든 계정 사용량을 순서대로 갱신. (중복 실행 방지)"""
        if not self._fetch_lock.acquire(blocking=False):
            return
        try:
            for name in list(self.cfg["accounts"].keys()):
                try:
                    parsed, msg = fetch_usage(self.cfg, name)
                except Exception as e:
                    parsed, msg = None, f"오류: {e}"
                if parsed is None and msg == EXPIRED:
                    # 프로필 폴더에서 조용히 복원 후 1회 재시도
                    if try_silent_restore(self.cfg, name):
                        parsed, msg = fetch_usage(self.cfg, name)
                    if msg == EXPIRED:
                        msg = "세션 만료 — 우클릭 → 재로그인"
                with self._lock:
                    if parsed:
                        self.usage[name] = parsed
                    self.acc_status[name] = msg
        finally:
            self._fetch_lock.release()

    def tick(self):
        self.render()
        self._sec = getattr(self, "_sec", 0) + 1
        if self._sec >= max(15, int(self.cfg.get("poll_seconds", 60))):
            self._sec = 0
            self.schedule_fetch()
        self.root.after(1000, self.tick)

    # ----- 렌더 -----
    def render(self):
        x, y = self.root.winfo_x(), self.root.winfo_y()
        active = self.cfg.get("active_account", "")
        prov = self._prov()
        with self._lock:
            usage = dict(self.usage.get(active) or {})
            status = self.acc_status.get(active) or self.acc_status.get("") or ""
        now = datetime.now(timezone.utc)
        self.prov_lbl.config(text=prov["label"] if active else "", fg=prov["color"])
        theme_mode = self.theme.ok
        for key, row in self.rows.items():
            info = usage.get(key)
            ratio = info["ratio"] if info else 0
            if not theme_mode:
                # 기본 막대: 이름/퍼센트 라벨 갱신
                row["name"].config(text=prov["rows"][key])
                if not info:
                    row["pct"].config(text="--%")
                else:
                    txt = f"{ratio*100:.0f}%"
                    if info["detail"]:
                        txt += f" ({info['detail']})"
                    if info["resets_at"]:
                        remain = int((info["resets_at"] - now).total_seconds())
                        if remain > 0:
                            d, rem = divmod(remain, 86400)
                            h, m = rem // 3600, (rem % 3600) // 60
                            txt += f" · {d}일 {h}h" if d > 0 else f" · {h}:{m:02d}"
                    row["pct"].config(text=txt)
            self.draw_bar(row["bar"], ratio, prov["color"])
        self.status_lbl.config(text=status)
        self._pin_geometry(x, y)

    def draw_bar(self, cv, ratio, accent):
        cv.delete("all")
        # 테마(나무): 잔여량(=1-사용률)에 맞는 이미지를 캔버스 폭에 맞춰 그림
        if self.theme.ok:
            remaining = 1.0 - max(0.0, min(1.0, ratio))
            w = int(cv.winfo_reqwidth()) or self._tree_width()
            photo = self.theme.photo_for(remaining, w)
            if photo is not None:
                if abs(cv.winfo_reqheight() - photo.height()) > 1:
                    cv.config(height=photo.height())
                cv.create_image(cv.winfo_reqwidth()//2, 0, anchor="n", image=photo)
                cv._photo_ref = photo   # GC 방지
                return
        # 기본 막대 게이지 (양 끝이 둥근 알약형)
        w = self.px(232)
        color = accent
        if ratio >= 0.95:
            color = DANGER
        elif ratio >= 0.8:
            color = WARN
        h = self.px(10)
        if abs(cv.winfo_reqheight() - h) > 1:
            cv.config(height=h)
        round_rect(cv, 0, 0, w - 1, h - 1, h / 2, fill=TRACK)
        if ratio > 0:
            round_rect(cv, 0, 0, max(int(w * ratio), h) - 1, h - 1, h / 2, fill=color)

    def quit(self):
        self.cfg["win_x"], self.cfg["win_y"] = self.root.winfo_x(), self.root.winfo_y()
        save_config(self.cfg)
        self.root.destroy()


# ---------------- 메인 ----------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", action="store_true", help="API 응답 원본 출력(진단)")
    parser.add_argument("--profile", default="", help="인스턴스 분리 실행용 이름 (설정/프로필 별도 저장)")
    parser.add_argument("--browser", default="", choices=["", "edge", "chrome"],
                        help="로그인 창을 띄울 브라우저 (기본: 내장 크로미움)")
    args = parser.parse_args()

    if args.profile:
        PROFILE_LABEL = args.profile
        CONFIG_FILE = os.path.join(DIR, f"ai_usage_config_{args.profile}.json")
        LEGACY_CONFIG_FILE = os.path.join(DIR, f"claude_sticker_config_{args.profile}.json")
    if args.browser:
        BROWSER_CHANNEL = {"edge": "msedge", "chrome": "chrome"}[args.browser]
    elif getattr(sys, "frozen", False):
        # 배포용 exe 기본값: 설치된 Edge 사용 → 받는 쪽에서 chromium 설치 불필요
        BROWSER_CHANNEL = "msedge"

    if args.dump:
        dump_mode()
        sys.exit(0)

    LiveSticker(load_config())
