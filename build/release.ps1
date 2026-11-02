# Сборка и публикация релиза расширения «ЦентрУправленияЭДО» на GitHub.
#
# Номер релиза берётся из <Version> в src/Configuration.xml — второго места для номера нет
# намеренно: расходящиеся сборки с одинаковым номером отличить в базе нечем.
#
#   powershell -ExecutionPolicy Bypass -File build\release.ps1 -DryRun   # прогон без публикации
#   powershell -ExecutionPolicy Bypass -File build\release.ps1           # тег + релиз на GitHub
[CmdletBinding()]
param(
    [string] $Bin,                       # ibcmd.exe; по умолчанию ищем в C:\Program Files\1cv8
    [string] $Out,                       # каталог артефактов (по умолчанию ..\dist)
    [string] $NotesFile,                 # готовые заметки; иначе собираются из истории коммитов
    [string] $Branch = 'main',
    [switch] $DryRun,                    # всё, кроме тега, пуша и создания релиза
    [switch] $Draft,                     # опубликовать черновиком
    [switch] $SkipChecks                 # пропустить build\checks.py
)

$ErrorActionPreference = 'Stop'
# В сеансе без консоли (задача планировщика, ssh) прогресс-бар роняет вызовы с Win32 0x5.
$ProgressPreference = 'SilentlyContinue'

$root = Split-Path -Parent $PSScriptRoot

function Write-Step { param([string] $Text) Write-Host "==> $Text" }

# ⚠️ Аргументы git передаются одним массивом, а не через ValueFromRemainingArguments:
#    иначе короткие ключи вида -a (git tag -a) PowerShell принимает за сокращение имени
#    параметра -Arguments и вызов рассыпается на биндинге, не дойдя до git.
function Invoke-Git {
    param([string[]] $Arguments)
    # ⚠️ git пишет в stderr и когда всё хорошо (push печатает туда прогресс). При
    #    $ErrorActionPreference = 'Stop' PowerShell 5.1 превращает такую строку
    #    в NativeCommandError и роняет скрипт на успешной команде — судить только по коду.
    $prevPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & git -C $root @Arguments 2>&1
    } finally {
        $ErrorActionPreference = $prevPreference
    }
    return [pscustomobject]@{ Code = $LASTEXITCODE; Text = ($output -join "`n").Trim() }
}

function Invoke-GitOrThrow {
    param([string] $Stage, [string[]] $Arguments)
    $result = Invoke-Git -Arguments $Arguments
    # ${Stage}, а не $Stage: двоеточие сразу после имени PowerShell читает как имя диска.
    if ($result.Code -ne 0) { throw "${Stage}: git вернул $($result.Code)`n$($result.Text)" }
    return $result.Text
}

function Get-ExtensionVersion {
    $path = Join-Path $root 'src\Configuration.xml'
    if (-not (Test-Path $path)) { throw "не найден $path" }
    $xml = [xml](Get-Content -Raw -Encoding UTF8 $path)
    $version = $xml.MetaDataObject.Configuration.Properties.Version
    if (-not $version) { throw 'в src\Configuration.xml не заполнен <Version>' }
    if ($version -notmatch '^\d+\.\d+\.\d+\.\d+$') { throw "версия '$version' не в формате N.N.N.N" }
    return $version
}

function Find-Ibcmd {
    # Формат исходников 2.17 ⟹ читает его любая платформа 8.3.20 и старше, но собирать
    # предпочтительно платформой из «родного» диапазона 8.3.20–8.3.24.
    $candidates = Get-ChildItem 'C:\Program Files\1cv8' -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^\d+\.\d+\.\d+\.\d+$' -and (Test-Path (Join-Path $_.FullName 'bin\ibcmd.exe')) } |
        Sort-Object { [version] $_.Name }
    if (-not $candidates) { throw 'не нашёл ни одной платформы с ibcmd.exe — укажите путь параметром -Bin' }

    $native = $candidates | Where-Object {
        $v = [version] $_.Name
        $v -ge [version] '8.3.20' -and $v -lt [version] '8.3.25'
    } | Select-Object -Last 1
    if ($native) { return (Join-Path $native.FullName 'bin\ibcmd.exe') }

    $fallback = $candidates | Select-Object -Last 1
    Write-Warning ("платформы 8.3.20–8.3.24 нет, собираю на {0} — проверьте, что .cfe ставится в целевую базу" -f $fallback.Name)
    return (Join-Path $fallback.FullName 'bin\ibcmd.exe')
}

