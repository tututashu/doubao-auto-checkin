# install-windows.ps1 — 豆包自动签到一键安装（Windows）
#
# 用法：在本仓库目录下，用 PowerShell 运行（普通权限即可；仅当系统拒绝注册时才弹一次 UAC 提权）
#     powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
#
# 可选参数：-Hour 9 -Minute 20（默认 09:20）
#
# 会创建两个定时任务：
#   1) DoubaoAutoSignin    每天 09:20    签到，静默写 data\checkin.log
#   2) DoubaoCheckinPoll   01/05/09/13/17/21 点共 6 次 + 每次登录时   补签（未签才签）
#
# 两者都零 Token、无窗口、错过会自动补跑；脚本幂等，重复触发不会重复签到。
# 01/05 点电脑关机也没关系：登录触发 + StartWhenAvailable 双保险，开机进桌面即补跑。

param(
    [int]$Hour = 9,
    [int]$Minute = 20
)

$ErrorActionPreference = "Stop"

# PowerShell 5.1 控制台默认按 GBK 解码 UTF-8 输出，中文会显示乱码（只影响显示，不影响功能与数据）。
[Console]::OutputEncoding = [Text.Encoding]::UTF8
# PS 5.1 默认不启用 TLS 1.2，脚本内发起的 HTTPS 请求（Invoke-WebRequest 等）会握手失败；
# pip 走 Python 自己的 TLS 栈，不受此影响。
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# ====== 一般不用改；自动探测失败时手动填这两个 ======
$ManualPythonw = ""   # 例如 C:\Python313\pythonw.exe
$ManualCheckin = ""   # 例如 C:\Tools\doubao-auto-checkin\checkin.py
# ====================================================

# --- 0. 管理员权限策略：能不弹窗就不弹窗 ---
# 实测：非管理员注册计划任务有两种结局——标准用户多数能直接注册成功（任务以自己身份
# 运行，无需管理员）；但部分机器上 Register-ScheduledTask 会「静默失败」：返回成功（空
# 对象）而不报错，任务实际不存在，schtasks /Create 也失败（exit -2147467259）。
# 所以策略是：先直接注册 + 回读校验，只有校验发现任务真不存在时，才弹 UAC 提权重试。
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

function Find-Pythonw {
    # 1) PATH 里就有 pythonw
    $c = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if ($c -and (Test-Path -LiteralPath $c)) { return $c }

    # 2) PATH 里有 python.exe，取同目录的 pythonw
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($py) {
        $c = Join-Path (Split-Path $py) "pythonw.exe"
        if (Test-Path -LiteralPath $c) { return $c }
    }

    # 3) 常见安装目录（很多 Windows 装机 Python 并不进 PATH，这一步最常命中）
    foreach ($pat in @(
        "C:\Python3*\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python3*\pythonw.exe",
        "C:\Program Files\Python3*\pythonw.exe"
    )) {
        $hit = Get-ChildItem $pat -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }

    # 4) py 启动器（装在 C:\Windows，几乎总在）
    $launcher = Join-Path $env:SystemRoot "py.exe"
    if (Test-Path -LiteralPath $launcher) {
        try {
            $exe = (& $launcher -3 -c "import sys;print(sys.executable)" 2>$null | Select-Object -Last 1)
            if ($exe) {
                $c = Join-Path (Split-Path $exe.Trim()) "pythonw.exe"
                if (Test-Path -LiteralPath $c) { return $c }
            }
        } catch { }
    }
    return $null
}

Write-Host ""
Write-Host "豆包自动签到 · Windows 一键安装" -ForegroundColor Cyan
Write-Host ("-" * 46) -ForegroundColor DarkGray

# --- 1. 定位 pythonw.exe ---
Write-Host "[1/4] 定位 pythonw.exe ..." -NoNewline
$pythonw = $ManualPythonw
# 必须先判空再 Test-Path：Test-Path "" 抛的是参数校验异常（不是返回 $false），
# 配合顶部的 $ErrorActionPreference = "Stop"，会让脚本直接死在这一行。
if (-not $pythonw -or -not (Test-Path -LiteralPath $pythonw)) { $pythonw = Find-Pythonw }
if (-not $pythonw -or -not (Test-Path -LiteralPath $pythonw)) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "没找到 pythonw.exe。请先安装 Python 3：https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "装好后重跑本脚本；或把脚本顶部的 `$ManualPythonw 填成完整路径。" -ForegroundColor Yellow
    exit 1
}
Write-Host " $pythonw" -ForegroundColor Green

# --- 2. 定位 checkin.py ---
Write-Host "[2/4] 定位 checkin.py ..." -NoNewline
$checkin = $ManualCheckin
# $PSScriptRoot 在脚本被直接粘进控制台执行时是空的，Join-Path 不接受空 Path。
if (-not $checkin -or -not (Test-Path -LiteralPath $checkin)) {
    if ($PSScriptRoot) { $checkin = Join-Path $PSScriptRoot "checkin.py" }
}
if (-not $checkin -or -not (Test-Path -LiteralPath $checkin)) { $checkin = Join-Path (Get-Location) "checkin.py" }
if (-not $checkin -or -not (Test-Path -LiteralPath $checkin)) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "没找到 checkin.py。请把本脚本和 checkin.py 放在同一目录后重跑，" -ForegroundColor Yellow
    Write-Host "或把脚本顶部的 `$ManualCheckin 填成完整路径。" -ForegroundColor Yellow
    exit 1
}
$checkin = (Resolve-Path -LiteralPath $checkin).Path
Write-Host " $checkin" -ForegroundColor Green

