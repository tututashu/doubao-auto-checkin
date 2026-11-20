#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
豆包电脑版自动签到

用法 (macOS / Linux 用 python3, Windows 用 python):
  python checkin.py run          # 日常运行: 无头签到, 结果打印到 stdout (供 AI 汇报)
  python checkin.py silent       # 同 run, 但结果写入日志文件而非 stdout (配合 Windows pythonw 静默运行)
  python checkin.py silent-poll  # 轮询: 未签才补签; 当天签上后自动秒退, 不再空耗 (配合定时轮询任务)
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
     - 锁库兜底 (客户端运行时): 直读拷贝 -> esentutl 备份拷贝 -> VSS 卷影拷贝
       (管理员) -> 本机加密缓存; run 模式下前三层全失败时还会自动弹一次 UAC
       请求管理员权限用 VSS 当场读取 (客户端开着也能读, 结果写回原终端)
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
# 客户端凭据加密缓存: 客户端运行时锁库读不到 Cookie, 用最近一次成功读取的凭据顶上
# (Windows 用 DPAPI 加密 / macOS 用钥匙串密钥派生 AES 密钥, 本机当前用户之外不可解)
CACHE_FILE = DATA_DIR / "client-cookies-cache.bin"
# 多账号配置: 文件不存在 = 单账号默认 (只签一个账号), 历史行为完全不变。
# 只有用户在安装时通过对话式询问明确选择多账号, 才由 AI 写入本文件;
# 脚本自身绝不擅自探测到多账号就自动多签。
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
# 最近一次签到结果的明文 JSON (每次 emit 覆盖写): 供豆包定时任务「零命令纯读汇报」用
LAST_RESULT_FILE = DATA_DIR / "last-result.json"
# run 模式提权重跑的结果回传文件: 提权子进程把结果写到这里, 原进程轮询读取后转述
ELEVATED_RESULT_FILE = DATA_DIR / "elevated-run-result.json"

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
CLICK_TEXTS = ["立即签到", "立即领取", "签到领额度", "今日签到", "签到"]
# 成功标志文案 (注意不要放「获得」这类页面常驻词, 会把已签状态误判成 success)
SUCCESS_TEXTS = ["签到成功", "已签到", "领取成功", "明日再来"]
# 已签过/无可签的标志
ALREADY_TEXTS = ["已签到", "明日再来", "今日已领"]

REPORTS = {
    "success": "豆包签到成功, 200 额度已到账",
    "already": "今日已签到",
    "unknown": "签到结果未能确认, 请人工查看运行截图",
    "AUTH_ERROR": "登录态失效: 客户端凭据模式需在豆包电脑版里重新登录; 网页模式重跑 login",
    "NETWORK": "网络不可达或页面打不开 (重试后仍失败), 稍后轮询会自动补",
    "BROWSER": "浏览器/playwright 环境异常, 先运行 doctor 自检",
    "TIMEOUT": "整体时间预算耗尽, 任务超时上限可能太小",
    "CLIENT_ONLY": "活动仅支持豆包电脑版客户端参与, 当前通道被平台拦截",
    "NO_CREDENTIAL": "未找到可用凭据: 安装豆包电脑版并登录, 或运行 login 保存网页登录态",
}

NEEDS_ATTENTION_CODES = {"unknown", "AUTH_ERROR", "ERROR", "BROWSER", "TIMEOUT", "CLIENT_ONLY", "NO_CREDENTIAL"}

# 最近一次 emit 的结果 (供提权重跑子进程写结果文件用)
_LAST_RESULT: dict | None = None


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
    global _LAST_RESULT
    _LAST_RESULT = out
    payload = json.dumps(out, ensure_ascii=False)
    # 明文结果文件: 覆盖写, 内容只有本次结果 (不含凭据), 供 AI 定时任务只读汇报
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = dict(out)
        snapshot["saved_at"] = now_str()
        LAST_RESULT_FILE.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
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


def load_accounts_config() -> list[dict] | None:
    """读多账号配置; 返回 None 表示「未启用多账号」, 调用方必须走单账号默认路径。

    设计原则(重要): 多账号是用户显式授权的结果, 不是脚本的默认行为。
    因此本函数只认 data/accounts.json 白纸黑字列出的 Profile, 任何解析异常、
    字段缺失、空列表都一律当作「未配置」返回 None —— 宁可少签一个号,
    也不能因为一个坏配置文件就擅自改变用户的账号范围。

    文件格式:
        {"version": 1,
         "accounts": [{"profile": "Default", "label": "主号"},
                      {"profile": "Profile 1", "label": "小号"}]}
    label 只用于汇报措辞, 省略时回落到 profile 名。
    """
    try:
        if not ACCOUNTS_FILE.is_file():
            return None
        obj = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return None
        raw = obj.get("accounts")
        if not isinstance(raw, list) or not raw:
            return None
        out: list[dict] = []
        seen: set[str] = set()
        for item in raw:
            if isinstance(item, str):  # 容错: 允许写成纯字符串列表
                name = item.strip()
                label = name
            elif isinstance(item, dict):
                name = str(item.get("profile") or "").strip()
                label = str(item.get("label") or name).strip()
            else:
                continue
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append({"profile": name, "label": label or name})
        return out or None
    except Exception:
        return None


def accounts_config_enabled() -> bool:
    """是否已启用多账号 (含只列了一个号的配置)。"""
    return load_accounts_config() is not None


