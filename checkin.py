#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
豆包电脑版自动签到

用法 (macOS / Linux 用 python3, Windows 用 python):
  python checkin.py run          # 日常运行: 无头签到, 结果打印到 stdout (供 AI 汇报)
  python checkin.py silent       # 同 run, 但结果写入日志文件而非 stdout (配合 Windows pythonw 静默运行)
  python checkin.py silent-poll  # 轮询: 未签才补签; 已签过的空跑不写日志 (配合定时轮询任务)
  python checkin.py doctor       # 离线自检: 不联网, 检查 python/playwright/chromium/凭据/目录可写性
  python checkin.py login        # 备用: 手动登录保存网页登录态 (装了豆包客户端时通常不需要)
  python checkin.py run --url <活动页直链>   # 显式指定活动页 (默认 agent-explore)

凭据来源 (按优先级自动选择):
  1. 客户端凭据模式: 从本机豆包电脑版客户端读取登录凭据, 装了客户端并登录过
     即零配置, 无需扫码。UA 携带客户端标识 SamanthaDoubao/<version> 以通过
     活动页的「仅限电脑版客户端」校验 (该校验实测为 navigator.userAgent
     是否包含 SamanthaDoubao 子串)。
     - macOS (已实测): 钥匙串「Doubao Safe Storage」取密钥 -> AES-128-CBC
       解密客户端 Cookie 库 (明文头部有 32 字节校验块, 跳过即得真实值)
     - Windows (已实测, Windows 11 真机验证): %LOCALAPPDATA% 定位客户端 Chromium 用户数据
       -> Local State 的 DPAPI 密钥 -> AES-256-GCM 解密; 若客户端采用
       v20/App-Bound 加密则暂不支持, 会明确报告原因并回落 login 模式
  2. 网页登录态模式 (后备): Playwright 持久化配置 data/profile 复用登录态。
     注意: 活动页目前限制仅客户端可参与, 网页登录态会被 CLIENT_ONLY 拦截,
     此模式仅作为平台放开限制后的备用通道与诊断手段。

脚本幂等: 已签过只查询不领取, 重复运行 / 多任务并发都不会重复签到。
凭据只读使用: 解密出的 Cookie 仅注入浏览器会话, 不落盘、不打印、不回写客户端。

可靠性设计 (实测教训):
  - 时间预算: 全程受 DOUBAO_BUDGET_SECONDS 约束 (默认 480s), 必须小于定时任务
    的超时上限——跑穿被系统强杀会死在写日志之前, 当天记录整条丢失
  - 分桶退避重试: 导航类失败 (页面打不开/超时) 按 5/15/30 秒退避, 覆盖
    「刚开机 WiFi 还没就绪」窗口; 重试有预算上限, 不会无限耗
  - 配置解析绝不抛异常: 环境变量写错只回落默认值, 避免崩在异常保护生效之前
  - 日志自动截断: 超过 512KB 时保留最新一半, 防止长期轮询无限膨胀

日志: data/checkin.log (可用环境变量 DOUBAO_CHECKIN_LOG 覆盖路径)
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright  # noqa: F401  顶层导入: run/login 运行时依赖, 缺了会 NameError

ROOT = pathlib.Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PROFILE_DIR = DATA_DIR / "profile"
SHOT_DIR = DATA_DIR / "shots"
LOG_FILE = DATA_DIR / "checkin.log"

START_URL = "https://www.doubao.com/"                     # 登录用
ACTIVITY_URL = "https://www.doubao.com/pc/agent-explore"  # 签到活动页直链

