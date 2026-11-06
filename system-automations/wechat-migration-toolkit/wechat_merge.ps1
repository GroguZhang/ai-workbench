# wechat_merge.ps1
# 微信老库合并执行器
#
# 安全前提：只删除「已在新库存在字节级相同副本」的老库文件；老库独有内容一律归档保留，不删除。
#
# 用法：
#   .\wechat_merge.ps1                      预演（不删不移，只出报告）
#   .\wechat_merge.ps1 -Apply -Mode dedup   只删重复
#   .\wechat_merge.ps1 -Apply -Mode archive 只归档独有内容
#   .\wechat_merge.ps1 -Apply -Mode all     两步都做
#   .\wechat_merge.ps1 -Apply -Mode dedup -Limit 500   小批量试跑 500 个
#
# 台账：$HOME\WorkBuddy\电脑迁移\.workbuddy\reports\

param(
    [switch]$Apply,
    [ValidateSet('all', 'dedup', 'archive')]
    [string]$Mode = 'all',
    [string]$PlanFile = '$HOME\WorkBuddy\电脑迁移\wechat_merge_plan.json',
    [string]$ArchiveRoot = 'H:\微信\_旧版微信归档_20261026',
    [string]$OldRoot = 'H:\微信\WeChat Files\kirayamato95',
    [double]$VerifyMinMB = 0.25,
    [double]$FullVerifyMB = 2,
    [int]$Limit = 0,
    [switch]$RemoveEmptyOld
)

$ErrorActionPreference = 'Continue'
$Stamp   = Get-Date -Format 'yyyyMMdd_HHmmss'
$RepDir  = '$HOME\WorkBuddy\电脑迁移\.workbuddy\reports'
if (-not (Test-Path -LiteralPath $RepDir)) { New-Item -ItemType Directory -Path $RepDir -Force | Out-Null }
$LogCsv  = Join-Path $RepDir "wechat_merge_$Stamp.csv"
$DryRun  = -not $Apply
$CH      = 16 * 1024
$VerifyMin = [int64]($VerifyMinMB * 1MB)
$FullVerify = [int64]($FullVerifyMB * 1MB)

$Rows = New-Object System.Collections.ArrayList

function Say($m, $c = 'Gray') { Write-Host $m -ForegroundColor $c }
function Title($m) {
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor Cyan
    Write-Host $m -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor Cyan
}
function Human([double]$n) {
    foreach ($u in 'B', 'KB', 'MB', 'GB', 'TB') {
        if ($n -lt 1024) { return ('{0:N1}{1}' -f $n, $u) }
        $n = $n / 1024
    }
    return ('{0:N1}PB' -f $n)
}
function Get-TreeSize([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    $s = (Get-ChildItem -LiteralPath $Path -Recurse -Force -File -ErrorAction SilentlyContinue |
          Measure-Object -Property Length -Sum).Sum
    if (-not $s) { return 0 }
    return [double]$s
}

# 与 Python 端完全一致的指纹算法：sha1(size 字符串 + 首 128KB [+ 尾 128KB])
function Get-Fingerprint([string]$Path, [int64]$Size) {
    $sha = $null
    $ms = $null
    $fs = $null
    try {
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
        $sha = [System.Security.Cryptography.SHA1]::Create()
        $ms = New-Object System.IO.MemoryStream
        $nb = [System.Text.Encoding]::UTF8.GetBytes([string]$Size)
        $ms.Write($nb, 0, $nb.Length)

        if ($Size -le $CH * 2) {
            $buf = New-Object byte[] $Size
            $read = 0
            while ($read -lt $Size) {
                $n = $fs.Read($buf, $read, [int]($Size - $read))
                if ($n -le 0) { break }
                $read += $n
            }
            $ms.Write($buf, 0, $read)
        } else {
            $buf = New-Object byte[] $CH
            $read = 0
            while ($read -lt $CH) {
                $n = $fs.Read($buf, $read, [int]($CH - $read))
                if ($n -le 0) { break }
                $read += $n
            }
            $ms.Write($buf, 0, $read)
            [void]$fs.Seek(-$CH, [System.IO.SeekOrigin]::End)
            $buf2 = New-Object byte[] $CH
            $read2 = 0
            while ($read2 -lt $CH) {
                $n = $fs.Read($buf2, $read2, [int]($CH - $read2))
                if ($n -le 0) { break }
                $read2 += $n
            }
            $ms.Write($buf2, 0, $read2)
        }
        $hash = $sha.ComputeHash($ms.ToArray())
        return ([System.BitConverter]::ToString($hash) -replace '-', '').ToLower()
    } catch {
        return $null
    } finally {
        if ($fs) { $fs.Dispose() }
        if ($ms) { $ms.Dispose() }
        if ($sha) { $sha.Dispose() }
    }
}

# 全量 SHA256，用于大文件最终把关
function Get-FullSha([string]$Path) {
    $sha = $null
    $fs = $null
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
        $hash = $sha.ComputeHash($fs)
        return ([System.BitConverter]::ToString($hash) -replace '-', '').ToLower()
    } catch {
        return $null
    } finally {
        if ($fs) { $fs.Dispose() }
        if ($sha) { $sha.Dispose() }
    }
}