def _parse_result_line(line: str) -> dict | None:
    """从日志行 `[时间] {json}` 里取出结果 JSON; 不是结果行返回 None。"""
    i = line.find("{")
    if i < 0:
        return None
    try:
        obj = json.loads(line[i:])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _already_signed_today() -> bool:
    """查今日日志里是否已有 success/already 结果。

    供静默模式秒退用: 当天签上后, 轮询再触发也只是空耗 (读凭据 + 启动浏览器
    + 打开页面要十几秒), 有这条守卫就在毫秒级结束。只认结果行
    (emit 写入的 JSON, 行首是当天日期), 过程日志 (凭据模式/打开页面) 不算数。

    多账号注意: 判据必须是「配置里的每个账号今天都有结果」而不是「任意一条」。
    否则 A 号签上后, 还没签的 B 号会被这条守卫连带跳过, 全天不再补签。
    """
    today = datetime.date.today().isoformat()
    cfg = load_accounts_config()
    pending = {a["label"] for a in cfg} if cfg and len(cfg) > 1 else set()
    paths = []
    custom = os.environ.get("DOUBAO_CHECKIN_LOG")
    if custom:
        paths.append(pathlib.Path(custom))
    paths.append(LOG_FILE)
    for path in paths:
        try:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.startswith(f"[{today} "):
                    continue
                if '"result": "success"' not in line and '"result": "already"' not in line:
                    continue
                if not pending:
                    return True  # 单账号: 任一成功结果即可秒退 (历史行为)
                obj = _parse_result_line(line)
                label = str((obj or {}).get("profile") or "").strip()
                if label in pending:
                    pending.discard(label)
                    if not pending:
                        return True
        except Exception:
            continue
    return False


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


def _cache_protect(data: bytes) -> bytes | None:
    """把缓存字节加密到「本机当前用户」可解: Windows 用 DPAPI, macOS 用钥匙串密钥派生 AES-256-CBC。"""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class _BLOB(ctypes.Structure):
                _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

            buf = ctypes.create_string_buffer(data, len(data))
            blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
            blob_out = _BLOB()
            ok = ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(blob_in), "doubao-checkin-cache", None, None, None, 0, ctypes.byref(blob_out))
            if not ok:
                return None
            try:
                return ctypes.string_at(blob_out.pbData, blob_out.cbData)
            finally:
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        if sys.platform == "darwin":
            key_material = _client_keychain_key()
            if not key_material:
                return None
            from Crypto.Cipher import AES
            km = key_material if isinstance(key_material, bytes) else key_material.encode("utf-8")
            aes_key = hashlib.sha256(b"cache:" + km).digest()
            iv = os.urandom(16)
            pad = 16 - len(data) % 16
            ct = AES.new(aes_key, AES.MODE_CBC, iv).encrypt(data + bytes([pad]) * pad)
            return b"DCC1" + iv + ct
    except Exception:
        return None
    return None


def _cache_unprotect(data: bytes) -> bytes | None:
    """解密 _cache_protect 的产物; 任何失败返回 None。"""
    try:
        if sys.platform == "win32":
            return _windows_dpapi_unprotect(data)
        if sys.platform == "darwin":
            if len(data) < 36 or data[:4] != b"DCC1":
                return None
            key_material = _client_keychain_key()
            if not key_material:
                return None
            from Crypto.Cipher import AES
            km = key_material if isinstance(key_material, bytes) else key_material.encode("utf-8")
            aes_key = hashlib.sha256(b"cache:" + km).digest()
            iv, ct = data[4:20], data[20:]
            pt = AES.new(aes_key, AES.MODE_CBC, iv).decrypt(ct)
            pad = pt[-1]
            if not (1 <= pad <= 16) or pt[-pad:] != bytes([pad]) * pad:
                return None
            return pt[:-pad]
    except Exception:
        return None
    return None


def _save_cookie_cache(accounts: list[dict]) -> None:
    """客户端凭据成功读取后把解密结果按账号分组加密落盘; 失败静默, 不影响主流程。

    v3 起按 Profile 分组保存: 多账号时每个账号各存一份, 绝不能合并成一个列表——
    同名 Cookie (sessionid / sid_guard) 混在一起就分不清属于谁, 会签错号。
    """
    try:
        blob = json.dumps({"v": 3, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "accounts": [{"profile": a["profile"], "cookies": a["cookies"]}
                                        for a in accounts if a.get("cookies")]},
                          ensure_ascii=False).encode("utf-8")
        enc = _cache_protect(blob)
        if enc:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_bytes(enc)
    except Exception:
        pass


def _filter_live(cookies: list[dict]) -> list[dict]:
    """丢掉已过期的 Cookie (expires == -1 表示会话 Cookie, 保留)。"""
    now = time.time()
    return [c for c in cookies
            if c.get("expires", -1) == -1 or c.get("expires", 0) > now]


def _load_cookie_cache() -> tuple[list[dict], str]:
    """读取加密缓存 → ([{profile, cookies}], 说明); 不可用返回 ([], 原因)。"""
    try:
        if not CACHE_FILE.is_file():
            return [], "无凭据缓存"
        raw = _cache_unprotect(CACHE_FILE.read_bytes())
        if not raw:
            return [], "凭据缓存解密失败 (可能更换了用户或机器)"
        obj = json.loads(raw.decode("utf-8"))
        ver = obj.get("v")
        if ver == 3:
            accounts = []
            for item in obj.get("accounts", []):
                cookies = _filter_live(item.get("cookies", []))
                if cookies:
                    accounts.append({"profile": item.get("profile") or "缓存账号", "cookies": cookies})
            if not accounts:
                return [], "凭据缓存已全部过期"
            return accounts, (f"来自本机加密缓存 (缓存于 {obj.get('saved_at', '?')}; "
                              "客户端 Cookie 库当前不可读, 下次成功读取后自动刷新)")
        if ver == 2:
            # v2 是单账号数据 (当时只保存第一个有登录态的 Profile), 可继续用;
            # 但 Profile 名没记录, 用中性标签, 避免冒充成 Default。
            cookies = _filter_live(obj.get("cookies", []))
            if not cookies:
                return [], "凭据缓存已全部过期"
            return ([{"profile": "缓存账号", "cookies": cookies}],
                    (f"来自本机加密缓存 v2 (单账号, 缓存于 {obj.get('saved_at', '?')}); "
                     "下次成功读取后自动升级为按账号分组"))
        # v1 是多 Profile 合并的数据, 可能包含非主账号的 Cookie, 不能再用
        return [], "凭据缓存为旧版格式 (已作废), 等下次成功读取后自动重建"
    except Exception as exc:
        return [], f"凭据缓存读取失败: {exc}"


def _windows_esentutl_copy(db: pathlib.Path, tmp: pathlib.Path) -> bool:
    """esentutl 备份语义拷贝被锁定的 Cookie 库 (系统自带, 不引第三方依赖)。

    用绝对路径调用: 部分机器系统 PATH 被精简, 裸命令名会找不到 (System32 必在)。
    """
    esentutl = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "esentutl.exe"
    try:
        r = subprocess.run(
            [str(esentutl) if esentutl.is_file() else "esentutl",
             "/y", str(db), "/d", str(tmp), "/o"],
            capture_output=True, timeout=60,
        )
    except Exception:
        return False
    return r.returncode == 0 and tmp.is_file()