function Get-RepoSlug {
    # ⚠️🔴 gh определяет репозиторий по ТЕКУЩЕМУ каталогу, а не по каталогу скрипта. Запуск
    #    из другого репозитория (сессия агента, любой другой cwd) молча создаёт релиз ТАМ —
    #    проверено, релиз уехал в соседний репозиторий. Слаг берём из origin и передаём явно.
    $url = Invoke-GitOrThrow 'origin url' @('remote', 'get-url', 'origin')
    if ($url -notmatch 'github\.com[:/](?<slug>[^/]+/[^/]+?)(\.git)?$') {
        throw "не разобрал адрес origin ('$url') — без явного --repo релиз уйдёт в репозиторий текущего каталога"
    }
    return $Matches['slug']
}

function Get-LastReleaseTag {
    $text = (Invoke-Git -Arguments @('tag', '--list', 'v*', '--sort=-v:refname')).Text
    if (-not $text) { return $null }
    foreach ($line in $text -split "`n") {
        $tag = $line.Trim()
        if ($tag -match '^v\d+\.\d+\.\d+\.\d+$') { return $tag }
    }
    return $null
}

function New-ReleaseNotes {
    param([string] $Version, [string] $PrevTag, [string] $CfeName, [string] $Sha, [string] $Slug)

    $range = if ($PrevTag) { "$PrevTag..HEAD" } else { 'HEAD' }
    $log = (Invoke-Git -Arguments @('log', '--no-merges', '--pretty=format:- %s', $range)).Text
    $lines = if ($log) { @($log -split "`n") } else { @() }
    if ($lines.Count -gt 30) {
        $lines = $lines[0..29] + @("- …и ещё $($lines.Count - 30) коммит(ов), см. историю")
    }

    $sb = [Text.StringBuilder]::new()
    [void]$sb.AppendLine('## Что изменилось')
    [void]$sb.AppendLine()
    if ($lines.Count) {
        foreach ($line in $lines) { [void]$sb.AppendLine($line) }
    } else {
        [void]$sb.AppendLine('- без изменений в истории коммитов')
    }
    [void]$sb.AppendLine()
    if ($PrevTag) {
        [void]$sb.AppendLine("Полный список изменений: [$PrevTag...v$Version](https://github.com/$Slug/compare/$PrevTag...v$Version)")
        [void]$sb.AppendLine()
    }
    [void]$sb.AppendLine('## Установка')
    [void]$sb.AppendLine()
    [void]$sb.AppendLine("Файл ``$CfeName`` — готовое расширение, ставится в базу без конфигуратора.")
    [void]$sb.AppendLine("Требования и настройка (свойства объектов, отпечаток КЭП) — в [README](https://github.com/$Slug#readme).")
    [void]$sb.AppendLine()
    [void]$sb.AppendLine('SHA-256:')
    [void]$sb.AppendLine()
    [void]$sb.AppendLine('```')
    [void]$sb.AppendLine("$Sha  $CfeName")
    [void]$sb.AppendLine('```')
    return $sb.ToString()
}

