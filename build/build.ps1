# Сборка .cfe расширения "ЦентрУправленияЭДО" из XML-исходников.
# Нужна только платформа: ни рабочей базы, ни конфигуратора. Пустой файловой ИБ достаточно,
# несмотря на заимствованные объекты — они лежат в самой выгрузке расширения.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Bin,                       # путь к ibcmd.exe
    [string] $Src,                                  # каталог XML-исходников (по умолчанию ..\src)
    [string] $Out,                                  # куда положить .cfe (по умолчанию ..\PlatformaEDO.cfe)
    [string] $Name = 'ЦентрУправленияЭДО',
    [string] $Prefix = 'ЭДО_',
    [int]    $MinSizeKb = 200                                           # порог «сборка не пустая»
)

$ErrorActionPreference = 'Stop'
# В сеансе без консоли (задача планировщика, ssh) прогресс-бар роняет вызов с Win32 0x5.
$ProgressPreference = 'SilentlyContinue'

# ⚠️ $PSScriptRoot в блоке param() ещё пуст — значения по умолчанию считаем в теле скрипта.
$root = Split-Path -Parent $PSScriptRoot
if (-not $Src) { $Src = Join-Path $root 'src' }
if (-not $Out) { $Out = Join-Path $root 'PlatformaEDO.cfe' }

function Invoke-Ibcmd {
    param([string] $Stage, [string[]] $Arguments)
    Write-Host "==> $Stage"
    & $Bin @Arguments
    if ($LASTEXITCODE -ne 0) {
        # ${Stage}, а не $Stage: двоеточие сразу после имени PowerShell читает как имя диска.
        throw "${Stage}: ibcmd вернул $LASTEXITCODE"
    }
}

if (-not (Test-Path $Bin)) { throw "не найден ibcmd: $Bin" }
if (-not (Test-Path $Src)) { throw "не найдены исходники: $Src" }

$ib = Join-Path ([IO.Path]::GetTempPath()) ('pedo-build-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $ib | Out-Null
try {
    Invoke-Ibcmd 'создание файловой ИБ'   @("infobase", "create", "--data=$ib")
    Invoke-Ibcmd 'заведение расширения'   @("config", "extension", "create", "--data=$ib", "--name=$Name", "--name-prefix=$Prefix")
    # ⚠️ У config import НЕТ параметра --force (в отличие от load/export/check). С ним шаг падает
    #    exit=2, а следующий save спокойно сохраняет ПУСТОЕ расширение и возвращает 0.
    Invoke-Ibcmd 'импорт XML'             @("config", "import", "--data=$ib", "--extension=$Name", $Src)
    if (Test-Path $Out) { Remove-Item -Force $Out }
    Invoke-Ibcmd 'выгрузка cfe'           @("config", "save", "--data=$ib", "--extension=$Name", $Out)
}
finally {
    Remove-Item -Recurse -Force $ib -ErrorAction SilentlyContinue
}

# Проверка размером: пустое расширение весит единицы килобайт и на глаз неотличимо от рабочего.
$size = (Get-Item $Out).Length
if ($size -lt ($MinSizeKb * 1KB)) {
    throw ("собран подозрительно маленький cfe: {0} Б (< {1} КБ) — почти наверняка импорт не отработал" -f $size, $MinSizeKb)
}
Write-Host ("готово: {0} ({1:N0} Б)" -f $Out, $size)