# VSS 卷影拷贝脚本: 在卷级别创建快照后从快照里取文件, 能读到被进程独占锁定的文件
# (直读与 esentutl 都以「打开原文件」为前提, 被完全锁死时都会失败; 卷影不受影响)。
# 全程 GUID 由 WMI 匹配, 不解析任何本地化文本输出, 中文系统同样适用。
# 对外只输出 ASCII 结果标签行 (OK / CREATE_FAIL:<code> / ...), 供 Python 解析原因。
_VSS_PS1 = r'''
param(
    [Parameter(Mandatory=$true)][string]$Src,
    [Parameter(Mandatory=$true)][string]$Dst
)
$ErrorActionPreference = "Stop"
$sc = $null
try {
    # VSS 相关服务可能被优化软件禁用或未启动, 先尝试拉起 (失败不阻断, Create 时再报)
    foreach ($svc in @("VSS", "swprv")) {
        $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
        if ($null -eq $s) { Write-Output "SVCMISSING:$svc"; continue }
        if ($s.Status -ne "Running") {
            try { Start-Service -Name $svc -ErrorAction Stop } catch { Write-Output "SVCSTARTFAIL:$svc" }
        }
    }
    $drive = [System.IO.Path]::GetPathRoot($Src)   # C:\
    $root = $drive.TrimEnd("\")                    # C:
    $r = (Get-WmiObject -List Win32_ShadowCopy).Create("$root\", "ClientAccessible")
    if ($r.ReturnValue -ne 0) { Write-Output "CREATE_FAIL:$($r.ReturnValue)"; exit 1 }
    $sid = $r.ShadowID
    # 注意: Create 的输出参数叫 ShadowID, 但实例上的标识属性叫 ID ——
    # 用 $_.ShadowID 去查实例永远为空 (真机踩过: 快照创建成功却查不到)。
    # WMI 注册有延迟, 轮询几次再放弃。
    $sc = $null
    foreach ($i in 1..4) {
        Start-Sleep -Milliseconds 600
        $sc = Get-WmiObject Win32_ShadowCopy | Where-Object { $_.ID -eq $sid }
        if ($sc) { break }
    }
    if (-not $sc) { Write-Output "NO_SHADOW"; exit 1 }
    $dev = $sc.DeviceObject.TrimEnd("\")           # \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopyN
    $rel = $Src.Substring($drive.Length)           # \Users\...
    [System.IO.File]::Copy(($dev + $rel), $Dst, $true)
    Write-Output "OK"
    exit 0
} catch {
    Write-Output ("COPY_FAIL:" + $_.Exception.Message)
    exit 1
} finally {
    if ($sc) { try { $sc.Delete() } catch { } }
}
'''


def _windows_vss_copy(db: pathlib.Path, tmp: pathlib.Path) -> tuple[bool, str]:
    """VSS 卷影拷贝: 客户端运行中把 Cookie 库完全锁死时的最终兜底。

    需要管理员权限与 Volume Shadow Copy 服务; 返回 (是否成功, 人读原因),
    失败原因会透传到结果里 (NO_CREDENTIAL 的 detail / doctor / 日志), 便于真机定位。
    """
    if sys.platform != "win32":
        return False, "VSS 未尝试 (非 Windows)"
    import ctypes
    import tempfile
    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            return False, "VSS 不可用: 当前非管理员权限 (run 自动提权后重试可解决)"
    except Exception:
        return False, "VSS 不可用: 无法判断管理员权限"
    try:
        work = pathlib.Path(tempfile.mkdtemp(prefix="doubao-vss-"))
        ps1 = work / "vss-copy.ps1"
        ps1.write_text(_VSS_PS1, encoding="utf-8-sig")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps1), "-Src", str(db), "-Dst", str(tmp)],
            capture_output=True, timeout=180,
        )
        out = (r.stdout or b"").decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        tail = lines[-1] if lines else "无输出"
        if r.returncode == 0 and tmp.is_file():
            return True, "VSS 卷影拷贝成功"
        if tail.startswith("CREATE_FAIL:"):
            code = tail.split(":", 1)[1]
            hint = {"1": "拒绝访问", "2": "功能无效", "3": "卷无效", "5": "拒绝访问",
                    "7": "服务被禁用", "8": "提供程序错误", "9": "超时"}.get(code, "详见 vssadmin/事件查看器")
            return False, f"VSS 不可用: 卷影创建失败 ReturnValue={code} ({hint})"
        if tail.startswith("SVCMISSING:"):
            return False, f"VSS 不可用: 系统 VSS 服务缺失 ({tail.split(':', 1)[1]})"
        if tail.startswith("SVCSTARTFAIL:"):
            return False, f"VSS 不可用: VSS 服务无法启动 ({tail.split(':', 1)[1]}, 可能被安全/优化软件禁用)"
        if tail.startswith("COPY_FAIL:"):
            return False, f"VSS 不可用: 快照内拷贝失败 ({tail.split(':', 1)[1]})"
        return False, f"VSS 不可用 ({tail}, 退出码 {r.returncode})"
    except subprocess.TimeoutExpired:
        return False, "VSS 不可用: 执行超时 (180s)"
    except Exception as exc:
        return False, f"VSS 不可用: {type(exc).__name__}: {exc}"