$prevEncoding = [Console]::OutputEncoding
try {
    # Отчёты git и checks.py — на русском; без этого в PowerShell 5.1 приезжают вопросики.
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $env:PYTHONIOENCODING = 'utf-8'

    Write-Step 'состояние рабочей копии'
    $branch = Invoke-GitOrThrow 'текущая ветка' @('rev-parse', '--abbrev-ref', 'HEAD')
    if ($branch -ne $Branch) {
        throw "релиз собирается из ветки '$Branch', а сейчас '$branch' (раздаём указатель на default-ветку — из feature-ветки релиз выпускать нельзя)"
    }
    $dirty = (Invoke-Git -Arguments @('status', '--porcelain')).Text
    if ($dirty) {
        throw "рабочая копия не чиста — собранный .cfe не будет соответствовать тегу:`n$dirty"
    }

    Write-Step 'синхронизация с origin'
    Invoke-GitOrThrow 'fetch' @('fetch', '--tags', '--prune', 'origin') | Out-Null
    $local = Invoke-GitOrThrow 'HEAD' @('rev-parse', 'HEAD')
    $remote = Invoke-GitOrThrow 'origin' @('rev-parse', "origin/$Branch")
    if ($local -ne $remote) {
        throw "локальная $Branch разошлась с origin/$Branch — сначала push/pull, иначе тег укажет не на то, что увидят получатели"
    }
    $slug = Get-RepoSlug
    Write-Host "  HEAD = $($local.Substring(0,8)), совпадает с origin/$Branch"
    Write-Host "  репозиторий: $slug"

    $version = Get-ExtensionVersion
    $tag = "v$version"
    Write-Step "версия расширения: $version (тег $tag)"

    $existingLocal = (Invoke-Git -Arguments @('rev-parse', '-q', '--verify', "refs/tags/$tag")).Code -eq 0
    $existingRemote = Invoke-GitOrThrow 'ls-remote' @('ls-remote', '--tags', 'origin', "refs/tags/$tag")
    if ($existingLocal -or $existingRemote) {
        throw "тег $tag уже существует — поднимите <Version> в src\Configuration.xml (4-й разряд = исправление, 3-й = изменение поведения)"
    }

    if (-not $SkipChecks) {
        Write-Step 'проверки исходников (build\checks.py)'
        $python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
        if ($python) {
            & $python.Source (Join-Path $PSScriptRoot 'checks.py')
            if ($LASTEXITCODE -ne 0) { throw "проверки не пройдены (см. выше); осознанный пропуск — ключ -SkipChecks" }
        } else {
            Write-Warning 'python не найден — проверки пропущены'
        }
    }

    if (-not $Bin) { $Bin = Find-Ibcmd }
    if (-not $Out) { $Out = Join-Path $root 'dist' }
    New-Item -ItemType Directory -Force $Out | Out-Null

    $cfeName = "PlatformaEDO-$version.cfe"
    $cfePath = Join-Path $Out $cfeName
    Write-Step "сборка $cfeName (платформа: $Bin)"
    & (Join-Path $PSScriptRoot 'build.ps1') -Bin $Bin -Src (Join-Path $root 'src') -Out $cfePath
    if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) { throw 'сборка не удалась' }

    $sha = (Get-FileHash -Algorithm SHA256 $cfePath).Hash.ToLower()
    $shaPath = "$cfePath.sha256"
    [IO.File]::WriteAllText($shaPath, "$sha  $cfeName`n", [Text.UTF8Encoding]::new($false))
    Write-Host "  SHA-256 $sha"

    $prevTag = Get-LastReleaseTag
    if ($NotesFile) {
        if (-not (Test-Path $NotesFile)) { throw "не найден файл заметок: $NotesFile" }
        $notes = Get-Content -Raw -Encoding UTF8 $NotesFile
    } else {
        $notes = New-ReleaseNotes -Version $version -PrevTag $prevTag -CfeName $cfeName -Sha $sha -Slug $slug
    }
    $notesPath = Join-Path $Out "release-notes-$version.md"
    [IO.File]::WriteAllText($notesPath, $notes, [Text.UTF8Encoding]::new($false))

    Write-Step 'заметки релиза'
    Write-Host $notes

    if ($DryRun) {
        Write-Host ''
        Write-Host "сухой прогон: тег не создан, релиз не опубликован"
        Write-Host "  артефакт: $cfePath"
        Write-Host "  заметки:  $notesPath"
        return
    }

    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw 'не найден gh — установите GitHub CLI или создайте релиз вручную из dist\'
    }

    Write-Step "тег $tag"
    Invoke-GitOrThrow 'tag' @('tag', '-a', $tag, '-m', "ЦентрУправленияЭДО $version") | Out-Null
    try {
        Invoke-GitOrThrow 'push тега' @('push', 'origin', $tag) | Out-Null
    } catch {
        # Откатывать локальный тег можно, только если на origin его действительно нет:
        # иначе снимаем свою единственную ссылку на уже опубликованный тег.
        $onRemote = (Invoke-Git -Arguments @('ls-remote', '--tags', 'origin', "refs/tags/$tag")).Text
        if ($onRemote) {
            Write-Warning "тег $tag на origin уже есть — локальный оставлен, доделать релиз: gh release create $tag"
        } else {
            Invoke-Git -Arguments @('tag', '-d', $tag) | Out-Null
        }
        throw
    }

    Write-Step "релиз на GitHub ($slug)"
    $ghArgs = @('release', 'create', $tag, $cfePath, $shaPath,
        '--repo', $slug, '--title', "ЦентрУправленияЭДО $version", '--notes-file', $notesPath)
    if ($Draft) { $ghArgs += '--draft' }
    # gh печатает ход загрузки в stderr — судим по коду возврата, как и с git.
    $prevPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & gh @ghArgs
    } finally {
        $ErrorActionPreference = $prevPreference
    }
    if ($LASTEXITCODE -ne 0) {
        throw "gh release create вернул $LASTEXITCODE — тег $tag уже запушен, после починки достаточно повторить gh release create"
    }

    Write-Host ''
    Write-Host "готово: релиз $tag опубликован"
}
finally {
    [Console]::OutputEncoding = $prevEncoding
}