# 客户端标识 UA: 活动页前端以 /SamanthaDoubao/ 正则检测 navigator.userAgent,
# 版本号来自客户端内嵌浏览器 (可被环境变量覆盖, 格式 SamanthaDoubao/x.y.z)
CLIENT_UA_VERSION = os.environ.get("DOUBAO_CLIENT_UA_VERSION", "2.33.3")
_CLIENT_UA_BASE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    if sys.platform == "win32" else
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
CLIENT_UA = f"{_CLIENT_UA_BASE} SamanthaDoubao/{CLIENT_UA_VERSION}"
# 客户端数据目录: macOS 固定路径; Windows 在 %LOCALAPPDATA% 下自动探测
CLIENT_DATA_DIR = pathlib.Path.home() / "Library/Application Support/Doubao"
KEYCHAIN_SERVICES = ["Doubao Safe Storage", "Doubao Browser Safe Storage"]

# 签到按钮的候选文案 (按优先级; 豆包改版后在这里补充新文案)
CLICK_TEXTS = ["立即领取", "立即签到", "签到领额度", "今日签到", "签到"]
# 成功标志文案 (注意不要放「获得」这类页面常驻词, 会把已签状态误判成 success)
SUCCESS_TEXTS = ["签到成功", "已签到", "领取成功", "明日再来"]
# 已签过/无可签的标志
ALREADY_TEXTS = ["已签到", "明日再来", "今日已领"]

REPORTS = {
    "success": "豆包签到成功, 200 额度已到账",
    "already": "今日已签过, 无需重复领取",
    "unknown": "签到结果未能确认, 请人工查看运行截图",
    "AUTH_ERROR": "登录态失效: 客户端凭据模式需在豆包电脑版里重新登录; 网页模式重跑 login",
    "NETWORK": "网络不可达或页面打不开 (重试后仍失败), 稍后轮询会自动补",
    "BROWSER": "浏览器/playwright 环境异常, 先运行 doctor 自检",
    "TIMEOUT": "整体时间预算耗尽, 任务超时上限可能太小",
    "CLIENT_ONLY": "活动仅支持豆包电脑版客户端参与, 当前通道被平台拦截",
    "NO_CREDENTIAL": "未找到可用凭据: 安装豆包电脑版并登录, 或运行 login 保存网页登录态",
}

NEEDS_ATTENTION_CODES = {"unknown", "AUTH_ERROR", "ERROR", "BROWSER", "TIMEOUT", "CLIENT_ONLY", "NO_CREDENTIAL"}