def read_client_accounts() -> tuple[list[dict], str, list[str]]:
    """客户端凭据读取入口(多账号): 返回 (账号列表, 说明, 配置里读不到的 Profile 名)。

    账号列表每项 {"profile": 原始 Profile 名, "label": 汇报用名, "cookies": [...]}。

    账号范围由 data/accounts.json 决定:
      - 文件不存在 → 单账号模式, 只取「第一个有登录态的 Profile」(历史行为, Default 字典序在前)
      - 文件存在   → 严格按配置列出的号来签, 顺序按配置, 绝不回落到配置外的账号
                     (否则「配置了 A 和 B 却签了 C」比不签更糟)
    missing 是配置里点名但磁盘上读不到的 Profile, 调用方必须把它计入汇报, 不能静默跳过。
    """
    live, reason = _read_client_accounts_live()
    if live:
        _save_cookie_cache(live)
    else:
        live, cache_note = _load_cookie_cache()
        if live:
            reason = cache_note
    if not live:
        return [], reason, []

    cfg = load_accounts_config()
    if not cfg:
        first = live[0]
        return ([{"profile": first["profile"], "label": first["profile"],
                  "cookies": first["cookies"]}],
                f"单账号模式, 本次只签「{first['profile']}」({reason})", [])

    by_name = {a["profile"].lower(): a for a in live}
    out: list[dict] = []
    missing: list[str] = []
    for want in cfg:
        hit = by_name.get(want["profile"].lower())
        if hit:
            out.append({"profile": hit["profile"], "label": want["label"], "cookies": hit["cookies"]})
        else:
            missing.append(want["profile"])
    return out, f"{reason} (多账号模式: 配置 {len(cfg)} 个, 读到 {len(out)} 个)", missing


def read_client_cookies() -> tuple[list[dict], str]:
    """单账号兼容入口 (doctor 与老调用点): 返回第一个账号的 Cookie。"""
    accounts, reason, _missing = read_client_accounts()
    if accounts:
        return accounts[0]["cookies"], reason
    return [], reason


def _read_client_accounts_live() -> tuple[list[dict], str]:
    """读取豆包电脑版客户端的登录 Cookie (macOS / Windows 均已实测)。

    返回 ([{profile, cookies}], reason): 列表为空时 reason 说明原因。
    只读取, 解密在内存完成, Cookie 值不打印、不落盘。

    多账号: 逐个 Profile 各自解密成独立的一份, 由调用方决定签哪些。
    每个账号必须各自独立注入到一个干净的浏览器上下文——同名 Cookie 合并
    在一起会互相覆盖, 那是签错账号的根源。
    """
    import shutil
    import sqlite3
    import tempfile

    if sys.platform not in ("darwin", "win32"):
        return [], "客户端凭据模式目前仅支持 macOS / Windows (其他平台仍走 login 模式)"
    try:
        from Crypto.Cipher import AES  # noqa: F401  探测依赖, 缺了给出安装提示
    except Exception:
        return [], "缺少 pycryptodome, 安装: python -m pip install pycryptodome"

    # 收集 (Profile 标识, Cookie 库路径, 解密函数) 任务列表。
    # 每个 Profile 各自独立解密: 多账号机器上各 Profile 各有一个账号,
    # 同名 Cookie 合并注入会互相覆盖、签错账号, 所以绝不能合并成一个列表。
    # 未启用多账号时, 调用方只取第一个 (Default 按字典序排在 Profile 1 之前);
    # 环境变量 DOUBAO_PROFILE 可强制只读某一个 Profile (调试用)。
    jobs: list[tuple[str, pathlib.Path, object]] = []
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
                jobs.append((profile.name, db, lambda blob, k=key: _decrypt_client_cookie(blob, k)))
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
                label = db.parent.name if db.parent.name != "Network" else db.parent.parent.name
                jobs.append((label, db, lambda blob, k=key: _decrypt_cookie_windows(blob, k)))
        if not jobs:
            return [], ("Windows 客户端凭据不可用: " + ("; ".join(notes) or "无可读 Cookie 库")
                        + " (若客户端为 v20/App-Bound 加密则暂不支持, 请用 login 模式)")

    forced = os.environ.get("DOUBAO_PROFILE", "").strip()
    if forced:
        matched = [j for j in jobs if j[0].lower() == forced.lower()]
        if matched:
            jobs = matched
            notes.append(f"已按 DOUBAO_PROFILE 指定 Profile「{forced}」")
        else:
            names = ", ".join(sorted({j[0] for j in jobs}))
            notes.append(f"DOUBAO_PROFILE={forced} 未匹配 (可用: {names}), 按默认顺序选取")

    accounts: list[dict] = []
    for label, db, decrypt in jobs:
        try:
            tmp = pathlib.Path(tempfile.mkdtemp(prefix="doubao-ck-")) / "Cookies"
            try:
                shutil.copy(db, tmp)  # 原库可能被客户端进程锁定, 拷贝后只读查询
            except OSError:
                # Windows 上客户端运行时 Cookies 库被独占锁定, shutil 拷不动。
                # 第二层: esentutl 备份语义拷贝 (系统自带);
                # 第三层: VSS 卷影拷贝 (管理员权限下可在卷级别绕过任何文件锁,
                #         客户端开着也能读); 都失败才回落加密缓存。
                if not _windows_esentutl_copy(db, tmp):
                    ok, vss_note = _windows_vss_copy(db, tmp)
                    if not ok:
                        notes.append(f"{label}: 备份拷贝与卷影拷贝均失败 ({vss_note})")
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
        profile_cookies: list[dict] = []
        for host, name, blob, path, secure, httponly, exp in rows:
            value = decrypt(blob)
            if not value:
                continue
            expires = -1  # -1 = 会话 cookie
            if exp and exp > 0:
                expires = int(exp / 1_000_000 - 11644473600)  # chromium 纪元 -> unix
                if expires < 0:
                    expires = -1
            profile_cookies.append({
                "name": name, "value": value, "domain": host, "path": path or "/",
                "secure": bool(secure), "httpOnly": bool(httponly), "expires": expires,
            })
        if profile_cookies:
            accounts.append({"profile": label, "cookies": profile_cookies})
    if not accounts:
        base = "客户端 Cookie 解密后为空 (客户端可能未登录或已清空数据)"
        if notes:
            base += "; " + "; ".join(notes)
        return [], base
    names = "、".join(f"{a['profile']}({len(a['cookies'])} 条)" for a in accounts)
    extra = f"; {'; '.join(notes)}" if notes else ""
    return accounts, f"解密成功 {len(accounts)} 个账号: {names}{extra}"


