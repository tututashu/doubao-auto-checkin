<div align="center">

# 🤖 doubao-auto-checkin

**每天自动领取豆包电脑版签到额度的小脚本**

[![版本](https://img.shields.io/badge/%E7%89%88%E6%9C%AC-v1.0.0-1F6FEB?style=flat&logo=github&logoColor=white)](https://github.com/tututashu/doubao-auto-checkin)
[![License: MIT](https://img.shields.io/badge/License-MIT-8B5CF6?style=flat&logo=opensourceinitiative&logoColor=white)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows-D85A30)]()
[![Stars](https://img.shields.io/github/stars/tututashu/doubao-auto-checkin?style=flat&logo=github&logoColor=white&color=F59E0B)](https://github.com/tututashu/doubao-auto-checkin/stargazers)
[![Author](https://img.shields.io/badge/Author-tututashu-10B981?style=flat&logo=github&logoColor=white)](https://github.com/tututashu)

</div>

> 每天签到领 200 额度，周一额度翻倍，连签 30 天可得 8800 —— 入口藏在豆包电脑版首页左下角，忘了签就白丢。
> 这个工具让系统定时器（或豆包定时任务）每天到点自动签，全程无感。装了豆包电脑版并登录过即可**零配置**：脚本直接复用客户端登录凭据，无需扫码。
>
> 👤 作者：[tututashu](https://github.com/tututashu) · 📦 仓库：[github.com/tututashu/doubao-auto-checkin](https://github.com/tututashu/doubao-auto-checkin)

> [!TIP]
> **⭐ 顺手点个 Star 再往下看**——客户端凭据读取依赖客户端本地的数据格式与活动页校验逻辑，豆包升级改一版它就可能失效，修复都会第一时间推到这里。Star 一下，等哪天连签莫名其妙断了，你能一秒翻回这个仓库。

---

## ✨ 特性

| | 特性 |
|:---:|---|
| ✨ | **零配置** —— 装了豆包电脑版并登录过即可直接用：脚本自动从客户端读取登录凭据，**无需扫码**（见[工作原理](#%EF%B8%8F-工作原理)） |
| 🪄 | **AI 一键配置** —— 把仓库链接发给豆包电脑版，它自己 clone、测试、建豆包定时任务、装系统级轮询，装完即全套 |
| ♻️ | **幂等安全** —— 先查状态，未签才领；重复运行、多任务并发都不会重复签到 |
| 📦 | **单文件** —— 签到逻辑完全自包含在一个 `checkin.py` 里 |
| ⏰ | **双定时组件** —— 豆包定时任务（到点自动签 + AI 汇报）与系统级定时（每 4 小时轮询 + 开机补跑、零 Token）可自由组合，AI 一键配置默认两个都装 |
| 🔁 | **错过后补跑** —— 定时方案自带兜底轮询 + 开机补跑，当天 24 点前补到都有效 |
| 📣 | **智能汇报** —— 一行 JSON，`report` 字段直接是人话；`needs_attention` 标记需要处理的情况 |
| 🛡️ | **自检** —— `python checkin.py doctor` 离线体检：playwright / chromium / 浏览器启动 / 凭据解密 / 目录可写，一次查清 |
| 🌐 | **网络重试** —— 页面打不开按 5/15/30 秒退避重试，覆盖「刚开机 WiFi 还没就绪」的窗口，受时间预算约束不会无限耗 |
| 📸 | **留痕** —— 每次运行截图存档，签到失败能直接看到豆包页面长什么样 |
| 🔐 | **无密钥** —— 仓库不含任何凭据，凭据解密在本机内存完成、不落盘，不会上传 |

---

## 📋 前置条件

- ✅ 已安装并**登录过豆包电脑版**（脚本靠客户端本地数据鉴权，凭据解密已在真机实测）
- ✅ 本机有 **Python 3.10+**（代码用了 `str | None` 类型语法，3.9 及以下会语法报错）
  - Windows 注意：必须是**独立安装的系统 Python**（python.org 安装器或 winget）。AI 运行时/沙箱内置的 Python 及其环境会随会话重置，装好的依赖留不住，不能替代
- ⬜ Python 包：`playwright`（必需）+ `pycryptodome`（客户端凭据解密用）——没装也没关系，AI 一键流程会自动装

---

## ⏰ 每日定时自动化

脚本依赖本机的登录凭据，因此定时必须跑在本机。提供**两个可自由组合的组件**——不是二选一，按需求拼装即可（脚本幂等，重复触发不会重复领取）：

- **模式 A · 豆包定时任务**：到点由豆包触发、跑脚本、一句话汇报到聊天记录，代价是每次一次模型调用；但豆包的调度器没有「错过补跑」，到点没开机当天就错过了
- **模式 B · 系统级定时**：系统调度器直接跑脚本，**自带每 4 小时轮询 + 开机补跑**，零 Token、无聊天记录

### 常见搭配

| 你的需求 | 配法 |
|---|---|
| **既要 AI 汇报，又要轮询兜底**（推荐，**AI 一键配置的默认装法**） | A 里只建**一条** 09:20 签到任务 + B 整套装一次：汇报来自 A，4 小时轮询和开机补跑来自 B，幂等共存互不冲突 |
| 只要静默签到，不要聊天记录 | 只装 B |
| 只要 AI 汇报，能接受错过就错过 | 只建 A 的那条 09:20 任务 |

> [!NOTE]
> **不存在选择题**：用下面的懒人指令让 AI 配置时，它会默认把 A 和 B **都装好**（即第一行的推荐搭配），不会也不该问你「选 A 还是 B」。如果哪个 AI 拿这个问题来问你，直接回「都装」即可。

| 对比项 | 模式 A：豆包定时任务 | 模式 B：系统级定时 |
|:---:|---|---|
| **原理** | 豆包定时任务触发 → 豆包跑脚本 → 一句话汇报 | 系统定时器直接跑脚本 → 写日志 + 通知 |
| **Token 消耗** | 每次消耗一次模型调用 | **零** |
| **聊天记录** | 每天一条签到汇报 | **零** |
| **日志** | 在聊天记录里 | 独立日志文件 `data/checkin.log` |
| **设置难度** | 低（一句话让豆包全配好） | 低（一条命令，全自动） |

### 模式 A：豆包定时任务（签到 + 汇报）

**懒人一键**：打开豆包电脑版 → 切到「工作任务模式」（执行环境选**本地电脑**）→ 发送：

```text
帮我把这个仓库 clone 到 ~/doubao-auto-checkin，然后严格按照仓库里的 SETUP.md 完成自动签到配置：
https://github.com/tututashu/doubao-auto-checkin
```

> [!NOTE]
> 这条是**一次性**的设置指令。豆包会依次完成：安装依赖 → 测试签到（装有豆包客户端时自动读取客户端凭据，无需扫码）→ 创建每天 09:20 的定时任务 → 安装系统级定时（每 4 小时轮询 + 开机补跑）→ 汇报结果。两个定时缺一不可，装完即是完整的「签到 + 汇报 + 轮询兜底」全套，无需再手动补任何配置。

**手动配置**：

1. clone 本仓库，确认 `python3 checkin.py run` 能跑通（装有豆包客户端则无需登录；否则先 `python3 checkin.py login`）
2. 豆包电脑版 → 左侧栏「定时任务」→ 新建
3. 把 `doubao-task-prompt.txt` 内容粘贴进去，`<仓库目录>`、`<python3>` 替换为你的实际路径
4. 执行时间每天 09:20（错开豆包高峰期整点），重复周期「每天」，勾选重复执行，执行环境选「本地电脑」
5. 先「立即执行」跑 1-2 次验证，能正常汇报后再放任定时跑

> [!NOTE]
> **为什么签到逻辑不写在提示词里？** 定时任务里的 AI 每次执行都可能有偏差，把确定性的事（找按钮、点签到）交给脚本，AI 只负责「跑命令 + 汇报」，稳定得多。

**想要轮询兜底？** 不用再建第二条豆包任务——装一次下面的模式 B 就行，它自带每 4 小时轮询和开机补跑，与模式 A 的签到任务幂等共存。

> [!NOTE]
> 模式 A 每次运行会消耗一次 AI 模型调用并产生一条聊天记录。签到逻辑本身是确定性代码，模型仅负责「跑命令 + 汇报」。

---

### 模式 B：系统级定时（轮询 + 补跑）

用系统自带的定时器直接运行脚本，**完全不经过 AI 模型**：零 Token、无窗口，且自带每 4 小时轮询和开机补跑。既可以单独用（纯静默签到），也可以和模式 A 搭配（A 管汇报、B 管兜底）：

- 每天到点静默签到，结果写日志文件（Windows 为 `data\checkin.log`，macOS 由 launchd 收进 `data/launchd.log`），无聊天记录、无弹窗（macOS 另有系统通知）
- 自带兜底轮询 + 开机补跑，关机错过下次开机会补上
- 纯系统级，不依赖模型可用性

**第 1 步 · 拿到脚本**（本地已有仓库就跳过）：

```bash
git clone https://github.com/tututashu/doubao-auto-checkin.git
cd doubao-auto-checkin
pip3 install -i https://pypi.tuna.tsinghua.edu.cn/simple playwright pycryptodome
```

> [!TIP]
> Playwright 下载浏览器走官方 CDN 可能超时，国内网络可改用镜像（实测约 4 分钟完成）：
> ```powershell
> $env:PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright/"
> python -m playwright install chromium
> ```

**第 2 步 · 一键安装**（Windows 在 PowerShell 里运行，macOS 在终端里运行）：

```powershell
# Windows：自动探测 pythonw.exe、检查依赖并注册任务
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
```

```bash
# macOS：自动探测真实 python3 路径并生成 launchd 配置
./install.sh install
```

装好后的触发完全一致：

| 触发 | 频率 | 干什么 |
|---|---|---|
| 每日定时 | 每天 09:20 | 正常签到（Windows 结果写 `data\checkin.log`；macOS 输出进 `data/launchd.log`） |
| 兜底轮询 | 每 4 小时（Windows 为 01/05/09/13/17/21 六个独立触发） | 已签自动收手，没签就补上 |
| 开机/登录补跑 | 每次开机/登录 | 补上关机、睡眠期间错过的签到——凌晨 01/05 点的轮询撞上关机时，开机进桌面这一下立即补上 |

三个触发跑的都是同一个幂等脚本——09:20 那次撞上关机、睡眠，或刚开机网络没就绪，轮询都会补上，当天 24 点前补到都有效。

> [!TIP]
> **Windows 权限问题已自动化**：`install-windows.ps1` 先用普通权限直接注册（标准用户大多能成功，全程零弹窗）；仅当系统拒绝（实测非管理员调 `Register-ScheduledTask` 会**静默失败**——返回成功但任务不存在，`schtasks` 也失败）时，才自动弹一次 UAC 提权重试。注册后脚本必定回读校验，任务不存在就不会报成功。提权只用于安装，定时任务本身仍以普通权限运行；任务运行时若客户端开着锁库，由加密缓存兜底（首次缓存由安装验证或客户端关闭窗口期自动建立）。

> [!NOTE]
> 无窗口定时运行**在 Windows 上用的是 `silent` / `silent-poll` 参数**：结果写入 `data/checkin.log`（无窗口下 stdout 是黑洞，只有 silent 类命令会写日志），已签过的轮询空跑不落盘。macOS 的 launchd 任务以 `run` 运行、stdout 由 launchd 收进 `data/launchd.log`（过程日志同样在 `data/checkin.log`）。结果日志路径均可用环境变量 `DOUBAO_CHECKIN_LOG` 覆盖；macOS 每次签到同时弹系统通知。

**卸载**：

```bash
./install.sh uninstall          # macOS
```

```powershell
# Windows (PowerShell):
Unregister-ScheduledTask -TaskName "DoubaoAutoSignin" -Confirm:$false
Unregister-ScheduledTask -TaskName "DoubaoCheckinPoll" -Confirm:$false
```

> [!IMPORTANT]
> 脚本不要放在 `~/Documents`、`~/Desktop`、`~/Downloads` 下——macOS 的隐私保护（TCC）会拦截后台进程读取这些目录，报 `Operation not permitted`；Windows 同理避免放受控文件夹。推荐放 `~/Library/Application Support/` 或任意普通目录。

---

## 🛠️ 手动运行（调试用）

想先手动跑一次、确认脚本可用：

```bash
git clone https://github.com/tututashu/doubao-auto-checkin.git
cd doubao-auto-checkin
python3 checkin.py run
```

看到 `签到成功` 或 `今日已签到` 的汇报，就说明跑通了。

> [!NOTE]
> 下文命令以 `python3` 为例，Windows 手动调试时换成 `python` 即可；让 AI 代配时遵循 SETUP.md 第 0 步的绝对路径规则（禁裸 `python`）。

<details>
<summary>📖 全部命令</summary>

```bash
python3 checkin.py run          # 执行一次签到, 结果打印到 stdout (一行 JSON)
python3 checkin.py silent       # 同 run, 但结果写入 data/checkin.log (配合定时任务静默运行)
python3 checkin.py silent-poll  # 轮询补签: 已签过的空跑不写日志 (配合轮询任务)
python3 checkin.py doctor       # 离线自检: 不联网, 逐项体检 (环境/浏览器/凭据/目录可写)
python3 checkin.py login        # 备用: 手动登录保存网页登录态 (装有豆包客户端则无需此步)
./install.sh test               # 同 run, 顺便验证 launchd 所用 python 是否可用
```

</details>

`run` 的输出示例：

```json
{"result": "success", "report": "豆包签到成功, 200 额度已到账", "mode": "client", "needs_attention": false, "screenshot": ".../data/shots/result-20261023-092001.png"}
```

---

<a id="%EF%B8%8F-工作原理"></a>

## ⚙️ 工作原理

登录后，豆包电脑版客户端把登录态保存在本机定制 Chromium（代号 saman）的 Cookie 数据库里。脚本从客户端数据目录读取 Cookie 库，在内存中解密后注入无头浏览器，全程不落盘、不打印、不回写客户端；登录与续期仍由客户端负责，脚本不碰 refresh token。签到活动页会校验「是否电脑版客户端」，实测判据就是 `navigator.userAgent` 里有没有 `SamanthaDoubao` 子串——脚本因此带上客户端同款 UA 标识，不带它会被「仅支持在豆包电脑版参与」拦截。

| 凭据来源 | 支持情况 |
|---|---|
| 客户端 Cookie 库（钥匙串密钥 → AES-128-CBC） | ✅ 真机实测通过（解密后签到成功） |
| 客户端 Cookie 库（DPAPI 密钥 → AES-256-GCM，兼容明文头部 32 字节校验块的定制；Cookie 库同时探测 `<profile>/Cookies` 与新版 `<profile>/Network/Cookies`） | ✅ 真机实测通过（29 条凭据全部解密，端到端签到成功） |
| 客户端运行时锁库 | 四层保障：直接拷贝 → `esentutl` 备份语义拷贝（系统自带）→ **VSS 卷影拷贝**（管理员权限下在卷级别绕过文件锁，客户端开着也能读；`run` 模式凭据读不到时自动弹一次 UAC 请求提权走这条路，结果写回原终端）→ **本机加密缓存**（任何一次成功读取后，凭据以 Windows DPAPI / macOS 钥匙串派生密钥加密缓存到 `data/`，锁库时自动顶上，成功读取后自动刷新） |
| v20 / App-Bound Encryption | 暂不支持：明确报告原因后回落 `login` 模式，不发送无效凭据 |
| `profile` 网页登录态（`data/profile`） | 后备通道：活动目前仅限客户端参与，网页登录态大概率被 `CLIENT_ONLY` 拦截，仅作备用与诊断 |

客户端升级可能改变数据目录布局或加密格式。先运行 `python checkin.py doctor` 离线自检：它会逐项确认 Python 包、浏览器、凭据解密（`credential_mode`）与目录可写性，`client` 即零配置成功；`detail` 提示 v20/App-Bound 即为暂不支持的加密格式，直接用 `login` 模式。

签到流程（两种凭据模式共用）：

1. 🧩 **读取**客户端登录凭据（自动探测，内存解密），UA 带上客户端标识
2. 🌐 **打开**签到活动页 `https://www.doubao.com/pc/agent-explore`
3. 🔍 **幂等前置检查**：页面显示「每日签到」任务且「立即签到」按钮已消失 → 已签过，直接收手
4. 🖱️ **点击**按 `CLICK_TEXTS` 候选文案找到的「立即领取 / 立即签到」按钮
5. 🔎 **判定**从页面文案确认结果：签到成功 / 已签过 / 未确认
6. 📤 **输出**一行 JSON（`result` / `report` / `mode` / `needs_attention` / `screenshot`），同时存档截图、弹系统通知

> [!NOTE]
> 全程走豆包页面 UI，不碰任何私有接口。页面打不开按 5/15/30 秒退避重试，覆盖「刚开机 WiFi 还没就绪」的窗口；重试次数受时间预算（`DOUBAO_BUDGET_SECONDS`，默认 480 秒）约束，不会无限耗——预算必须小于定时任务的超时上限，否则跑穿被系统强杀会死在写日志之前。签到判定幂等：已签过只查询不领取，重复运行、多任务并发都不会重复签到。环境变量写错只回落默认值（避免崩在异常保护生效之前）；任何未预期异常都会以 `ERROR` 结果码落到日志并标记 `needs_attention`，不会无声消失；`checkin.log` 超过 512KB 自动截断保留最新一半，长期轮询不会无限膨胀。签到活动页改版导致按钮文案变化时，脚本会输出 `unknown` 并留下截图，把新文案补进 `checkin.py` 的 `CLICK_TEXTS` 列表即可。

---

## 🔧 配置

| 环境变量 / 位置 | 作用 |
|---|---|
| `CHECKIN_HOUR` / `CHECKIN_MINUTE` | 定时触发时间，默认 9:20 |
| `CHECKIN_POLL_SECONDS` | 兜底轮询间隔，默认 14400（4 小时） |
| `DOUBAO_CHECKIN_LOG` | `silent` 类模式的结果日志路径，默认脚本目录下 `data/checkin.log` |
| `DOUBAO_BUDGET_SECONDS` | 单次运行的时间预算，默认 480 秒。**必须小于定时任务的超时上限**，否则跑穿被系统强杀会死在写日志之前，当天记录整条丢失 |
| `DOUBAO_CLIENT_UA_VERSION` | 客户端凭据模式的 UA 版本号（`SamanthaDoubao/<版本>`），默认 2.33.3；客户端升级后若遇 `CLIENT_ONLY` 可尝试同步更新 |
| `checkin.py` 里的 `CLICK_TEXTS` | 签到按钮候选文案，豆包改版后在此补充 |

> [!NOTE]
> 环境变量非法、越界时脚本会夹到安全值并回落默认，**绝不抛异常**——避免崩在异常保护生效之前、日志一条不留。

---

## 🧪 排错

| 现象 | 处理 |
|---|---|
| `AUTH_ERROR / 登录态失效` | `mode` 为 `client` 时：打开豆包电脑版重新登录一次即可（客户端凭据随之更新）；`mode` 为 `profile` 时重跑 `login` |
| Windows 提示 VSS 不可用 | 看结果 `detail`（或 `data\checkin.log`）里的具体原因：`当前非管理员权限` → 正常，`run` 会自动弹 UAC 提权重跑；`VSS 服务无法启动 / SVC*` → VSS 服务被安全或优化软件禁用，需到「服务」里把 `Volume Shadow Copy` 和 `Microsoft Software Shadow Copy Provider` 设为可用；`卷影创建失败 ReturnValue=1/5` → 组策略或第三方安全软件拦截卷影创建，按事件查看器里 VSS 来源的错误进一步定位；`ReturnValue=7` → 存储配额不足，管理员运行 `vssadmin resize shadowstorage /for=C: /on=C: /maxsize=10%` 后重试 |
| `NO_CREDENTIAL / 未找到可用凭据` | 安装豆包电脑版并登录一次（首选），或运行 `python3 checkin.py login`；还需确认装了 `pycryptodome`。Windows 上若因客户端运行中锁库导致：`run` 会自动弹一次 UAC 请求管理员权限用 VSS 绕过锁库——点「是」即可，点完全自动；用户取消授权则等客户端关闭窗口期（轮询含凌晨时点 + 开机登录补跑）自动完成首次读取并建缓存 |
| `CLIENT_ONLY / 活动仅限电脑版参与` | 客户端凭据模式不应出现此码；若出现说明平台升级了校验（可能不再只看 UA），请提 issue 并附截图 |
| `doctor` 里 `client-credential` 不过 | 确认客户端已登录、装了 `pycryptodome`；提示「客户端 Cookie 解密后为空」先确认客户端是否登录过、是否正被豆包进程锁库（退出客户端重试）；`detail` 提示 v20/App-Bound 即为暂不支持的加密格式，直接用 `login` 模式；提示「未找到用户数据目录」则说明客户端目录名不在探测范围，提 issue 并附上客户端数据目录的截图 |
| `NETWORK / 网络不可达` | 已自动重试 3 轮（5/15/30 秒退避）仍失败；轮询任务稍后会补，频繁出现则检查代理/DNS |
| `BROWSER / 浏览器环境异常` | 先跑 `python3 checkin.py doctor` 看哪项体检不过，按 `detail` 提示修复 |
| `TIMEOUT / 时间预算耗尽` | 网络严重超时把预算耗光，脚本已主动收尾并如实记录；轮询任务稍后会补 |
| `unknown / 签到结果未能确认` | 打开 JSON 里 `screenshot` 的截图，按页面实际按钮文案更新 `CLICK_TEXTS` |
| 定时跑了但日志空空如也 | 先查脚本有没有放在受隐私保护的目录（见上方 IMPORTANT）；再确认定时任务里的 python 路径真实可用——本仓库安装脚本会自动规避占位壳问题 |
| Windows 注册任务报 0x80070005 或「参数错误 (20,8): UserId」 | 安装脚本已自动弹 UAC 提权并处理「域\用户」完整主体；仍失败则确认组策略未禁止当前用户注册任务 |
| Windows 注册「成功」但任务不存在 | 权限不足时 `Register-ScheduledTask` 会静默失败（不报错）——安装脚本已内置回读校验 + 自动提权，用最新版脚本重跑即可 |
| Windows 装依赖时 pip 找不到 / 装到别的 Python | 系统 PATH 极简、pip 常不在 PATH：一律用 `python -m pip install ...` 模块形式，且用第 0 步确定的**绝对路径** Python |
| Windows AI 代配时装好依赖但定时任务跑不起来 | 豆包沙箱的 PATH 会漂移，裸 `python` 可能是另一个解释器——定时任务必须用安装脚本探测出的 pythonw **绝对路径**（脚本已内置） |
| Windows PowerShell 下载报「基础连接已经关闭」 | PS 5.1 默认不启用 TLS 1.2，先执行 `[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12` 再下载；pip / playwright 浏览器下载走 Python 自己的 TLS 栈，不受影响 |
| Windows 控制台中文乱码 | PS 5.1 控制台按 GBK 解码 UTF-8 输出，**只影响显示不影响功能**；安装脚本已自动设置 UTF-8，其他脚本如遇乱码可先执行 `[Console]::OutputEncoding = [Text.Encoding]::UTF8` |
| Windows 定时跑了但日志没动静 | 确认任务参数是 `silent` / `silent-poll`（无窗口 pythonw 下 stdout 是黑洞，只有 silent 类命令会写日志文件） |
| 09:20 那次没跑，连签断了 | 装了模式 B 就不会断：当天开过机，轮询会自动补上；只用了模式 A 的话，豆包的调度器没有补跑，撞上关机/睡眠当天就错过了 |
| 签到成功但额度没到账 | 打开 `data/shots/` 最新截图人工核对，确认无误后把额度到期时间记一下（当天 24 点过期） |

> [!IMPORTANT]
> 需要处理的结果都会附 `needs_attention: true`；静默模式下认证错误、未预期异常也一定会写入 `data/checkin.log`，不会无声消失。

---

## 🔐 安全与隐私

- 仓库不含、不内嵌、不传输任何凭据，`.gitignore` 已排除 `data/`
- **客户端凭据只读使用**：解密在本机内存完成，Cookie 值不打印、不回写客户端
- **唯一落盘的凭据副本是加密缓存** `data/client-cookies-cache.bin`（客户端锁库时的自动兜底）：Windows 用 DPAPI 按当前用户加密、macOS 用钥匙串密钥派生 AES-256-CBC 加密——换用户 / 换机器均不可解，且 `.gitignore` 已排除；每次成功读取后自动刷新，仅在本机签到流程内使用
- 密钥只取自本机系统标准接口（macOS 钥匙串 / Windows DPAPI），全部在本机完成；脚本首次运行时系统可能弹一次授权框，选「始终允许」后不再弹
- 脚本只打开豆包官方页面做签到点击，不读取、不修改其他页面，不调用任何私有接口
- 不会打印或记录任何 Cookie / Token
- 可安全 fork、分享、在自己机器上运行——它只作用于**你自己的**登录态

---

## ⚠️ 免责声明

> [!WARNING]
> 本项目为**非官方**工具，与字节跳动 / 豆包无任何隶属关系。通过浏览器 UI 自动化模拟人工签到，不逆向、不调用任何私有接口；客户端凭据读取仅使用本机系统标准接口与客户端本地数据，全部在本机完成。签到活动规则由豆包官方随时调整，本工具可能随页面改版失效。使用风险自负，请遵守豆包服务条款。

---

<div align="center">

**看到这儿了，说明这脚本大概率对你有用 —— 那就[点个 ⭐ Star](https://github.com/tututashu/doubao-auto-checkin) 吧**

一秒的事，却能在豆包哪天改版、脚本悄悄失灵时，让你还找得到回来的路。

</div>

---

## 📄 协议

[MIT](LICENSE) © 2026 [tututashu](https://github.com/tututashu) · 仓库：[github.com/tututashu/doubao-auto-checkin](https://github.com/tututashu/doubao-auto-checkin)