# =====================================================================
Title '前置检查'
$wx = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^(WeChat|Weixin|wechat)' }
if ($wx) {
    Say ('✗ 微信正在运行：' + ($wx.ProcessName -join ', ')) 'Red'
    Say '  请完全退出微信（含托盘图标）后重新运行本脚本。' 'Red'
    exit 1
}
Say '✓ 微信未运行' 'Green'

if (-not (Test-Path -LiteralPath $PlanFile)) {
    Say ('✗ 缺少计划文件：' + $PlanFile) 'Red'
    Say '  请先运行 scripts\wechat_merge_plan.py 生成。' 'Red'
    exit 1
}
Say '✓ 计划文件存在' 'Green'

if (-not (Test-Path -LiteralPath $OldRoot)) { Say ('✗ 老库不存在：' + $OldRoot) 'Red'; exit 1 }
if (-not (Test-Path -LiteralPath $ArchiveRoot)) { New-Item -ItemType Directory -Path $ArchiveRoot -Force | Out-Null }

Say '读取计划...' 'White'
$plan = Get-Content -LiteralPath $PlanFile -Raw -Encoding UTF8 | ConvertFrom-Json
$dups = @($plan.dup)
$uniqs = @($plan.uniq)
Say ('  可删重复 {0} 个 / {1}' -f $dups.Count, (Human $plan.dup_bytes)) 'Gray'
Say ('  老库独有 {0} 个 / {1}' -f $uniqs.Count, (Human $plan.uniq_bytes)) 'Gray'

$sizeBefore = Get-TreeSize 'H:\微信'
Say ('  H:\微信 当前 {0}' -f (Human $sizeBefore)) 'Gray'
if ($DryRun) { Say ''
    Say '*** 预演模式：不会删除或移动任何文件 ***' 'Yellow' }