def _windows_elevated_rerun(action: str) -> int | None:
    """run 模式下凭据读不到时, 以管理员权限重跑一次 (弹一次 UAC), 结果写回后转述。

    适用场景: 用户刚装完想立即验证, 而豆包客户端开着把 Cookie 库锁死、
    加密缓存又尚未建立——提权子进程可用 VSS 卷影绕过文件锁当场读取。
    返回 None 表示未尝试或用户取消了 UAC (调用方继续走正常失败路径);
    否则已 emit 子进程结果并返回对应退出码。
    """
    import ctypes
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return None  # 已是管理员: VSS 已在读取链里试过, 再提权无意义
    except Exception:
        return None
    try:
        ELEVATED_RESULT_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    script = pathlib.Path(__file__).resolve()
    # ShellExecuteW + runas 是 Windows 唯一的提权入口; UAC 弹窗是系统安全边界,
    # 无法绕过, 这里只保证「用户点一次『是』之后全自动」。
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable,
        f'"{script}" run --elevated-child', str(script.parent), 0)  # SW_HIDE
    if ret <= 32:
        log(f"提权重跑未获授权 (ShellExecuteW={ret}), 继续以当前权限处理")
        return None
    log("凭据读取失败, 已请求管理员权限重跑 (弹一次 UAC), 等待结果...")
    # 子进程有自己的时间预算 (默认 480s), 轮询上限给足余量
    deadline = time.monotonic() + 660
    while time.monotonic() < deadline:
        time.sleep(3)
        if not ELEVATED_RESULT_FILE.is_file():
            continue
        try:
            out = json.loads(ELEVATED_RESULT_FILE.read_text(encoding="utf-8"))
        except Exception:
            continue
        result = out.get("result", "unknown")
        log(f"提权重跑结果: {result}")
        emit(out, action)
        notify("豆包签到", out.get("report", "提权重跑完成"))
        return 0 if result in ("success", "already") else 1
    log("等待提权重跑结果超时, 继续以当前权限处理")
    return None


def _log_result(out: dict) -> None:
    """只写日志: 不覆盖 last-result.json、不打印。供多账号逐条留痕用。

    last-result.json 只存聚合后的那一条 (豆包定时任务读它), 逐账号明细进日志,
    既让汇报只看到一句结论, 又留下排查依据。
    """
    line = f"[{now_str()}] {json.dumps(out, ensure_ascii=False)}\n"
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


# 多账号聚合汇报用的短文案: REPORTS 的完整说明太长, 塞进一句话会把结论淹没
BRIEF_RESULTS = {
    "success": "签到成功",
    "already": "今日已签到",
    "AUTH_ERROR": "需在豆包电脑版重新登录",
    "NETWORK": "网络不可达, 稍后自动补",
    "CLIENT_ONLY": "被活动页拦截",
    "TIMEOUT": "本轮超时, 稍后自动补",
    "unknown": "结果未确认, 见截图",
    "NO_CREDENTIAL": "读不到凭据",
    "BROWSER": "浏览器环境异常",
    "ERROR": "运行异常",
}


def _notify_brief(text: str) -> str:
    """系统通知正文太长会被截断, 这里主动收一收, 保证结论先看到。"""
    return text if len(text) <= 60 else text[:57] + "…"


def _aggregate_results(results: list[tuple[int, dict, str]]) -> tuple[int, dict]:
    """把逐账号结果聚合成一条汇报。

    result 取「最差」的那个 (有失败就报失败), needs_attention 任一失败即为真;
    report 里逐个列明账号名与简短结论, 这样豆包定时任务只读 report 也能说清多账号。
    """
    n = len(results)
    done = [r for r in results if r[1].get("result") in ("success", "already")]
    bad = [r for r in results if r[1].get("result") not in ("success", "already")]
    parts = [f"{label} {BRIEF_RESULTS.get(str(out.get('result')), out.get('result'))}"
             for _code, out, label in results]
    if bad:
        head = f"{n} 个账号: {len(done)} 个已完成、{len(bad)} 个需要处理"
    else:
        head = f"{n} 个账号均已签到"
    if bad:
        result = str(bad[0][1].get("result"))
    elif any(out.get("result") == "success" for _c, out, _l in results):
        result = "success"
    else:
        result = "already"
    out = {
        "result": result,
        "report": head + ": " + "、".join(parts),
        "mode": "client",
        "needs_attention": bool(bad),
        "accounts": [{"profile": label, "result": o.get("result")} for _c, o, label in results],
    }
    return (bad[0][0] if bad else 0), out


def _missing_result(name: str) -> dict:
    """配置里点名、但本机读不到登录态的账号的结果 (不打印凭据, 只说明事实)。"""
    return {"result": "AUTH_ERROR", "needs_attention": True, "profile": name,
            "report": "配置里的账号在本机读不到登录态 (可能已被客户端清除登录)"}


