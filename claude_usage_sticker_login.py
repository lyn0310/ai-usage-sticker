# -*- coding: utf-8 -*-
"""
Claude.ai 실사용량 스티커 위젯 — 멀티 계정 버전
- 계정마다 독립된 브라우저 프로필 폴더 사용 → 로그인/로그아웃 충돌 없음
  (Edge/Chrome은 실행 파일만 빌려 쓰고, 평소 쓰는 브라우저 프로필은 건드리지 않음)
- 스티커 상단 탭으로 계정 전환, [＋]로 계정 추가
- 모든 계정 사용량을 백그라운드에서 함께 갱신 → 탭 전환 시 즉시 표시
- 5시간 세션 / 주간 전체 / 주간 모델별 게이지 + 리셋 카운트다운
- 팀에 속한 계정은 개인/팀 중 어느 플랜 사용량을 볼지 선택 (로그인 직후 자동 표시, 우클릭 → 조직 선택)
- 드래그로 위치 이동, ✕=종료 · –=작업표시줄로 축소(작업표시줄 아이콘 클릭 시 복귀)
- 우클릭 메뉴: 새로고침/계정 추가/조직 선택/재로그인/삭제/위젯 크기(85~175%)/종료
- 세션 만료 시 프로필 폴더에서 조용히 자동 복원 시도 → 안 되면 "재로그인 필요" 표시

실행:   python claude_usage_sticker_login.py
진단:   python claude_usage_sticker_login.py --dump
Edge로: python claude_usage_sticker_login.py --browser edge   (로그인 창을 Edge로)
구방식: python claude_usage_sticker_login.py --profile work   (인스턴스 자체를 분리 실행)
필요:   pip install curl_cffi playwright
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
from datetime import datetime, timezone

BASE = "https://claude.ai"

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

CONFIG_FILE = os.path.join(DIR, "claude_sticker_config.json")
BROWSER_CHANNEL = None   # None=내장 크로미움, "msedge"=Edge, "chrome"=설치된 크롬
PROFILE_LABEL = ""       # --profile 이름 (인스턴스 분리 실행용, 제목에 표시)
ICON_NAME = "claude_sticker.ico"
THEMES_DIRNAME = "themes"   # exe/스크립트 옆의 테마 폴더


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
    "accounts": {},        # 계정이름 -> {"session_key": "", "org_uuid": ""}
    "active_account": "",
    "usage_url": "",       # 비워두면 후보 URL 자동 시도. {org} 플레이스홀더 사용 가능
    "poll_seconds": 60,
    "scale": 1.0,          # 위젯 크기 배율 (0.85 ~ 1.75)
    "theme": "",           # "" = 기본 막대 게이지, 그 외 = themes/<이름> 폴더 사용
    "win_x": 100,
    "win_y": 100,
}

BG, FG, SUB = "#2b2620", "#e8e0d4", "#a89f90"
ACCENT, WARN, DANGER, TRACK = "#d97757", "#e3b341", "#e05d44", "#4a4238"
TAB_ACTIVE_BG = "#403830"
ALPHA = 0.97   # 창 투명도 (낮을수록 뒷화면이 비쳐 글씨가 안 보일 수 있음)

ROW_KEYS = ("session", "weekly")
ROW_NAMES = {"session": "5시간 세션", "weekly": "주간 · 전체"}

EXPIRED = "__EXPIRED__"   # 내부용: 세션 만료 표식


# ---------------- 계정별 프로필 폴더 ----------------
def safe_name(name):
    """폴더 이름에 못 쓰는 문자 제거 (한글은 그대로 사용 가능)."""
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", (name or "").strip())
    return s or "계정"


def profile_dir_for(name):
    prefix = f"claude_login_profile_{PROFILE_LABEL}_" if PROFILE_LABEL else "claude_login_profile_"
    return os.path.join(DIR, prefix + safe_name(name))


# ---------------- 설정 ----------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
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
    if cfg["accounts"] and cfg.get("active_account") not in cfg["accounts"]:
        cfg["active_account"] = next(iter(cfg["accounts"]))
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------- HTTP ----------------
def http_get(url, session_key):
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
        "Accept": "application/json",
        "Referer": "https://claude.ai/",
    }
    cookies = {"sessionKey": session_key}
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, headers=headers, cookies=cookies,
                     impersonate="chrome", timeout=15)
    except ImportError:
        import requests
        r = requests.get(url, headers=headers, cookies=cookies, timeout=15)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, getattr(r, "text", "")[:500]


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


def fetch_usage(cfg, name):
    """지정 계정의 사용량 조회. (parsed, 상태메시지) 반환. 만료면 (None, EXPIRED)."""
    acc = cfg["accounts"].get(name) or {}
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


# ---------------- 브라우저 로그인 ----------------
def _find_session_key(ctx):
    try:
        for c in ctx.cookies():
            if c.get("name") == "sessionKey" and "claude.ai" in c.get("domain", ""):
                return c.get("value")
    except Exception:
        pass
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


def read_key_from_profile(profile_dir):
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
            key = _find_session_key(ctx)
            ctx.close()
            return key
    except Exception:
        return None


def interactive_login(profile_dir, timeout_sec=600):
    """브라우저 창을 띄워 사용자가 직접 로그인. 완료되면 sessionKey 반환."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "Playwright 미설치 → pip install playwright && playwright install chromium"
    try:
        with sync_playwright() as p:
            ctx = _launch_ctx(p, headless=False, profile_dir=profile_dir)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(f"{BASE}/login", timeout=60000)
            except Exception:
                pass
            key, t0 = None, time.time()
            while time.time() - t0 < timeout_sec:
                key = _find_session_key(ctx)
                if key:
                    break
                if not ctx.pages:  # 사용자가 창을 닫음
                    return None, "브라우저가 닫혔어요 (로그인 미완료)"
                time.sleep(1)
            ctx.close()
            return (key, None) if key else (None, "로그인 시간 초과")
    except Exception as e:
        return None, f"브라우저 실행 실패: {e}"