# =====================================================================
if ($Mode -in 'all', 'dedup') {
    Title '步骤 1 · 删除新库中已有相同副本的老库文件'
    Say '只删「新库存在字节级相同副本」的文件，其余一律跳过。' 'DarkYellow'

    $del = 0
    $delBytes = 0
    $skipGone = 0
    $skipChanged = 0
    $skipNoCopy = 0
    $n = 0

    foreach ($d in $dups) {
        $n++
        if ($Limit -gt 0 -and $del -ge $Limit) { break }
        if ($n % 2000 -eq 0) { Say ('  进度 {0}/{1}  已删 {2} / {3}' -f $n, $dups.Count, $del, (Human $delBytes)) 'DarkGray' }

        $p = $d.p
        if (-not (Test-Path -LiteralPath $p)) { $skipGone++; continue }
        $cur = (Get-Item -LiteralPath $p -Force).Length
        if ($cur -ne $d.s) {
            $skipChanged++
            [void]$Rows.Add([PSCustomObject]@{ Time = (Get-Date -Format 's'); Action = '跳过-尺寸已变'; Path = $p; Size = $cur; Note = '' })
            continue
        }

        # 新库副本仍存在？
        if ($d.m -and -not (Test-Path -LiteralPath $d.m)) {
            $skipNoCopy++
            [void]$Rows.Add([PSCustomObject]@{ Time = (Get-Date -Format 's'); Action = '跳过-新库副本缺失'; Path = $p; Size = $cur; Note = $d.m })
            continue
        }

        # 大文件（>= FullVerifyMB）：老库与新库副本做全量 SHA256 直接比对，最高保证
        if ($cur -ge $FullVerify) {
            $ho = Get-FullSha $p
            $hn = Get-FullSha $d.m
            if (-not $ho -or -not $hn -or $ho -ne $hn) {
                $skipChanged++
                [void]$Rows.Add([PSCustomObject]@{ Time = (Get-Date -Format 's'); Action = '跳过-全量哈希不符'; Path = $p; Size = $cur; Note = $d.m })
                continue
            }
        }
        # 中等文件：复核指纹，老库文件与计划一致且新库副本等于同一指纹
        elseif ($cur -ge $VerifyMin -and $d.f) {
            $fo = Get-Fingerprint $p $cur
            if (-not $fo -or $fo -ne $d.f) {
                $skipChanged++
                [void]$Rows.Add([PSCustomObject]@{ Time = (Get-Date -Format 's'); Action = '跳过-指纹不符'; Path = $p; Size = $cur; Note = '老库文件内容与计划不符' })
                continue
            }
            $fn = Get-Fingerprint $d.m $cur
            if (-not $fn -or $fn -ne $d.f) {
                $skipNoCopy++
                [void]$Rows.Add([PSCustomObject]@{ Time = (Get-Date -Format 's'); Action = '跳过-新库指纹不符'; Path = $p; Size = $cur; Note = $d.m })
                continue
            }
        }

        if ($DryRun) {
            if ($del -lt 25) { Say ('  [预演] ' + (Human $cur) + '  ' + $p) 'DarkGray' }
            $del++
            $delBytes += $cur
            continue
        }

        try {
            $attr = (Get-Item -LiteralPath $p -Force).Attributes
            if ($attr -band [System.IO.FileAttributes]::ReadOnly) {
                (Get-Item -LiteralPath $p -Force).Attributes = 'Normal'
            }
            [System.IO.File]::Delete($p)
            $del++
            $delBytes += $cur
            [void]$Rows.Add([PSCustomObject]@{ Time = (Get-Date -Format 's'); Action = '删除重复'; Path = $p; Size = $cur; Note = $d.m })
        } catch {
            [void]$Rows.Add([PSCustomObject]@{ Time = (Get-Date -Format 's'); Action = '删除失败'; Path = $p; Size = $cur; Note = $_.Exception.Message })
        }
    }

    Say ''
    Say ('  处理 {0} 个，删除 {1} / {2}' -f $n, $del, (Human $delBytes)) 'Green'
    Say ('  跳过：已不存在 {0} / 尺寸变化 {1} / 新库副本缺失 {2}' -f $skipGone, $skipChanged, $skipNoCopy) 'Yellow'
}