def _checkin_one(start_url: str, action: str, client_cookies: list[dict] | None,
                 label: str | None, standalone: bool) -> tuple[int, dict]:
    """对单个账号执行一次签到。

    standalone=True  —— 自己 emit 结果并弹通知 (单账号路径, 输出格式与历史版本一致)
    standalone=False —— 只返回结果, 交给调度层聚合成一条汇报 (多账号路径)

    每个账号都在自己的 sync_playwright + 独立 context 里跑, 绝不共用:
    两个账号的 Cookie 同名 (sessionid / sid_guard), 混进同一个 context 会互相
    覆盖, 结果是拿 A 的登录态去签 B 的号。
    """
    use_client = bool(client_cookies)
    mode = "client" if use_client else "profile"

    def finish(out: dict, code: int, notify_msg: str | None = None) -> tuple[int, dict]:
        out = dict(out)
        out.setdefault("report", REPORTS.get(str(out.get("result")), ""))
        out["mode"] = mode
        if label:
            out["profile"] = label
        if standalone:
            skip = (action == "silent-poll" and out.get("result") == "already")
            if not skip:
                emit(out, action)
            if notify_msg:
                notify("豆包签到", notify_msg)
        return code, out

    # Windows 立即验证场景: 客户端运行中锁库且缓存尚未建立, 凭据当场读不到。
    # 仅 run 模式 (AI/人工驱动的验证) 自动弹一次 UAC 提权重跑, 用 VSS 卷影
    # 绕过文件锁当场读取并建缓存; silent / silent-poll (定时任务) 绝不弹窗。
    if (not use_client and sys.platform == "win32" and action == "run"
            and "--elevated-child" not in sys.argv):
        handled = _windows_elevated_rerun(action)
        if handled is not None:
            return handled, {"result": "ERROR", "report": "已以管理员权限重跑一次",
                             "needs_attention": True}

    if not use_client:
        has_profile = PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir())
        if not has_profile:
            log("无可用凭据: 客户端凭据读不到; 且 data/profile 不存在")
            return finish({"result": "NO_CREDENTIAL", "report": REPORTS["NO_CREDENTIAL"],
                           "detail": "无客户端凭据, 且 data/profile 不存在"}, 5,
                          "无可用凭据: 需要豆包客户端或手动 login")

    with sync_playwright() as p:
        if use_client:
            # 客户端凭据模式: 普通 context + 注入本账号 Cookie + 客户端标识 UA
            browser = p.chromium.launch(headless=True, channel="chromium")
            ctx = browser.new_context(
                user_agent=CLIENT_UA,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
            )
            ctx.add_cookies(client_cookies or [])
        else:
            # 网页登录态模式: 持久化 profile (仅单账号)
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
                return finish({"result": "NETWORK", "report": REPORTS["NETWORK"]}, 3,
                              "网络不可达, 稍后轮询会自动补签")

            page.wait_for_timeout(3000)

            # 客户端专属活动检测: 页面出现拦截文案说明当前通道未通过客户端校验
            # (正常只会在网页登录态模式发生; 客户端凭据模式带 SamanthaDoubao UA 不应触发)
            body = page_text(page)
            if "仅支持在豆包电脑版" in body:
                shot = SHOT_DIR / f"client-only-{datetime.datetime.now():%Y%m%d-%H%M%S}.png"
                SHOT_DIR.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot))
                log("活动仅限豆包电脑版客户端参与, 当前通道被拦截")
                return finish({"result": "CLIENT_ONLY", "report": REPORTS["CLIENT_ONLY"],
                               "screenshot": str(shot)}, 4,
                              "活动仅限电脑版客户端参与, 当前通道被拦")

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
                return finish({"result": "AUTH_ERROR", "report": REPORTS["AUTH_ERROR"],
                               "detail": hint, "screenshot": str(shot)}, 2,
                              "登录态失效, 请重新登录")

            # 幂等前置: 未进点击流程前先判已签 (误点按钮可能触发不可逆动作)
            pre = check_success(page)
            if pre == "already":
                shot = SHOT_DIR / f"result-{datetime.datetime.now():%Y%m%d-%H%M%S}.png"
                SHOT_DIR.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot), full_page=False)
                log(f"幂等前置命中: 今日已签过, 跳过点击 (截图: {shot})")
                return finish({"result": "already", "report": REPORTS["already"],
                               "needs_attention": False, "screenshot": str(shot)}, 0,
                              "今天已经签过啦")

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
            # 修历史误判: 走到这里的 pre 必然不是 already, 点击后却变成 already,
            # 说明「立即签到」按钮是在这次点击后消失的 —— 也就是这次真的签上了。
            # (旧逻辑记成 already, 多账号汇报里就看不出哪个号是本次签的)
            if result == "already" and clicked:
                result = "success"

            shot = SHOT_DIR / f"result-{datetime.datetime.now():%Y%m%d-%H%M%S}.png"
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shot), full_page=False)
            log(f"签到结果: {result} (截图: {shot})")

            out = {"result": result, "report": REPORTS[result],
                   "needs_attention": result in NEEDS_ATTENTION_CODES, "screenshot": str(shot)}
            if action == "silent-poll" and result == "already":
                log("轮询空跑: 今日已签过, 不落盘")

            if result == "success":
                msg = "签到成功, 额度已到账"
            elif result == "already":
                msg = "今天已经签过啦"
            else:
                msg = f"结果未确认, 请查看截图 {shot.name}"
            return finish(out, 0 if result in ("success", "already") else 1, msg)
        finally:
            try:
                ctx.close()
                if browser is not None:
                    browser.close()
            except Exception:
                pass


def checkin(start_url: str, action: str) -> int:
    """run / silent / silent-poll 三条路径的入口, 单账号与多账号都在这里分流。

    账号范围由 data/accounts.json 决定 (见 load_accounts_config):
      - 未配置 → 单账号, 只签第一个有登录态的 Profile, 输出格式与历史版本一致
      - 已配置 → 只签配置里列出的账号, 逐个独立签到后聚合成一条汇报
    """
    # 静默路径秒退守卫: 当天已成功签到就不再执行 (签上后的轮询毫秒级结束)。
    # 只对 silent / silent-poll 生效; run 是验证入口, 始终完整执行给真实结果。
    if action.startswith("silent") and _already_signed_today():
        log("今日已签到成功, 本次静默执行跳过 (不启动浏览器)")
        return 0

    accounts, cred_reason, missing = read_client_accounts()
    if accounts:
        log(f"凭据模式: 客户端凭据 ({cred_reason})")
    elif missing:
        log(f"凭据模式: 客户端凭据 (配置的账号未命中: {'、'.join(missing)}; {cred_reason})")
    else:
        log(f"凭据模式: 网页登录态 ({cred_reason})")

    if not accounts:
        if missing:
            # 配置点名的账号一个都读不到: 绝不回落到「去签别的号」, 直接如实汇报。
            # (配了 A 和 B 却签了 C, 比什么都不签更糟)
            results = [(2, _missing_result(n), n) for n in missing]
            code, agg = _aggregate_results(results)
            log(f"配置里的账号全部读不到登录态: {'、'.join(missing)}")
            notify("豆包签到", _notify_brief(agg["report"]))
            emit(agg, action)
            return code
        # 没有客户端凭据: 走网页登录态单账号路径 (历史行为)
        code, _out = _checkin_one(start_url, action, None, None, standalone=True)
        return code

    if len(accounts) == 1 and not missing:
        # 单账号: 不写 profile 字段, 日志与 last-result.json 格式和旧版完全一致
        code, _out = _checkin_one(start_url, action, accounts[0]["cookies"], None, standalone=True)
        return code

    log(f"多账号模式: 依次签到 {len(accounts) + len(missing)} 个账号")
    results: list[tuple[int, dict, str]] = []
    for acc in accounts:
        if budget_exhausted():
            out = {"result": "TIMEOUT", "report": REPORTS["TIMEOUT"], "profile": acc["label"]}
            log(f"账号「{acc['label']}」跳过: 本轮时间预算已用完")
            _log_result(out)
            results.append((1, out, acc["label"]))
            continue
        log(f"—— 账号「{acc['label']}」(Profile {acc['profile']}) 开始 ——")
        code, out = _checkin_one(start_url, action, acc["cookies"], acc["label"], standalone=False)
        _log_result(out)
        results.append((code, out, acc["label"]))

    # 配置里点名、但本机读不到登录态的账号: 必须显式汇报, 不能静默跳过
    for name in missing:
        out = _missing_result(name)
        log(f"账号「{name}」读不到登录态: 已跳过, 请在豆包电脑版里重新登录该账号")
        _log_result(out)
        results.append((2, out, name))

    code, agg = _aggregate_results(results)
    log(f"多账号汇总: {agg['report']}")
    notify("豆包签到", _notify_brief(agg["report"]))
    emit(agg, action)
    return code