# --- 3. 检查 playwright 依赖 ---
Write-Host "[3/4] 检查 playwright ..." -NoNewline
$pyExe = Join-Path (Split-Path $pythonw) "python.exe"
if (-not (Test-Path -LiteralPath $pyExe)) { $pyExe = $pythonw }
& $pyExe -c "import playwright" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "该 Python 没装 playwright。请运行：" -ForegroundColor Yellow
    Write-Host "  $pyExe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple playwright" -ForegroundColor Yellow
    Write-Host "装好后重跑本脚本。" -ForegroundColor Yellow
    exit 1
}
Write-Host " OK" -ForegroundColor Green

# --- 4. 创建两个定时任务 ---
Write-Host "[4/4] 创建定时任务 ..." -NoNewline
# env 覆盖值必须校验格式：[int] 强转非数字会在 $ErrorActionPreference=Stop 下直接崩掉脚本
$hour = if ($env:CHECKIN_HOUR -match '^\d{1,2}$') { [int]$env:CHECKIN_HOUR } else { $Hour }
$minute = if ($env:CHECKIN_MINUTE -match '^\d{1,2}$') { [int]$env:CHECKIN_MINUTE } else { $Minute }
$taskRows = @(
    @{ Name = "DoubaoAutoSignin"; When = "每天 {0:d2}:{1:d2}" -f $hour, $minute; What = "签到"; Arg = "silent" },
    @{ Name = "DoubaoCheckinPoll"; When = "每天 6 次轮询 + 每次登录"; What = "补签（未签才签）"; Arg = "silent-poll" }
)
$registeredTasks = @()
$operation = "创建"
$currentTask = "DoubaoAutoSignin"
try {
    # 计划任务主体必须用「域\计算机名\用户名」的完整形式：裸用户名会被任务计划服务
    # 以「参数错误」拒绝注册（真机实测）。WindowsIdentity.GetCurrent().Name 天然就是
    # 完整形式，无需手动拼接。
    $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    if (-not $userId) { $userId = "$env:USERDOMAIN\$env:USERNAME" }
    if (-not $userId) { $userId = $env:USERNAME }
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive `
        -RunLevel Limited -ErrorAction Stop

    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -ErrorAction Stop

    # 任务 1：每日签到（每天 09:20，结果写 data\checkin.log）
    $act1 = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$checkin`" silent" -ErrorAction Stop
    $tri1 = New-ScheduledTaskTrigger -Daily -At ("{0:d2}:{1:d2}" -f $hour, $minute) -ErrorAction Stop
    Register-ScheduledTask -TaskName $currentTask -TaskPath "\" `
        -Action $act1 -Trigger $tri1 -Settings $settings -Principal $principal `
        -Description "Doubao daily auto signin (silent, zero token)" -Force -ErrorAction Stop | Out-Null
    $registeredTasks += $currentTask

    # 任务 2：轮询（每天 01/05/09/13/17/21 点，共 6 次）
    # 刻意用 6 个独立的 Daily 触发器，而不是"Daily + 每 4 小时重复"：
    # 实测后者的重复实例一旦错过（关机/睡眠）就永久跳过，StartWhenAvailable
    # 不会为它补跑。独立触发器则每次错过都会在下次开机时补上。
    # 另加登录触发：01/05 点电脑很可能关机，错过的时间点落在关机时段时，
    # 用户开机进桌面的那一刻立即补跑（与 StartWhenAvailable 双保险）。
    # 跑的是 silent-poll：未签才补签；当天签上后脚本秒退（读日志判重，不启动浏览器），幂等不会重复领取。
    $currentTask = "DoubaoCheckinPoll"
    $tri2 = @()
    foreach ($hh in @("01:00", "05:00", "09:00", "13:00", "17:00", "21:00")) {
        $tri2 += New-ScheduledTaskTrigger -Daily -At $hh -ErrorAction Stop
    }
    $tri2 += New-ScheduledTaskTrigger -AtLogOn -ErrorAction Stop
    $act2 = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$checkin`" silent-poll" -ErrorAction Stop
    Register-ScheduledTask -TaskName $currentTask -TaskPath "\" `
        -Action $act2 -Trigger $tri2 -Settings $settings -Principal $principal `
        -Description "Doubao poll (catch-up signin, idempotent)" -Force -ErrorAction Stop | Out-Null
    $registeredTasks += $currentTask

    # 两个任务及运行信息均回读成功后才宣布完成；同名旧任务不能掩盖注册失败。
    $operation = "校验"
    $verifiedTasks = @()
    foreach ($row in $taskRows) {
        $currentTask = $row.Name
        $tasks = @(Get-ScheduledTask -TaskName $currentTask -TaskPath "\" -ErrorAction Stop)
        if ($tasks.Count -ne 1) { throw "未找到唯一的计划任务：\$currentTask" }
        $info = Get-ScheduledTaskInfo -TaskName $currentTask -TaskPath "\" -ErrorAction Stop
        if ($null -eq $info) { throw "未读取到计划任务运行信息：\$currentTask" }
        $verifiedTasks += @{
            Name = $row.Name; When = $row.When; What = $row.What
            State = $tasks[0].State; NextRunTime = $info.NextRunTime
        }
    }
} catch {
    # 普通权限注册失败（含「静默失败」：返回成功但回读不到任务）——若当前非管理员，
    # 自动弹 UAC 提权重启自身重试一次；UAC 弹窗是 Windows 安全边界，无法绕过，
    # 只能在系统确实拒绝时才弹，且仅此一次。
    if (-not $isAdmin) {
        Write-Host " 失败" -ForegroundColor Red
        Write-Host ""
        Write-Host "当前权限无法注册计划任务，正在弹出 UAC 授权窗口重试，请在弹窗中点「是」..." -ForegroundColor Yellow
        $self = if ($PSScriptRoot) { Join-Path $PSScriptRoot "install-windows.ps1" } else { $MyInvocation.MyCommand.Path }
        if (-not $self -or -not (Test-Path -LiteralPath $self)) {
            Write-Host "无法定位脚本自身，请用管理员身份打开 PowerShell 后重跑本脚本。" -ForegroundColor Red
            exit 1
        }
        # 环境变量 CHECKIN_HOUR/MINUTE 无法跨越提权进程传递，把实际生效的小时/分钟
        # （env 或参数算出来的结果）用参数显式带上
        $relaunchArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', "`"$self`"", '-Hour', $hour, '-Minute', $minute)
        try {
            Start-Process powershell -Verb RunAs -ArgumentList $relaunchArgs -Wait
            exit 0
        } catch {
            Write-Host "已取消授权或提权失败，未注册任何任务。" -ForegroundColor Red
            exit 1
        }
    }
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "$operation $currentTask 失败，安装未完成。" -ForegroundColor Red
    Write-Host "错误信息：$($_.Exception.Message)" -ForegroundColor Red
    if ($registeredTasks.Count -gt 0) {
        Write-Host ("已注册：" + ($registeredTasks -join "、") + "。这些任务会保留；排除问题后重跑可更新同名任务。") -ForegroundColor Yellow
    }
    Write-Host "已知坑：权限不足时 Register-ScheduledTask 可能不报错但任务实际不存在（回读校验已兜住这种情况）；" -ForegroundColor Yellow
    Write-Host "若提示拒绝访问（0x80070005），请确认当前是管理员账号，且组策略未禁止该用户注册任务。" -ForegroundColor Yellow
    Write-Host "也可使用 README 的模式 A：豆包定时任务，无需计划任务。" -ForegroundColor Yellow
    exit 1
}
Write-Host " 完成" -ForegroundColor Green