def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _env_int(name: str, default: int, lo: int = 1, hi: int = 86400) -> int:
    """读整型环境变量; 解析失败/越界一律回落默认值, 绝不抛异常。

    教训: 曾有环境变量写成非数字导致进程崩在异常保护生效之前,
    结果一条日志都没留下, 当天记录整条丢失。
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, val))


BUDGET_SECONDS = _env_int("DOUBAO_BUDGET_SECONDS", 480, lo=60, hi=7200)
BUDGET_START = time.monotonic()


def budget_left() -> int:
    """剩余时间预算 (秒), 下限 0。"""
    return max(0, BUDGET_SECONDS - int(time.monotonic() - BUDGET_START))


def budget_exhausted() -> bool:
    return budget_left() <= 0


def rotate_log() -> None:
    """日志超过 512KB 时截半保留最新部分, 防长期轮询无限膨胀。失败静默。"""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512 * 1024:
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            LOG_FILE.write_text("".join(lines[len(lines) // 2:]), encoding="utf-8")
    except Exception:
        pass


def log(msg: str) -> None:
    """过程日志: 打印 + 落盘, 双通道, 保证不抛异常。"""
    line = f"[{now_str()}] {msg}"
    try:
        print(line)
    except Exception:
        pass  # pythonw 下 stdout 是 None, 打印会静默失败
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        rotate_log()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(title: str, body: str) -> None:
    """macOS 弹系统通知; 其他平台静默跳过 (结果以日志为准)。"""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{body}" with title "{title}"'],
            check=False, timeout=10,
        )
    except Exception:
        pass


def emit(out: dict, action: str) -> None:
    """结果输出: silent 类模式写日志文件, 其余打印到 stdout; 本函数保证不抛异常。

    判定用前缀而不是等号: silent-poll 同样是无窗口跑的, pythonw 下 sys.stdout
    是 None, print 会静默成功(不抛异常), 于是"打到 stdout"等于扔进黑洞——
    AUTH_ERROR、unknown 这类最需要人处理的结果会一条不剩地消失。

    非 silent 模式下 stdout 不可用(编码/管道问题)时退到日志, 至少不把结果丢掉。
    """
    out = dict(out)
    out.setdefault("needs_attention", out.get("result") in NEEDS_ATTENTION_CODES)
    payload = json.dumps(out, ensure_ascii=False)
    if not str(action).startswith("silent"):
        try:
            print(payload)
            return
        except Exception:
            pass  # stdout 不可用时退到日志
    line = f"[{now_str()}] {payload}\n"
    custom = os.environ.get("DOUBAO_CHECKIN_LOG")
    for path in ([pathlib.Path(custom)] if custom else []) + [LOG_FILE]:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            rotate_log()
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            return
        except Exception:
            continue


def goto_with_retry(page, url: str) -> bool:
    """导航 + 分桶退避重试。

    导航是只读操作 (页面本身幂等), 重试安全。退避序列 5/15/30 秒,
    专门覆盖「刚开机 WiFi 还没连上」的窗口; 受时间预算约束, 超预算即止。

    返回 True=页面已打开; False=预算内重试仍失败 (结果按 NETWORK 上报)。
    """
    backoffs = [5, 15, 30]
    for attempt in range(len(backoffs) + 1):
        if budget_exhausted():
            log("时间预算耗尽, 停止重试")
            return False
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return True
        except Exception as exc:
            kind = "超时" if "Timeout" in type(exc).__name__ else "网络错误"
            if attempt >= len(backoffs):
                log(f"导航失败 ({kind}), 重试额度已用完: {exc}")
                return False
            wait = min(backoffs[attempt], budget_left())
            log(f"导航失败 ({kind}), {wait}s 后第 {attempt + 1} 次重试")
            page.wait_for_timeout(wait * 1000)


def page_text(page) -> str:
    try:
        return page.inner_text("body")[:4000]
    except Exception:
        return ""


def find_and_click(page) -> bool:
    """按候选文案找签到按钮并点击, 返回是否点到。"""
    for text in CLICK_TEXTS:
        try:
            # 优先精确按钮, 再退到文本节点
            loc = page.get_by_role("button", name=text, exact=False)
            if loc.count() == 0:
                loc = page.get_by_text(text, exact=False)
            if loc.count() == 0:
                continue
            target = loc.first
            if target.is_visible():
                target.click(timeout=5000)
                log(f"已点击按钮: 「{text}」")
                return True
        except Exception:
            continue
    return False


def check_success(page) -> str:
    """判断签到结果: success / already / unknown

    「每日签到」任务的完成态判定 (实测): 任务卡片常驻, 未签时按钮区是
    「立即签到」, 已签后按钮消失变成对勾图标——所以「每日签到」在而
    「立即签到」不在即是已签, 这是比通用文案更可靠的幂等判据。
    """
    body = page_text(page)
    if "每日签到" in body and "立即签到" not in body:
        return "already"
    for t in ALREADY_TEXTS:
        if t in body:
            return "already"
    for t in SUCCESS_TEXTS:
        if t in body:
            return "success"
    return "unknown"


# ---------------------------------------------------------------------------
# 客户端凭据模式 (macOS): 钥匙串密钥 -> AES 解密客户端 Cookie 库
# ---------------------------------------------------------------------------

def _client_keychain_key() -> bytes | None:
    """从 macOS 钥匙串取豆包客户端的 Cookie 加密密钥。失败返回 None。

    只读取出密钥内容用于内存解密, 不写入任何文件。
    首次在终端使用时 macOS 可能弹一次钥匙串授权框, 选「始终允许」后不再弹。
    """
    if sys.platform != "darwin":
        return None
    for service in KEYCHAIN_SERVICES:
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", service],
                capture_output=True, timeout=15,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            continue
    return None


def _decrypt_client_cookie(blob: bytes, key_material: bytes) -> str | None:
    """解密豆包客户端 (macOS, 定制 Chromium) 的 v10 Cookie 密文, 返回明文文本。

    算法: PBKDF2-SHA1(salt='saltysalt', 1003 轮, 128 位) -> AES-128-CBC(IV=16 空格)
    -> PKCS7 去填充 -> 明文头部有 32 字节校验块 (豆包定制, 原版 Chromium 没有),
    跳过即得真实值。任何一步失败返回 None。
    """
    if len(blob) < 3 + 48 or blob[:3] not in (b"v10", b"v11"):
        return None
    try:
        from Crypto.Cipher import AES  # pycryptodome
    except Exception:
        return None
    try:
        aes_key = hashlib.pbkdf2_hmac("sha1", key_material, b"saltysalt", 1003, dklen=16)
        pt = AES.new(aes_key, AES.MODE_CBC, b" " * 16).decrypt(blob[3:])
        pad = pt[-1]
        if 1 <= pad <= 16 and pt[-pad:] == bytes([pad]) * pad:
            pt = pt[:-pad]
        value = pt[32:]  # 跳过头部 32 字节校验块
        if not value:
            return None
        return value.decode("utf-8", errors="replace")
    except Exception:
        return None


def _windows_dpapi_unprotect(data: bytes) -> bytes:
    """Windows DPAPI CryptUnprotectData (当前用户作用域), ctypes 实现, 免 pywin32。"""
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _windows_client_roots() -> list[pathlib.Path]:
    """定位 Windows 豆包客户端的 Chromium 用户数据目录 (以含 Local State 为准)。"""
    out = []
    la = os.environ.get("LOCALAPPDATA")
    base = pathlib.Path(la) if la else None
    if not base or not base.exists():
        return out
    for p in sorted(base.iterdir()):
        if not (p.is_dir() and "doubao" in p.name.lower()):
            continue
        # 兼容两种布局: 数据直接在根目录 / 标准 Chromium 的 User Data 子目录
        for cand in (p, p / "User Data"):
            if (cand / "Local State").is_file():
                out.append(cand)
                break
    return out


def _windows_os_crypt_key(root: pathlib.Path) -> bytes | None:
    """从 Local State 取 Windows Chromium 的 Cookie 主密钥 (DPAPI 解包)。"""
    try:
        data = json.loads((root / "Local State").read_text(encoding="utf-8"))
        enc = base64.b64decode(data["os_crypt"]["encrypted_key"])
        if enc[:5] != b"DPAPI":
            return None
        return _windows_dpapi_unprotect(enc[5:])
    except Exception:
        return None


def _decrypt_cookie_windows(blob: bytes, key: bytes) -> str | None:
    """解密 Windows Chromium 格式 Cookie (v10 = AES-256-GCM), 返回明文文本。

    原版明文直接可用; 若豆包 Windows 版沿用了 mac 端的 32 字节校验头定制,
    标准解出会不可读, 此时尝试跳头重解。两路都以 UTF-8 可解码为准,
    防止把密文残渣当值。v20 (App-Bound Encryption) 与解密失败返回 None。
    """
    if blob[:3] not in (b"v10", b"v11"):
        return None
    if len(blob) < 3 + 12 + 16 + 16:
        return None
    try:
        from Crypto.Cipher import AES  # pycryptodome
    except Exception:
        return None
    data = blob[3:]
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=data[:12])
        pt = cipher.decrypt(data[12:])[:-16]  # 末 16 字节是 GCM tag
    except Exception:
        return None
    for cand in (pt, pt[32:]):
        if not cand:
            continue
        try:
            return cand.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def read_client_cookies() -> tuple[list[dict], str]:
    """读取豆包电脑版客户端的登录 Cookie (macOS / Windows 均已实测)。

    返回 (cookies, reason): cookies 为空列表时 reason 说明原因。
    只读取, 解密在内存完成, Cookie 值不打印、不落盘。
    """
    import shutil
    import sqlite3
    import tempfile

    if sys.platform not in ("darwin", "win32"):
        return [], "客户端凭据模式目前仅支持 macOS / Windows (其他平台仍走 login 模式)"
    try:
        from Crypto.Cipher import AES  # noqa: F401  探测依赖, 缺了给出安装提示
    except Exception:
        return [], "缺少 pycryptodome, 安装: pip3 install pycryptodome"

    # 收集 (Cookie 库路径, 解密函数) 任务列表
    jobs: list[tuple[pathlib.Path, object]] = []
    notes: list[str] = []
    if sys.platform == "darwin":
        if not CLIENT_DATA_DIR.exists():
            return [], f"未找到豆包客户端数据目录: {CLIENT_DATA_DIR}"
        key = _client_keychain_key()
        if not key:
            return [], ("钥匙串取密钥失败 (需安装豆包电脑版并登录过; "
                        "终端首次使用请在弹窗里选「始终允许」)")
        for profile in sorted(CLIENT_DATA_DIR.glob("*")):
            db = profile / "Cookies"
            if db.is_file():
                jobs.append((db, lambda blob, k=key: _decrypt_client_cookie(blob, k)))
        if not jobs:
            return [], "客户端目录里没有可读的 Cookie 库 (客户端可能未登录或已清空数据)"
    else:  # win32
        roots = _windows_client_roots()
        if not roots:
            return [], "未在 %LOCALAPPDATA% 下找到豆包客户端的用户数据目录 (确认已安装并登录过客户端)"
        for root in roots:
            key = _windows_os_crypt_key(root)
            if not key:
                notes.append(f"{root.name}: Local State 密钥解密失败")
                continue
            for db in sorted(root.glob("*/Cookies")) + sorted(root.glob("*/Network/Cookies")):
                jobs.append((db, lambda blob, k=key: _decrypt_cookie_windows(blob, k)))
        if not jobs:
            return [], ("Windows 客户端凭据不可用: " + ("; ".join(notes) or "无可读 Cookie 库")
                        + " (若客户端为 v20/App-Bound 加密则暂不支持, 请用 login 模式)")

    cookies: list[dict] = []
    for db, decrypt in jobs:
        try:
            tmp = pathlib.Path(tempfile.mkdtemp(prefix="doubao-ck-")) / "Cookies"
            try:
                shutil.copy(db, tmp)  # 原库可能被客户端进程锁定, 拷贝后只读查询
            except PermissionError:
                # Windows 上客户端运行时 Cookies 库被独占锁定, shutil 拷不动;
                # esentutl 用备份语义可读锁定文件 (系统自带, 不引第三方依赖)
                r = subprocess.run(
                    ["esentutl", "/y", str(db), "/d", str(tmp), "/o"],
                    capture_output=True, timeout=60,
                )
                if r.returncode != 0 or not tmp.is_file():
                    continue
            con = sqlite3.connect(str(tmp))
            try:
                rows = con.execute(
                    "SELECT host_key, name, encrypted_value, path, is_secure, is_httponly, expires_utc "
                    "FROM cookies WHERE host_key LIKE '%doubao%'"
                ).fetchall()
            finally:
                con.close()  # Windows 上不先关连接, unlink 会报 WinError 32 并吞掉整库数据
            tmp.unlink(missing_ok=True)
        except Exception:
            continue
        for host, name, blob, path, secure, httponly, exp in rows:
            value = decrypt(blob)
            if not value:
                continue
            expires = -1  # -1 = 会话 cookie
            if exp and exp > 0:
                expires = int(exp / 1_000_000 - 11644473600)  # chromium 纪元 -> unix
                if expires < 0:
                    expires = -1
            cookies.append({
                "name": name, "value": value, "domain": host, "path": path or "/",
                "secure": bool(secure), "httpOnly": bool(httponly), "expires": expires,
            })
    if not cookies:
        return [], "客户端 Cookie 解密后为空 (客户端可能未登录或已清空数据)"
    return cookies, f"解密成功 {len(cookies)} 条"


def checkin(start_url: str, action: str) -> int:
    """run / silent / silent-poll 三条路径的共用实现。

    凭据源自动选择: 客户端凭据 (macOS 优先, 零配置) -> 网页登录态 data/profile。
    """
    client_cookies, client_reason = read_client_cookies()
    use_client = bool(client_cookies)
    log(f"凭据模式: {'客户端凭据' if use_client else '网页登录态'} ({client_reason})")

    if not use_client:
        has_profile = PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir())
        if not has_profile:
            shot = None
            log(f"无可用凭据: {client_reason}; 且 data/profile 不存在")
            emit({"result": "NO_CREDENTIAL", "report": REPORTS["NO_CREDENTIAL"],
                  "detail": client_reason}, action)
            notify("豆包签到", "无可用凭据: 需要豆包客户端或手动 login")
            return 5

    with sync_playwright() as p:
        if use_client:
            # 客户端凭据模式: 普通 context + 注入 Cookie + 客户端标识 UA
            browser = p.chromium.launch(headless=True, channel="chromium")
            ctx = browser.new_context(
                user_agent=CLIENT_UA,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
            )
            ctx.add_cookies(client_cookies)
        else:
            # 网页登录态模式: 持久化 profile
            browser = None
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chromium",
                headless=True,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
            )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(20000)

            log(f"打开页面: {start_url}")
            if not goto_with_retry(page, start_url):
                emit({"result": "NETWORK", "report": REPORTS["NETWORK"]}, action)
                notify("豆包签到", "网络不可达, 稍后轮询会自动补签")
                return 3

            page.wait_for_timeout(3000)

            # 客户端专属活动检测: 页面出现拦截文案说明当前通道未通过客户端校验
            # (正常只会在网页登录态模式发生; 客户端凭据模式带 SamanthaDoubao UA 不应触发)
            body = page_text(page)
            if "仅支持在豆包电脑版" in body:
                shot = SHOT_DIR / f"client-only-{datetime.datetime.now():%Y%m%d-%H%M%S}.png"
                SHOT_DIR.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot))
                log("活动仅限豆包电脑版客户端参与, 当前通道被拦截")
                emit({"result": "CLIENT_ONLY", "report": REPORTS["CLIENT_ONLY"],
                      "mode": "client" if use_client else "profile",
                      "screenshot": str(shot)}, action)
                notify("豆包签到", "活动仅限电脑版客户端参与, 当前通道被拦")
                return 4

            # 登录态检查: 未登录时页面一般会出现「扫码登录」字样
            if "扫码登录" in body or ("登录" in body and "立即领取" not in body and "签到" not in body):
                shot = SHOT_DIR / f"need-login-{datetime.datetime.now():%Y%m%d-%H%M%S}.png"
                SHOT_DIR.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot))
                if use_client:
                    hint = "客户端登录态失效: 请打开豆包电脑版重新登录一次"
                else:
                    hint = f"登录态失效, 请先运行: python {pathlib.Path(__file__).name} login"
                log(f"{hint} (截图: {shot})")
                emit({"result": "AUTH_ERROR", "report": REPORTS["AUTH_ERROR"],
                      "mode": "client" if use_client else "profile",
                      "detail": hint, "screenshot": str(shot)}, action)
                notify("豆包签到", "登录态失效, 请重新登录")
                return 2

            # 幂等前置: 未进点击流程前先判已签 (误点按钮可能触发不可逆动作)
            pre = check_success(page)
            if pre == "already":
                shot = SHOT_DIR / f"result-{datetime.datetime.now():%Y%m%d-%H%M%S}.png"
                SHOT_DIR.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot), full_page=False)
                log(f"幂等前置命中: 今日已签过, 跳过点击 (截图: {shot})")
                emit({"result": "already", "report": REPORTS["already"],
                      "mode": "client" if use_client else "profile",
                      "needs_attention": False, "screenshot": str(shot)}, action)
                notify("豆包签到", "今天已经签过啦")
                return 0

            clicked = find_and_click(page)
            page.wait_for_timeout(2500)

            # 首页没找到按钮时, 尝试点进「免费领额度/积分」相关入口再来一次
            if not clicked:
                for entry in ["免费领额度", "领额度", "积分"]:
                    if budget_exhausted():
                        break
                    try:
                        loc = page.get_by_text(entry, exact=False)
                        if loc.count() and loc.first.is_visible():
                            loc.first.click(timeout=5000)
                            log(f"进入入口: 「{entry}」")
                            page.wait_for_timeout(3000)
                            clicked = find_and_click(page)
                            if clicked:
                                break
                    except Exception:
                        continue

            result = check_success(page)
            shot = SHOT_DIR / f"result-{datetime.datetime.now():%Y%m%d-%H%M%S}.png"
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shot), full_page=False)
            log(f"签到结果: {result} (截图: {shot})")

            out = {"result": result, "report": REPORTS[result],
                   "mode": "client" if use_client else "profile",
                   "needs_attention": result in NEEDS_ATTENTION_CODES, "screenshot": str(shot)}
            # 轮询模式的空跑(已签过)不写日志, 防止有价值的记录被淹没; 其余结果正常输出
            if action == "silent-poll" and result == "already":
                log("轮询空跑: 今日已签过, 不落盘")
            else:
                emit(out, action)

            if result == "success":
                notify("豆包签到", "签到成功, 额度已到账")
            elif result == "already":
                notify("豆包签到", "今天已经签过啦")
            else:
                notify("豆包签到", f"结果未确认, 请查看截图 {shot.name}")

            return 0 if result in ("success", "already") else 1
        finally:
            try:
                ctx.close()
                if browser is not None:
                    browser.close()
            except Exception:
                pass


def login() -> int:
    """有头模式打开豆包, 让用户手动登录, 登录态存入 data/profile (备用通道)。"""
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chromium",
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(START_URL, wait_until="domcontentloaded")
        print("请在打开的窗口里登录豆包账号。")
        print("注意: 签到活动目前仅限豆包电脑版客户端参与, 网页登录态可能被拦截;")
        print("已安装豆包电脑版并登录过的 macOS 用户无需此步, run 会自动使用客户端凭据。")
        print("登录成功(能看到对话页/头像)后, 回到这里按回车退出, 登录态会保存下来。")
        input("登录完成后按回车 > ")
        ctx.close()
    log("登录态已保存")
    print("登录态已保存, 之后可直接运行: python checkin.py run")
    return 0


def doctor() -> int:
    """离线自检: 不联网、不签到, 逐项检查运行环境, 输出 JSON + 可读清单。

    检查项:
      1. playwright 包可导入
      2. chromium 完整版可定位 (channel="chromium")
      3. 浏览器可实际启动 (launch 后立即关闭, 不访问网络)
      4. 客户端凭据可用性 (macOS: 钥匙串密钥 + Cookie 库 + 解密, 全程不打印值)
      5. 网页登录态 profile 是否存在 (存在≠有效, 仅提示)
      6. data 目录与日志文件可写 (launchd/pythonw 静默运行时落盘的唯一通道)
    """
    checks = []
    ok_all = True

    def add(name: str, ok: bool, detail: str) -> None:
        nonlocal ok_all
        checks.append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            ok_all = False

    # 1. playwright 包
    try:
        import playwright  # noqa: F401
        add("playwright", True, "包可导入")
    except Exception as exc:
        add("playwright", False, f"未安装或导入失败: {exc}; 安装: pip install playwright")

    # pycryptodome (客户端凭据模式依赖, 缺失不阻断: 可回落 login 模式)
    try:
        import Crypto  # noqa: F401
        add("pycryptodome", True, "包可导入 (客户端凭据解密可用)")
    except Exception:
        add("pycryptodome", False, "未安装, 客户端凭据模式不可用; 安装: pip3 install pycryptodome")

    # 2+3. chromium 可定位并可启动 (不联网)
    if checks[0]["ok"]:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                exe = p.chromium.executable_path
                exe_ok = pathlib.Path(exe).exists()
                add("chromium", exe_ok, f"可执行文件: {exe}" if exe_ok else f"路径不存在: {exe}")
                if exe_ok:
                    try:
                        browser = p.chromium.launch(headless=True, channel="chromium")
                        browser.close()
                        add("browser-launch", True, "浏览器可正常启动")
                    except Exception as exc:
                        add("browser-launch", False, f"启动失败: {exc}")
        except Exception as exc:
            add("chromium", False, f"定位失败: {exc}")

    # 4. 客户端凭据 (macOS)
    cookies, reason = read_client_cookies()
    add("client-credential", bool(cookies), reason)

    # 5. 网页登录态 profile (存在≠有效)
    has_profile = PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir())
    add("login-profile", has_profile,
        "data/profile 存在 (备用通道)" if has_profile
        else "data/profile 不存在 (有客户端凭据时无需此步; 否则运行 login)")

    # 6. data 目录与日志可写
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("data-writable", True, f"{DATA_DIR} 可写")
    except Exception as exc:
        add("data-writable", False, f"不可写: {exc} (放 ~/Documents 等受 TCC 保护的目录会导致此问题)")

    # 总评: client-credential 或 login-profile 至少一个可用即可签到;
    # pycryptodome 缺失不单独判失败 (可回落 login 模式), 已由 client-credential 体现
    cred_ok = bool(cookies) or has_profile
    hard_checks = [c for c in checks if c["check"] not in ("login-profile", "pycryptodome")]
    ok_all = cred_ok and all(c["ok"] for c in hard_checks)
    out = {"result": "ok" if ok_all else "FAIL", "report": "自检通过" if ok_all else "自检发现问题, 见 checks",
           "credential_mode": "client" if bool(cookies) else ("profile" if has_profile else "none"),
           "needs_attention": not ok_all, "checks": checks}
    emit(out, "doctor")

    # 人读版
    mark = {True: "✓", False: "✗"}
    for c in checks:
        print(f"  {mark[c['ok']]} {c['check']}: {c['detail']}")
    print(f"  凭据模式: {out['credential_mode']}")
    return 0 if ok_all else 1


def main() -> int:
    # argparse 之前不放任何可能抛异常的逻辑; 之后所有路径都收敛到受保护的分支
    ap = argparse.ArgumentParser(description="豆包自动签到")
    ap.add_argument(
        "action",
        choices=["login", "run", "silent", "silent-poll", "doctor"],
        help="run=签到(stdout), silent=签到(写日志), silent-poll=轮询补签(空跑不落盘), "
             "doctor=离线自检, login=手动登录(备用)",
    )
    ap.add_argument("--url", default=ACTIVITY_URL, help="签到活动页直链 (默认 agent-explore)")
    args = ap.parse_args()
    if args.action == "login":
        return login()
    if args.action == "doctor":
        try:
            return doctor()
        except Exception as exc:
            log(f"未预期异常: {type(exc).__name__}: {exc}")
            emit({"result": "ERROR", "report": f"运行异常: {type(exc).__name__}: {exc}",
                  "needs_attention": True}, "doctor")
            return 1
    try:
        return checkin(args.url, args.action)
    except Exception as exc:
        # 兜底: 任何未预期异常也要留痕, 不能无声消失
        log(f"未预期异常: {type(exc).__name__}: {exc}")
        emit({"result": "ERROR", "report": f"运行异常: {type(exc).__name__}: {exc}",
              "needs_attention": True}, args.action)
        return 1


if __name__ == "__main__":
    sys.exit(main())