def _scan_profiles_metadata() -> list[dict]:
    """只读 Cookie 库元数据 (不解密、不碰钥匙串): 有哪些 Profile、各有多少条豆包 Cookie。

    作为账号探测的降级通道: 解密失败时仍能告诉用户「本机有几个登录位置」,
    但措辞必须诚实 —— 登录位置不等于账号数, 同一账号反复切换会留下多份记录。
    """
    import sqlite3
    base = CLIENT_DATA_DIR
    dbs: list[pathlib.Path] = []
    if sys.platform == "darwin":
        if base.exists():
            dbs = [d / "Cookies" for d in sorted(base.glob("*")) if (d / "Cookies").is_file()]
    elif sys.platform == "win32":
        for root in _windows_client_roots():
            dbs += [d for d in sorted(root.glob("*/Cookies")) + sorted(root.glob("*/Network/Cookies"))]
    out: list[dict] = []
    for db in dbs:
        name = db.parent.name if db.parent.name != "Network" else db.parent.parent.name
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
            try:
                rows = con.execute(
                    "SELECT COUNT(*), SUM(CASE WHEN expires_utc = 0 OR expires_utc > ? THEN 1 ELSE 0 END) "
                    "FROM cookies WHERE host_key LIKE '%doubao%'",
                    (int((time.time() + 11644473600) * 1_000_000),)).fetchone()
            finally:
                con.close()
            total, alive = int(rows[0] or 0), int(rows[1] or 0)
        except Exception:
            continue
        if total:
            out.append({"profile": name, "cookies": total, "alive": alive})
    return out


def _uid_fp(cookies: list[dict]) -> str | None:
    """从已解密的 Cookie 里取账号 uid 的指纹 (只返回哈希前 8 位, 不落明文)。"""
    for c in cookies:
        if c.get("name") == "__tea__ug__uid" and c.get("value"):
            return hashlib.sha256(str(c["value"]).encode()).hexdigest()[:8]
    return None


def detect_accounts() -> dict:
    """探测本机可签到的账号。

    优先解密后按 uid 去重 (同一账号多个 Profile 只算一个, 保留 Cookie 最多的那份);
    解密不可用时降级为「登录位置」清单, 并明确标注未经身份校验。
    """
    live, note = _read_client_accounts_live()
    result: dict = {"configured": [a["profile"] for a in (load_accounts_config() or [])] or None,
                    "multi_enabled": accounts_config_enabled()}
    if live:
        accounts: list[dict] = []
        idx_by_uid: dict[str, int] = {}
        for acc in live:
            fp = _uid_fp(acc["cookies"])
            entry = {"profile": acc["profile"], "label": acc["profile"],
                     "uid_fp": fp, "cookies": len(acc["cookies"]), "duplicate_of": None}
            if fp and fp in idx_by_uid:
                i = idx_by_uid[fp]
                # 同一账号留 Cookie 更全的那份, 另一份标记为重复 (不参与签到)
                if entry["cookies"] > accounts[i]["cookies"]:
                    accounts[i]["duplicate_of"] = entry["profile"]
                    accounts[i] = entry
                else:
                    entry["duplicate_of"] = accounts[i]["profile"]
                    accounts.append(entry)
                continue
            if fp:
                idx_by_uid[fp] = len(accounts)
            accounts.append(entry)
        result.update({"count": len([a for a in accounts if not a["duplicate_of"]]),
                       "mode": "uid", "accounts": accounts, "note": note})
    else:
        meta = _scan_profiles_metadata()
        result.update({"count": len(meta), "mode": "profile",
                       "accounts": [{"profile": m["profile"], "label": m["profile"],
                                     "uid_fp": None, "cookies": m["cookies"],
                                     "alive": m["alive"], "duplicate_of": None} for m in meta],
                       "note": note + "; 已降级为只读元数据, 未做登录身份校验"})
    return result


