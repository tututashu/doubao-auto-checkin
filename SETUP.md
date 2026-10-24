# SETUP.md — 一键配置步骤（本文件是写给 AI 助手执行的）

> AI 助手你好。用户要求你按本文件完成豆包自动签到配置。
> 请严格按顺序执行，每步完成后再进行下一步；任何一步失败就停下来报告原因，不要跳步、不要自由发挥。
> 本文件所在目录即仓库目录（下称 `<仓库目录>`），请用它的实际绝对路径。

## 第 0 步 · 环境准备

1. 确定本机 Python 3 路径并记录（下称 `<python3>`）：
   - macOS / Linux：`which python3`
   - Windows：`where python`（会输出绝对路径，后续命令**一律用这个绝对路径**代替 `python3`/`python`）
   - 注意：不要想当然用 `/usr/bin/python3`，没装 Xcode CLT 时它是占位壳
   - **Windows 关键坑（路径漂移）**：豆包沙箱里的 PATH 与真实用户环境不同，裸 `python` 可能解析到另一个没有 playwright 的 Python。因此**后续所有命令必须用这里确定的绝对路径**（如 `C:\Python313\python.exe`），不要写裸 `python`；创建定时任务前用同一路径再验证一次 `import playwright`
   - **Windows 其他已知坑**：系统 PATH 极简、pip 常不在 PATH（用 `python -m pip` 模块形式可绕过）；PowerShell 5.1 需要下载文件时先执行 `[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12`（TLS 1.2 默认未启用，否则报「基础连接已经关闭」）；控制台中文乱码是 GBK 解码显示问题，执行 `[Console]::OutputEncoding = [Text.Encoding]::UTF8` 即可，不影响功能
2. 检查该 Python 是否装有依赖，缺哪个装哪个。**Windows 上 pip 常常不在 PATH、系统 PATH 也极简，一律用 `python -m pip` 模块形式**（`pip install` 裸命令会直接找不到）：

```
<python3> -c "import playwright" && <python3> -c "import Crypto"
```

```
<python3> -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple playwright pycryptodome
```

- `playwright`：浏览器自动化（必需）；chromium 已装过则无需再下载，脚本用 `channel="chromium"` 走完整版
- `pycryptodome`：客户端凭据解密用（macOS / Windows 客户端凭据模式必需）

## 第 1 步 · 凭据确认（多数情况可跳过登录）

**macOS 且装有豆包电脑版（用户登录过）**：无需任何登录操作——`run` 会自动从客户端读取登录凭据。先跑第 2 步自检确认 `credential_mode` 为 `client` 即可。

**Windows 且装有豆包电脑版（用户登录过）**：同样无需登录操作——`run` 会自动从客户端读取登录凭据（已在 Windows 11 真机实测通过）。若豆包客户端正在运行可能锁库，属正常现象，脚本有兜底处理；为 `none` 或 `FAIL` 时运行 login（下行）。

**未装豆包客户端**：运行下面的命令，会弹出浏览器窗口：

```
<python3> <仓库目录>/checkin.py login
```

提醒用户在弹出的窗口里登录豆包账号（扫码即可），等用户确认登录完成后回车退出，再继续。注意：签到活动目前仅限豆包电脑版客户端参与，网页登录态可能被平台拦截（`run` 返回 `CLIENT_ONLY` 时如实告知用户）。

## 第 2 步 · 自检 + 测试签到

先运行离线自检（不联网、不签到）：

```
<python3> <仓库目录>/checkin.py doctor
```

输出 JSON 的 `result` 为 `ok` 且 `credential_mode` 不为 `none` 才继续；为 `FAIL` 时按 `checks` 里失败项的 `detail` 修复后重跑（`credential_mode` 为 `none` 说明既没有客户端凭据也没有网页登录态，回第 1 步）。

再测试签到：

```
<python3> <仓库目录>/checkin.py run
```

它输出一行 JSON，按 `result` 处理：

- `success` / `already`：测试通过，继续下一步
- `unknown`：打开 JSON 里 `screenshot` 字段的截图，告诉用户页面上实际的按钮文字，等用户确认后再继续（必要时把新文案补进 checkin.py 的 `CLICK_TEXTS` 后重跑）
- `AUTH_ERROR`：登录态失效。`mode` 为 `client` 时请用户打开豆包电脑版重新登录一次；`mode` 为 `profile` 时回到第 1 步的 login 重试
- `NETWORK`：页面打不开（脚本已自动退避重试 3 轮仍失败），检查网络后重跑本步
- `NO_CREDENTIAL`：没有可用凭据，回第 1 步（macOS 确认客户端已登录、依赖已装齐）

## 第 3 步 · 创建豆包定时任务（必做）

在豆包电脑版（工作任务模式）里创建定时任务，配置：

- 执行时间：每天上午 9 点 20 分，重复周期「每天」，勾选重复执行
- 执行环境：**本地电脑**（不要选云电脑）
- 任务内容：`<仓库目录>/doubao-task-prompt.txt` 的原文，并把其中所有 `<仓库目录>` 替换为实际绝对路径

## 第 4 步 · 系统级定时安装（必做：轮询 + 补跑，与第 3 步共存）

- **macOS**：运行 `<仓库目录>/install.sh install`（每天 09:20 + 每 4 小时轮询 + 开机补跑，均幂等；自动用真实 python3 路径生成 plist）
- **Windows**：PowerShell 运行 `powershell -ExecutionPolicy Bypass -File "<仓库目录>\install-windows.ps1"`（每天 09:20 签到 + 01/05/09/13/17/21 六次轮询补签；自动探测 pythonw.exe 并检查 playwright）。运行中若弹出 UAC 授权窗口属正常现象（脚本自动提权重试），请提示用户点「是」；窗口保持打开时任务已注册完成，读完结果再关闭

第 3 步与第 4 步**都要执行，缺一不可**：豆包任务负责到点签到和汇报，系统级任务负责每 4 小时轮询兜底和开机补跑；脚本幂等，两者共存不会重复领取。除非用户明确要求只用其中一种，否则不得跳过任何一步。注册完成后回读任务/任务计划确认存在，并告知用户下次运行时间。

## 第 5 步 · 汇报

全部完成后向用户汇报：凭据模式（`client` 即零配置成功）、签到测试结果、豆包定时任务与系统级定时任务各自的创建结果及下次执行时间。
