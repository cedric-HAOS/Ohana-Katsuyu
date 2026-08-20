param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$buildRoot = Join-Path $repositoryRoot "build\windows"
$payloadRoot = Join-Path $buildRoot "payload"
$iconPath = Join-Path $buildRoot "ohana.ico"
$ageArchive = Join-Path $buildRoot "age-v1.3.1-windows-amd64.zip"
$ageExtracted = Join-Path $buildRoot "age"
$versionInfoPath = Join-Path $buildRoot "version_info.txt"
$ageUrl = "https://github.com/FiloSottile/age/releases/download/v1.3.1/age-v1.3.1-windows-amd64.zip"
$ageSha256 = "c56e8ce22f7e80cb85ad946cc82d198767b056366201d3e1a2b93d865be38154"

New-Item -ItemType Directory -Force -Path $buildRoot, $payloadRoot | Out-Null
$version = (& $Python -c "from ohana_katsuyu import __version__; print(__version__)").Trim()
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$parts = $version.Split('.')
if ($parts.Count -ne 3) { throw "Katsuyu version must contain three numeric parts" }
$versionTuple = "($($parts[0]), $($parts[1]), $($parts[2]), 0)"
$versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=$versionTuple,
    prodvers=$versionTuple,
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Ohana'),
        StringStruct('FileDescription', 'Ohana Katsuyu'),
        StringStruct('FileVersion', '$version'),
        StringStruct('InternalName', 'Ohana-Katsuyu'),
        StringStruct('ProductName', 'Ohana Katsuyu'),
        StringStruct('ProductVersion', '$version')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
[System.IO.File]::WriteAllText(
    $versionInfoPath,
    $versionInfo,
    [System.Text.UTF8Encoding]::new($false)
)
& $Python (Join-Path $repositoryRoot "scripts\export_icon.py") $iconPath
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path -LiteralPath $ageArchive)) {
    Invoke-WebRequest -Uri $ageUrl -OutFile $ageArchive
}
$actualAgeSha256 = (Get-FileHash -LiteralPath $ageArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualAgeSha256 -ne $ageSha256) {
    throw "age v1.3.1 archive checksum mismatch"
}
if (Test-Path -LiteralPath $ageExtracted) {
    $resolvedBuild = [System.IO.Path]::GetFullPath($buildRoot)
    $resolvedAge = [System.IO.Path]::GetFullPath($ageExtracted)
    if (-not $resolvedAge.StartsWith($resolvedBuild, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clear age outside build root"
    }
    Remove-Item -LiteralPath $resolvedAge -Recurse -Force
}
Expand-Archive -LiteralPath $ageArchive -DestinationPath $ageExtracted
$ageExecutable = Get-ChildItem -LiteralPath $ageExtracted -Filter age.exe -Recurse | Select-Object -First 1
if ($null -eq $ageExecutable) { throw "age.exe is missing from the verified archive" }

& $Python -m PyInstaller --noconfirm --clean --onefile --noconsole `
    --name KatsuyuWorker --icon $iconPath --version-file $versionInfoPath `
    (Join-Path $repositoryRoot "scripts\worker_entry.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m PyInstaller --noconfirm --clean --onefile --noconsole `
    --name KatsuyuTray --icon $iconPath --version-file $versionInfoPath `
    (Join-Path $repositoryRoot "scripts\tray_entry.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Copy-Item -LiteralPath (Join-Path $repositoryRoot "dist\KatsuyuWorker.exe") -Destination $payloadRoot -Force
Copy-Item -LiteralPath (Join-Path $repositoryRoot "dist\KatsuyuTray.exe") -Destination $payloadRoot -Force
Copy-Item -LiteralPath $ageExecutable.FullName -Destination (Join-Path $payloadRoot "age.exe") -Force
Copy-Item -LiteralPath (Join-Path $repositoryRoot "vendor\age-LICENSE.txt") -Destination $payloadRoot -Force

& $Python -m PyInstaller --noconfirm --clean --onefile --noconsole --uac-admin `
    --name KatsuyuSetup --icon $iconPath --version-file $versionInfoPath `
    --add-data "${payloadRoot};payload" `
    (Join-Path $repositoryRoot "scripts\setup_entry.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$setupPath = Join-Path $repositoryRoot "dist\KatsuyuSetup.exe"
$setupHash = (Get-FileHash -LiteralPath $setupPath -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllLines(
    (Join-Path $repositoryRoot "dist\SHA256SUMS"),
    @("$setupHash  KatsuyuSetup.exe"),
    [System.Text.Encoding]::ASCII
)
Get-FileHash -LiteralPath $setupPath -Algorithm SHA256