def try_silent_restore(cfg, name):
    """프로필 폴더에서 새 sessionKey를 조용히 복원. 성공 시 True."""
    key = read_key_from_profile(profile_dir_for(name))
    if key and key_alive(key):
        acc = cfg["accounts"].get(name)
        if acc is not None:
            acc["session_key"] = key   # org_uuid(조직 선택)는 유지
            save_config(cfg)
            return True
    return False


# ---------------- 진단 모드 ----------------
def dump_mode():
    cfg = load_config()
    name = cfg.get("active_account") or "계정1"
    if name not in cfg["accounts"]:
        cfg["accounts"][name] = {"session_key": "", "org_uuid": ""}
        cfg["active_account"] = name
    acc = cfg["accounts"][name]

    if not key_alive(acc.get("session_key")):
        if not try_silent_restore(cfg, name):
            key, err = interactive_login(profile_dir_for(name))
            if not key:
                print(f"[로그인 실패] {err}")
                return
            acc["session_key"], acc["org_uuid"] = key, ""
            save_config(cfg)
    print(f"[계정] {name}")

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

        try:
            self.scale = float(cfg.get("scale", 1.0)) or 1.0
        except Exception:
            self.scale = 1.0
        self._minimized = False

        self.theme = ThemeImages()
        self.theme.load(cfg.get("theme", ""))

        # 작업표시줄이 파이썬 아이콘 대신 우리 아이콘을 쓰도록 앱 ID 등록
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "claude.usage.sticker")
            except Exception:
                pass

        self.root = tk.Tk()
        self.root.title("Claude 사용량")          # 축소 시 작업표시줄에 표시될 이름
        self._set_icon()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", ALPHA)
        self.root.configure(bg=BG)
        self.root.geometry(f"+{cfg['win_x']}+{cfg['win_y']}")   # 크기는 내용에 맞춰 자동
        self.root.bind("<Map>", self._on_map)

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
        return ("Malgun Gothic", s, "bold") if bold else ("Malgun Gothic", s)

    def mono(self, size):
        return ("Consolas", max(7, int(round(size * self.scale))), "bold")

    def set_scale(self, s):
        self.scale = s
        self.cfg["scale"] = s
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
        for w in self.root.winfo_children():
            w.destroy()
        self.build_ui()
        self.rebuild_tabs()
        self.rebuild_menu()
        self.render()

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

    # ----- UI 구성 -----
    def build_ui(self):
        tk = self.tk
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=self.px(10), pady=(self.px(7), 0))
        title_text = "Claude 사용량" + (f" · {PROFILE_LABEL}" if PROFILE_LABEL else "")
        title = tk.Label(head, text=title_text, bg=BG, fg=FG, font=self.fnt(9, True))
        title.pack(side="left")

        # 우상단 ✕(종료) / –(작업표시줄로 축소)
        close_b = tk.Label(head, text=" ✕ ", bg=BG, fg=SUB, font=self.fnt(9, True),
                           cursor="hand2")
        close_b.pack(side="right")
        close_b.bind("<Button-1>", lambda e: self.quit())
        close_b.bind("<Enter>", lambda e: close_b.config(fg=DANGER))
        close_b.bind("<Leave>", lambda e: close_b.config(fg=SUB))
        min_b = tk.Label(head, text=" – ", bg=BG, fg=SUB, font=self.fnt(9, True),
                         cursor="hand2")
        min_b.pack(side="right")
        min_b.bind("<Button-1>", lambda e: self.minimize())
        min_b.bind("<Enter>", lambda e: min_b.config(fg=FG))
        min_b.bind("<Leave>", lambda e: min_b.config(fg=SUB))

        self.make_draggable(head)
        self.make_draggable(title)

        # 계정 탭 바
        self.tabbar = tk.Frame(self.root, bg=BG)
        self.tabbar.pack(fill="x", padx=self.px(10), pady=(self.px(2), self.px(3)))
        self.make_draggable(self.tabbar)

        self.rows = {}
        if self.theme.ok:
            self._build_tree_row()
        else:
            self._build_bar_rows()

        self.status_lbl = tk.Label(self.root, text="시작 중…", bg=BG, fg=SUB,
                                   font=self.fnt(8), anchor="w")
        self.status_lbl.pack(fill="x", padx=self.px(10), pady=(0, self.px(6)))
        self.make_draggable(self.status_lbl)
        self.make_draggable(self.root)

    def _build_bar_rows(self):
        """기본 막대 게이지 (세로로 세션/주간 2줄)."""
        tk = self.tk
        for key in ROW_KEYS:
            line = tk.Frame(self.root, bg=BG)
            line.pack(fill="x", padx=self.px(10))
            name_lbl = tk.Label(line, text=ROW_NAMES[key], bg=BG, fg=SUB, font=self.fnt(8))
            name_lbl.pack(side="left")
            pct_lbl = tk.Label(line, text="--%", bg=BG, fg=FG, font=self.mono(9))
            pct_lbl.pack(side="right")
            bar = tk.Canvas(self.root, width=self.px(232), height=self.px(11),
                            bg=BG, highlightthickness=0)
            bar.pack(padx=self.px(10), pady=(0, self.px(4)))
            for w in (line, name_lbl):
                self.make_draggable(w)
            self.make_draggable(bar)
            self.rows[key] = {"name": name_lbl, "pct": pct_lbl, "bar": bar}

    def _build_tree_row(self):
        """나무 테마: 세션/주간 나무를 가로로 나란히. 이름/퍼센트는 툴팁으로."""
        tk = self.tk
        tree_h = self._tree_height()
        tree_w = self._tree_width()
        holder = tk.Frame(self.root, bg=BG)
        holder.pack(padx=self.px(10), pady=(self.px(4), self.px(8)))
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
        text = self._tip_text(key)
        tk = self.tk
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg=TRACK)
        lbl = tk.Label(tip, text=text, bg=TRACK, fg=FG, font=self.fnt(8),
                       padx=self.px(8), pady=self.px(3), justify="left")
        lbl.pack()
        tip.update_idletasks()
        tip.geometry(f"+{e.x_root + 12}+{e.y_root + 12}")
        self._tip_win = tip

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
        name = ROW_NAMES.get(key, key)
        if not info:
            return f"{name}: --"
        from datetime import datetime, timezone
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
        tk = self.tk
        x, y = self.root.winfo_x(), self.root.winfo_y()
        for w in self.tabbar.winfo_children():
            w.destroy()
        for name in self.cfg["accounts"]:
            active = (name == self.cfg.get("active_account"))
            lbl = tk.Label(self.tabbar, text=name,
                           bg=TAB_ACTIVE_BG if active else BG,
                           fg=FG if active else SUB,
                           font=self.fnt(8),
                           padx=self.px(6), pady=0)
            lbl.pack(side="left", padx=(0, self.px(4)))
            lbl.bind("<Button-1>", lambda e, n=name: self.switch_account(n))
            lbl.bind("<Double-Button-1>", lambda e, n=name: self.rename_account(n))
            lbl.bind("<Button-3>", lambda e: self.show_menu(e.x_root, e.y_root))
        plus = tk.Label(self.tabbar, text="＋", bg=BG, fg=SUB,
                        font=self.fnt(9, True), padx=self.px(4))
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
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_reqheight()
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def rebuild_menu(self):
        # 커스텀 메뉴는 열 때마다 새로 그리므로, 열려 있는 메뉴만 닫음
        self._close_menu()

    # ----- 커스텀 다크 팝업/대화상자 -----
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
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        left, top, right, bottom = self._monitor_workarea(x, y)
        x = max(left, min(x, right - w - 4))
        y = max(top, min(y, bottom - h - 4))
        win.geometry(f"+{x}+{y}")

    def _close_menu(self):
        win = getattr(self, "_menu_win", None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
            self._menu_win = None

    def show_menu(self, x, y):
        """스티커와 같은 다크 테마의 우클릭 메뉴."""
        tk = self.tk
        self._close_menu()
        active = self.cfg.get("active_account", "")
        win = tk.Toplevel(self.root)
        self._menu_win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG, highlightbackground=TRACK, highlightthickness=1)

        def add_item(text, cmd, fg=FG):
            lbl = tk.Label(win, text=text, bg=BG, fg=fg, anchor="w",
                           font=self.fnt(9), padx=self.px(12), pady=self.px(4))
            lbl.pack(fill="x")
            lbl.bind("<Button-1>", lambda e: (self._close_menu(), cmd()))
            lbl.bind("<Enter>", lambda e, w=lbl: w.config(bg=TAB_ACTIVE_BG))
            lbl.bind("<Leave>", lambda e, w=lbl: w.config(bg=BG))

        def add_sep():
            tk.Frame(win, bg=TRACK, height=1).pack(fill="x", padx=self.px(10),
                                                   pady=self.px(3))

        add_item("지금 새로고침", self.schedule_fetch)
        if self.cfg["accounts"]:
            add_sep()
            for n in self.cfg["accounts"]:
                mark = "● " if n == active else "○ "
                add_item(mark + n, lambda n=n: self.switch_account(n))
        add_sep()
        add_item("계정 추가…", self.add_account)
        if active:
            add_item(f"'{active}' 이름 변경…",
                     lambda n=active: self.rename_account(n))
            add_item(f"'{active}' 조직 선택…",
                     lambda n=active: self.maybe_choose_org(n, force=True))
            add_item(f"'{active}' 재로그인", self.relogin)
            add_item(f"'{active}' 삭제", self.remove_account)
        add_sep()
        row = tk.Frame(win, bg=BG)
        row.pack(fill="x", padx=self.px(12), pady=self.px(2))
        tk.Label(row, text="크기", bg=BG, fg=SUB, font=self.fnt(8)).pack(side="left")
        for pct in (85, 100, 125, 150, 175):
            cur = abs(self.scale - pct / 100) < 0.01
            b = tk.Label(row, text=str(pct), bg=TAB_ACTIVE_BG if cur else BG,
                         fg=FG if cur else SUB, font=self.fnt(8),
                         padx=self.px(4), cursor="hand2")
            b.pack(side="left", padx=self.px(2))
            b.bind("<Button-1>",
                   lambda e, p=pct: (self._close_menu(), self.set_scale(p / 100)))

        # 테마 선택: 기본(막대) + themes 폴더에서 찾은 테마들
        themes = list_themes()
        trow = tk.Frame(win, bg=BG)
        trow.pack(fill="x", padx=self.px(12), pady=self.px(2))
        tk.Label(trow, text="테마", bg=BG, fg=SUB, font=self.fnt(8)).pack(side="left")
        opts = [("기본", "")] + [(t, t) for t in themes]
        for label, val in opts:
            cur = (self.cfg.get("theme", "") == val)
            b = tk.Label(trow, text=label, bg=TAB_ACTIVE_BG if cur else BG,
                         fg=FG if cur else SUB, font=self.fnt(8),
                         padx=self.px(4), cursor="hand2")
            b.pack(side="left", padx=self.px(2))
            b.bind("<Button-1>",
                   lambda e, v=val: (self._close_menu(), self.set_theme(v)))
        if not themes:
            tk.Label(trow, text="(themes 폴더 비어있음)", bg=BG, fg=SUB,
                     font=self.fnt(7)).pack(side="left", padx=self.px(4))

        add_item("작업표시줄로 축소", self.minimize)
        add_sep()
        add_item("종료", self.quit, fg=DANGER)

        self.place_popup(win, x, y)
        win.after(10, win.focus_force)
        win.bind("<FocusOut>", lambda e: self._close_menu())
        win.bind("<Escape>", lambda e: self._close_menu())

    def _dialog_base(self, title):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG, highlightbackground=ACCENT, highlightthickness=1)
        tk.Label(win, text=title, bg=BG, fg=FG, font=self.fnt(9, True)
                 ).pack(padx=self.px(14), pady=(self.px(10), self.px(2)))
        return win

    def ask_text(self, title, prompt, initial=""):
        """다크 테마 텍스트 입력 대화상자. 취소 시 None."""
        tk = self.tk
        win = self._dialog_base(title)
        tk.Label(win, text=prompt, bg=BG, fg=SUB, font=self.fnt(8),
                 justify="left", wraplength=self.px(220)).pack(padx=self.px(14))
        ent = tk.Entry(win, bg=TAB_ACTIVE_BG, fg=FG, insertbackground=FG,
                       relief="flat", font=self.fnt(9), width=22)
        ent.pack(padx=self.px(14), pady=self.px(8), ipady=self.px(3))
        ent.insert(0, initial)
        ent.select_range(0, "end")
        result = {"v": None}

        def ok(e=None):
            result["v"] = ent.get()
            win.destroy()

        def cancel(e=None):
            win.destroy()

        self._dialog_buttons(win, [("확인", ok, ACCENT), ("취소", cancel, TRACK)])
        win.bind("<Return>", ok)
        win.bind("<Escape>", cancel)
        self.place_popup(win, self.root.winfo_x(),
                         self.root.winfo_y() + self.root.winfo_height() + 6)
        win.grab_set()
        ent.focus_set()
        self.root.wait_window(win)
        return result["v"]

    def ask_confirm(self, title, prompt):
        """다크 테마 예/아니오 대화상자."""
        tk = self.tk
        win = self._dialog_base(title)
        tk.Label(win, text=prompt, bg=BG, fg=SUB, font=self.fnt(8),
                 justify="left", wraplength=self.px(220)
                 ).pack(padx=self.px(14), pady=self.px(4))
        result = {"v": False}

        def yes(e=None):
            result["v"] = True
            win.destroy()

        def no(e=None):
            win.destroy()

        self._dialog_buttons(win, [("삭제", yes, DANGER), ("취소", no, TRACK)])
        win.bind("<Return>", yes)
        win.bind("<Escape>", no)
        self.place_popup(win, self.root.winfo_x(),
                         self.root.winfo_y() + self.root.winfo_height() + 6)
        win.grab_set()
        self.root.wait_window(win)
        return result["v"]

    def _dialog_buttons(self, win, specs):
        tk = self.tk
        btns = tk.Frame(win, bg=BG)
        btns.pack(pady=(self.px(2), self.px(10)))
        for text, cmd, color in specs:
            b = tk.Label(btns, text=f"  {text}  ", bg=color,
                         fg="white" if color in (ACCENT, DANGER) else FG,
                         font=self.fnt(9), cursor="hand2", pady=self.px(2))
            b.pack(side="left", padx=self.px(4))
            b.bind("<Button-1>", cmd)

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
        default = self._unique_name(f"계정{len(self.cfg['accounts']) + 1}")
        name = self.ask_text("계정 추가", "계정 이름 (예: 개인, 회사)", default)
        if name is None:
            return
        name = self._unique_name(name.strip() or default)

        self._logging_in = True
        self.set_status_now(f"'{name}' 로그인: 브라우저 창에서 로그인해 주세요…")

        def worker():
            key, err = interactive_login(profile_dir_for(name))
            with self._lock:
                if key:
                    self.cfg["accounts"][name] = {"session_key": key, "org_uuid": ""}
                    self.cfg["active_account"] = name
                    save_config(self.cfg)
                    self.acc_status[name] = "로그인 완료"
                else:
                    self.acc_status[self.cfg.get("active_account", "")] = err or "로그인 실패"
            self._logging_in = False
            self.root.after(0, self.rebuild_tabs)
            self.root.after(0, self.rebuild_menu)
            self.root.after(0, lambda: self.maybe_choose_org(name))
            self.schedule_fetch()

        threading.Thread(target=worker, daemon=True).start()

    def relogin(self):
        name = self.cfg.get("active_account")
        if not name or self._logging_in:
            return
        self._logging_in = True
        self.set_status_now(f"'{name}' 재로그인: 브라우저 창에서 로그인해 주세요…")

        def worker():
            key, err = interactive_login(profile_dir_for(name))
            with self._lock:
                if key:
                    acc = self.cfg["accounts"].get(name)
                    if acc is not None:
                        acc["session_key"] = key   # 조직 선택 유지(다른 계정이면 아래서 재확인)
                        save_config(self.cfg)
                    self.acc_status[name] = "로그인 완료"
                else:
                    self.acc_status[name] = err or "로그인 실패"
            self._logging_in = False
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

    # ----- 조직(개인/팀) 선택 -----
    def maybe_choose_org(self, name, force=False):
        """계정이 여러 조직(개인 플랜 + 팀 등)에 속하면 어느 쪽 사용량을 볼지 선택.
        팀에 초대된 계정이 개인 사용량만 보이던 문제의 해결 지점."""
        acc = self.cfg["accounts"].get(name)
        if not acc or not acc.get("session_key"):
            return

        def worker():
            orgs, err = list_orgs(acc.get("session_key", ""))
            self.root.after(0, lambda: self._org_dialog(name, orgs, err, force))

        threading.Thread(target=worker, daemon=True).start()

    def _org_dialog(self, name, orgs, err, force):
        tk = self.tk
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

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG, highlightbackground=TRACK, highlightthickness=1)
        tk.Label(win, text=f"'{name}' — 사용량을 볼 플랜(조직) 선택", bg=BG, fg=FG,
                 font=self.fnt(9, True), wraplength=self.px(250)
                 ).pack(padx=self.px(12), pady=(self.px(10), self.px(6)))
        for o in orgs:
            mark = "● " if o["uuid"] == cur else "○ "
            b = tk.Label(win, text=mark + o["name"], bg=TRACK, fg=FG, anchor="w",
                         padx=self.px(10), pady=self.px(5), font=self.fnt(9),
                         cursor="hand2", justify="left", wraplength=self.px(240))
            b.pack(fill="x", padx=self.px(12), pady=self.px(3))

            def pick(e=None, u=o["uuid"], n=o["name"]):
                a = self.cfg["accounts"].get(name)
                if a is not None:
                    a["org_uuid"] = u
                    save_config(self.cfg)
                with self._lock:
                    self.usage.pop(name, None)
                    self.acc_status[name] = f"플랜 전환: {n}"
                win.destroy()
                self.schedule_fetch()

            b.bind("<Button-1>", pick)
            b.bind("<Enter>", lambda e, w=b: w.config(bg=TAB_ACTIVE_BG))
            b.bind("<Leave>", lambda e, w=b: w.config(bg=TRACK))
        c = tk.Label(win, text="나중에", bg=BG, fg=SUB, font=self.fnt(8), cursor="hand2")
        c.pack(pady=(self.px(2), self.px(8)))
        c.bind("<Button-1>", lambda e: win.destroy())
        self.place_popup(win, self.root.winfo_x(),
                         self.root.winfo_y() + self.root.winfo_height() + 6)

    # ----- 데이터 -----
    def set_status_now(self, text):
        name = self.cfg.get("active_account", "")
        with self._lock:
            self.acc_status[name] = text

    def startup_worker(self):
        """시작 시: 계정이 없으면 첫 로그인, 있으면 조용히 세션 점검 후 갱신."""
        if not self.cfg["accounts"]:
            self._logging_in = True
            name = "계정1"
            with self._lock:
                self.acc_status[""] = "첫 로그인: 브라우저 창에서 로그인해 주세요…"
            key, err = interactive_login(profile_dir_for(name))
            with self._lock:
                if key:
                    self.cfg["accounts"][name] = {"session_key": key, "org_uuid": ""}
                    self.cfg["active_account"] = name
                    save_config(self.cfg)
                    self.acc_status[name] = "로그인 완료"
                else:
                    self.acc_status[""] = err or "로그인 실패 — 우클릭 → 계정 추가"
            self._logging_in = False
            self.root.after(0, self.rebuild_tabs)
            self.root.after(0, self.rebuild_menu)
            self.root.after(0, lambda: self.maybe_choose_org(name))
        else:
            for name in list(self.cfg["accounts"]):
                acc = self.cfg["accounts"][name]
                if not key_alive(acc.get("session_key")):
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
                parsed, msg = fetch_usage(self.cfg, name)
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
        with self._lock:
            usage = dict(self.usage.get(active) or {})
            status = self.acc_status.get(active) or self.acc_status.get("") or ""
        now = datetime.now(timezone.utc)
        theme_mode = self.theme.ok
        for key, row in self.rows.items():
            info = usage.get(key)
            ratio = info["ratio"] if info else 0
            if not theme_mode:
                # 기본 막대: 이름/퍼센트 라벨 갱신
                row["name"].config(text=ROW_NAMES[key])
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
            self.draw_bar(row["bar"], ratio)
        self.status_lbl.config(text=status)
        self._pin_geometry(x, y)

    def draw_bar(self, cv, ratio):
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
        # 기본 막대 게이지
        w = self.px(232)
        color = ACCENT
        if ratio >= 0.95:
            color = DANGER
        elif ratio >= 0.8:
            color = WARN
        h, r = self.px(11), self.px(5)
        if abs(cv.winfo_reqheight() - h) > 1:
            cv.config(height=h)
        self._round_rect(cv, 0, 0, w, h, r, fill=TRACK)
        if ratio > 0:
            self._round_rect(cv, 0, 0, max(int(w * ratio), h), h, r, fill=color)

    @staticmethod
    def _round_rect(cv, x1, y1, x2, y2, r, **kw):
        cv.create_arc(x1, y1, x1+2*r, y1+2*r, start=90, extent=90, style="pieslice", outline="", **kw)
        cv.create_arc(x2-2*r, y1, x2, y1+2*r, start=0, extent=90, style="pieslice", outline="", **kw)
        cv.create_arc(x1, y2-2*r, x1+2*r, y2, start=180, extent=90, style="pieslice", outline="", **kw)
        cv.create_arc(x2-2*r, y2-2*r, x2, y2, start=270, extent=90, style="pieslice", outline="", **kw)
        cv.create_rectangle(x1+r, y1, x2-r, y2, outline="", **kw)
        cv.create_rectangle(x1, y1+r, x2, y2-r, outline="", **kw)

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
        CONFIG_FILE = os.path.join(DIR, f"claude_sticker_config_{args.profile}.json")
    if args.browser:
        BROWSER_CHANNEL = {"edge": "msedge", "chrome": "chrome"}[args.browser]
    elif getattr(sys, "frozen", False):
        # 배포용 exe 기본값: 설치된 Edge 사용 → 받는 쪽에서 chromium 설치 불필요
        BROWSER_CHANNEL = "msedge"

    if args.dump:
        dump_mode()
        sys.exit(0)

    LiveSticker(load_config())