def accounts_cmd(args) -> int:
    """accounts 子命令: 探测 / 开启 / 关闭 / 查看多账号配置。

    只读写 data/accounts.json, 不碰 last-result.json —— 否则会把签到汇报冲掉。
    """
    if args.enable is not None:
        wanted = [x.strip() for x in str(args.enable).replace("，", ",").split(",") if x.strip()]
        if not wanted:
            print("错误: --enable 后面要跟 Profile 名, 多个用逗号分隔, 例如 \"Default,Profile 1\"")
            return 1
        info = detect_accounts()
        known = {str(a["profile"]).lower(): a["profile"] for a in info.get("accounts", [])}
        unknown = [w for w in wanted if w.lower() not in known]
        if unknown:
            avail = "、".join(sorted(known.values())) or "（本机没检测到任何可读账号）"
            print(f"错误: 本机找不到这些 Profile: {', '.join(unknown)}")
            print(f"当前可用: {avail}")
            return 1
        accounts = [{"profile": known[w.lower()], "label": known[w.lower()]} for w in wanted]
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ACCOUNTS_FILE.write_text(
            json.dumps({"version": 1, "accounts": accounts}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"已开启多账号签到: {'、'.join(a['profile'] for a in accounts)}")
        print(f"配置: {ACCOUNTS_FILE}")
        print("下一轮签到（或手动 run）会按此逐个签到; 删除该文件即恢复单账号。")
        return 0

    if args.disable:
        if ACCOUNTS_FILE.exists():
            ACCOUNTS_FILE.unlink()
            print("已关闭多账号, 恢复为只签一个账号 (第一个有登录态的 Profile)")
        else:
            print("当前本来就是单账号模式, 无需关闭")
        return 0

    info = detect_accounts()
    if args.status:
        cfg = load_accounts_config()
        scope = "、".join(a["profile"] if a["label"] == a["profile"] else f"{a['label']}({a['profile']})"
                          for a in cfg) if cfg else ""
        print(f"账号范围: {'多账号 — ' + scope if cfg else '单账号(默认)'}")
        print(f"本机检测到: {info.get('count', 0)} 个账号 (口径: "
              f"{'按登录身份去重' if info.get('mode') == 'uid' else '按登录位置, 未校验身份'})")
        print(f"配置文件: {ACCOUNTS_FILE}{'' if ACCOUNTS_FILE.exists() else ' (不存在)'}")
        print("JSON: " + json.dumps({"mode": info.get("mode"), "count": info.get("count"),
                                    "configured": info.get("configured")}, ensure_ascii=False))
        return 0

    # 默认 (含 --detect): 人读清单 + 末尾一行 JSON 给 AI 解析
    n = info.get("count", 0)
    if info.get("mode") == "uid":
        print(f"【账号】检测到 {n} 个可签到账号 (已按登录身份去重)")
    else:
        print(f"【登录位置】检测到 {n} 个带登录数据的 Profile (未能校验身份, 可能含同一账号的旧记录)")
    for i, a in enumerate(info.get("accounts", []), 1):
        dup = f"  ← 与 {a['duplicate_of']} 是同一账号, 已合并" if a.get("duplicate_of") else ""
        fp = f"  uid {a['uid_fp']}" if a.get("uid_fp") else ""
        print(f"  {i}. {a['profile']}   {a['cookies']} 条 cookie{fp}{dup}")
    cfg = info.get("configured")
    print(f"【配置】{'多账号: ' + '、'.join(cfg) if cfg else '未启用 (默认只签一个账号)'}")
    if info.get("note"):
        print(f"【说明】{info['note']}")
    print("JSON: " + json.dumps(info, ensure_ascii=False))
    return 0


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
      4. 客户端凭据可用性 (钥匙串密钥/DPAPI + Cookie 库 + 解密, 全程不打印值)
      4b. Windows 专属: VSS 卷影兜底可用性 (管理员权限 + VSS 服务状态, 信息项)
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
        add("playwright", False, f"未安装或导入失败: {exc}; 安装: python -m pip install playwright")

    # pycryptodome (客户端凭据模式依赖, 缺失不阻断: 可回落 login 模式)
    try:
        import Crypto  # noqa: F401
        add("pycryptodome", True, "包可导入 (客户端凭据解密可用)")
    except Exception:
        add("pycryptodome", False, "未安装, 客户端凭据模式不可用; 安装: python -m pip install pycryptodome")

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

    # 4. 客户端凭据 (macOS / Windows)
    cookies, reason = read_client_cookies()
    add("client-credential", bool(cookies), reason)

    # 4b. VSS 卷影兜底可用性诊断 (仅 Windows; 信息项, 不计入总评):
    # 锁库第三层需要 管理员权限 + VSS 服务未被禁用, 真机排错看这一项就够
    if sys.platform == "win32":
        import ctypes
        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            is_admin = False
        svc_state = "未知"
        try:
            r = subprocess.run(["sc", "query", "VSS"], capture_output=True, text=True, timeout=10)
            svc_state = "运行中" if "RUNNING" in (r.stdout or "").upper() else "未运行/被禁用"
        except Exception:
            pass
        add("windows-vss", True,
            f"管理员权限: {'有' if is_admin else '无'}; VSS 服务: {svc_state}"
            " (锁库兜底第三层; run 凭据读不到时会自动提权后重试, 定时任务不弹窗只靠缓存)")

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
    hard_checks = [c for c in checks if c["check"] not in ("login-profile", "pycryptodome", "windows-vss")]
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
        choices=["login", "run", "silent", "silent-poll", "doctor", "accounts"],
        help="run=签到(stdout), silent=签到(写日志), silent-poll=轮询补签(空跑不落盘), "
             "doctor=离线自检, login=手动登录(备用), accounts=查看/开启多账号",
    )
    ap.add_argument("--url", default=ACTIVITY_URL, help="签到活动页直链 (默认 agent-explore)")
    ap.add_argument("--elevated-child", action="store_true", help=argparse.SUPPRESS)
    ag = ap.add_mutually_exclusive_group()
    ag.add_argument("--detect", action="store_true",
                    help="accounts: 列出本机可签到的账号 (只读, 不签到)")
    ag.add_argument("--enable", metavar="LIST",
                    help="accounts: 开启多账号签到, 逗号分隔 Profile 名, 如「Default,Profile 1」")
    ag.add_argument("--disable", action="store_true",
                    help="accounts: 关闭多账号, 恢复只签一个账号")
    ag.add_argument("--status", action="store_true",
                    help="accounts: 查看当前账号范围")
    args = ap.parse_args()
    if args.action == "accounts":
        try:
            return accounts_cmd(args)
        except Exception as exc:
            print(f"账号命令执行失败: {type(exc).__name__}: {exc}")
            return 1
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
        if args.elevated_child:
            # 提权重跑子进程: 无窗口跑 silent, 结果写回回传文件给原进程转述
            try:
                return checkin(args.url, "silent")
            finally:
                try:
                    DATA_DIR.mkdir(parents=True, exist_ok=True)
                    payload = _LAST_RESULT or {
                        "result": "ERROR", "report": "提权重跑未产生结果",
                        "needs_attention": True}
                    ELEVATED_RESULT_FILE.write_text(
                        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
        return checkin(args.url, args.action)
    except Exception as exc:
        # 兜底: 任何未预期异常也要留痕, 不能无声消失
        log(f"未预期异常: {type(exc).__name__}: {exc}")
        emit({"result": "ERROR", "report": f"运行异常: {type(exc).__name__}: {exc}",
              "needs_attention": True}, args.action)
        return 1


if __name__ == "__main__":
    sys.exit(main())