# --- 收尾汇报 ---
Write-Host ""
Write-Host "两个定时任务已就位：" -ForegroundColor Green
foreach ($row in $verifiedTasks) {
    Write-Host ("  {0,-20} {1,-7} {2,-14} {3}" -f $row.Name, $row.State, $row.When, $row.What)
    Write-Host ("  {0,-20} 下次运行 {1}" -f "", $row.NextRunTime) -ForegroundColor DarkGray
}

Write-Host ""
$logFile = Join-Path (Split-Path -LiteralPath $checkin) "data\checkin.log"
Write-Host "日志：$logFile" -ForegroundColor Cyan
Write-Host "想确认是否跑通：" -ForegroundColor DarkGray
Write-Host "  Get-Content -LiteralPath '$logFile' -Tail 3" -ForegroundColor DarkGray
Write-Host ""
Write-Host "卸载（两个一起删）：" -ForegroundColor DarkGray
Write-Host '  Unregister-ScheduledTask -TaskName "DoubaoAutoSignin" -Confirm:$false' -ForegroundColor DarkGray
Write-Host '  Unregister-ScheduledTask -TaskName "DoubaoCheckinPoll" -Confirm:$false' -ForegroundColor DarkGray
Write-Host ""
Write-Host "安装完成，本窗口可以关闭了。" -ForegroundColor Gray
Write-Host ""