# =====================================================================
if ($Mode -in 'all', 'archive') {
    Title '步骤 2 · 老库独有内容归档'
    Say ('归档目标：' + $ArchiveRoot) 'White'
    Say '保持老库内部目录结构，可随时还原。' 'DarkYellow'

    $mv = 0
    $mvBytes = 0
    $fail = 0
    $n = 0
    $oldRootFull = (Resolve-Path -LiteralPath $OldRoot).Path

    foreach ($u in $uniqs) {
        $n++
        if ($n % 1000 -eq 0) { Say ('  进度 {0}/{1}  已归档 {2} / {3}' -f $n, $uniqs.Count, $mv, (Human $mvBytes)) 'DarkGray' }

        $p = $u.p
        if (-not (Test-Path -LiteralPath $p)) { continue }
        if (-not $p.StartsWith($oldRootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
            [void]$Rows.Add([PSCustomObject]@{ Time = (Get-Date -Format 's'); Action = '跳过-路径越界'; Path = $p; Size = $u.s; Note = '' })
            continue
        }
        $rel = $p.Substring($oldRootFull.Length).TrimStart('\')
        $dest = Join-Path $ArchiveRoot $rel

        if ($DryRun) {
            if ($mv -lt 25) { Say ('  [预演] ' + (Human $u.s) + '  ' + $rel) 'DarkGray' }
            $mv++
            $mvBytes += $u.s
            continue
        }

        try {
            $destDir = Split-Path $dest -Parent
            if (-not (Test-Path -LiteralPath $destDir)) {
                New-Item -ItemType Directory -Path $destDir -Force | Out-Null
            }
            $final = $dest
            $i = 1
            while (Test-Path -LiteralPath $final) {
                $bn = [System.IO.Path]::GetFileNameWithoutExtension($dest)
                $ex = [System.IO.Path]::GetExtension($dest)
                $final = Join-Path $destDir ("{0}_{1}{2}" -f $bn, $i, $ex)
                $i++
            }
            Move-Item -LiteralPath $p -Destination $final -ErrorAction Stop
            $mv++
            $mvBytes += $u.s
            [void]$Rows.Add([PSCustomObject]@{ Time = (Get-Date -Format 's'); Action = '归档'; Path = $p; Size = $u.s; Note = $final })
        } catch {
            $fail++
            [void]$Rows.Add([PSCustomObject]@{ Time = (Get-Date -Format 's'); Action = '归档失败'; Path = $p; Size = $u.s; Note = $_.Exception.Message })
        }
    }

    Say ''
    Say ('  处理 {0} 个，归档 {1} / {2}，失败 {3}' -f $n, $mv, (Human $mvBytes), $fail) 'Green'
}

# =====================================================================
Title '清理空目录'
$oldFull = (Resolve-Path -LiteralPath $OldRoot -ErrorAction SilentlyContinue).Path
if ($oldFull -and (Test-Path -LiteralPath $oldFull)) {
    $emptyDirs = Get-ChildItem -LiteralPath $oldFull -Recurse -Directory -Force -ErrorAction SilentlyContinue |
                 Sort-Object { $_.FullName.Length } -Descending
    $removed = 0
    foreach ($dir in $emptyDirs) {
        $hasChild = Get-ChildItem -LiteralPath $dir.FullName -Force -ErrorAction SilentlyContinue
        if (-not $hasChild) {
            if (-not $DryRun) { try { [System.IO.Directory]::Delete($dir.FullName, $false); $removed++ } catch {} }
            else { $removed++ }
        }
    }
    Say ('  清理空目录 {0} 个' -f $removed) 'Green'

    $leftover = Get-ChildItem -LiteralPath $oldFull -Recurse -File -Force -ErrorAction SilentlyContinue
    $leftBytes = 0
    foreach ($f in $leftover) { $leftBytes += $f.Length }
    Say ('  老库剩余 {0} 个文件 / {1}' -f @($leftover).Count, (Human $leftBytes)) 'White'
    if (@($leftover).Count -eq 0) {
        Say '  老库已清空。' 'Green'
        if ($RemoveEmptyOld -and -not $DryRun) {
            try {
                [System.IO.Directory]::Delete($oldFull, $true)
                Say '  已删除空的老库根目录。' 'Green'
            } catch { Say ('  删除老库根目录失败：' + $_.Exception.Message) 'Red' }
        } else {
            Say ('  如需删除空壳，加 -RemoveEmptyOld 或手动删除：' + $oldFull) 'DarkYellow'
        }
    }
}

# =====================================================================
Title '汇总'
$sizeAfter = Get-TreeSize 'H:\微信'
Say ('  H:\微信  {0} -> {1}' -f (Human $sizeBefore), (Human $sizeAfter)) 'White'
Say ('  变化     -{0}' -f (Human ($sizeBefore - $sizeAfter))) 'Cyan'

if ($Rows.Count -gt 0 -and -not $DryRun) {
    $Rows | Export-Csv -LiteralPath $LogCsv -NoTypeInformation -Encoding UTF8
    Say ('  台账 -> ' + $LogCsv) 'Gray'
}

if ($DryRun) {
    Say ''
    Say '预演结束。确认无误后加 -Apply 正式执行，例如：' 'Yellow'
    Say '  .\wechat_merge.ps1 -Apply -Mode dedup' 'Yellow'
} else {
    Say ''
    Say '完成。按回车退出。' 'Cyan'
    [void](Read-Host)
}
