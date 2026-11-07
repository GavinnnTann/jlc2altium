<#
.SYNOPSIS
    Set up jlc2altium on Windows: dependencies, Python environments, the
    altium-mcp server for Claude Code, skills, and command shims on PATH.

.DESCRIPTION
    Safe to run again: existing clones, environments, registrations and skill
    links are left alone. Run from the repo folder:
        powershell -ExecutionPolicy Bypass -File .\install.ps1 [-FetchDb]

.PARAMETER SrcDir
    Where altium-mcp and LCSC2Altium are cloned. Default: $HOME\src

.PARAMETER FetchDb
    Download the jlcparts catalogue (about 300 MB download, 2.3 GB on disk).

.PARAMETER McpPython
    Python 3.10-3.13 that runs altium-mcp. Default: python on PATH if it
    qualifies, otherwise a uv-managed 3.12.

.PARAMETER Unpinned
    Clone the latest upstream commits instead of the tested ones below.
    jlc2altium uses internals of altium-mcp and LCSC2Altium, so newer
    commits can break it.
#>
param(
    [string]$SrcDir = (Join-Path $HOME 'src'),
    [switch]$FetchDb,
    [string]$McpPython = '',
    [switch]$Unpinned
)
$ErrorActionPreference = 'Stop'
$Repo = $PSScriptRoot
function Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }

# ------------------------------------------------------------------ prerequisites
foreach ($tool in @('git', 'uv')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool is required. Install it with: winget install $(if ($tool -eq 'git') {'Git.Git'} else {'astral-sh.uv'})"
    }
}
$altium = Get-ChildItem 'HKLM:\SOFTWARE\Altium\Builds' -ErrorAction SilentlyContinue |
    ForEach-Object { Get-ItemProperty $_.PSPath } | Where-Object { $_.ProgramsInstallPath } | Select-Object -First 1
if ($altium) { Write-Host "Altium Designer $($altium.Version) at $($altium.ProgramsInstallPath)" }
else { Write-Warning 'Altium Designer was not found in the registry. Library tools work without it; capture needs it.' }

# ------------------------------------------------------------------ clones
# Tested commits. jlc2altium calls into these projects' internals (altium-mcp's
# server module and SchDoc reader, LCSC2Altium's builders and EasyEDA client),
# so it is pinned to the versions it was tested with. Bump these together
# with a test run of capture/examples/led_demo_spec.py.
$Pins = @{
    'altium-mcp'           = 'f7eabfad31fb28733015ee3366b8e9e3253677dd'   # 2026-11-04
    'LCSC2Altium'          = '9d2cfcdf9455a123ad84db6279e3ccccb08326c3'   # 2026-10-13
    'altium-scripts-skill' = '4dc1f7681fdb7f23f8644f7b24d10fea309c376a'   # 2026-04-24
}

function Get-Pinned([string]$Name, [string]$Url, [string]$Dest) {
    if (Test-Path $Dest) {
        $head = try { (git -C $Dest rev-parse HEAD 2>$null) } catch { 'unknown' }
        if ($Unpinned -or $head -eq $Pins[$Name]) { Write-Host "  $Name already present" }
        else {
            Write-Warning ("$Name at $Dest is at $head, not the tested commit $($Pins[$Name]). " +
                "Left as is. To switch: git -C `"$Dest`" fetch origin; git -C `"$Dest`" checkout $($Pins[$Name])")
        }
        return
    }
    if ($Unpinned) { git clone --depth 1 $Url $Dest }
    else {
        git clone --quiet $Url $Dest
        if ($LASTEXITCODE -eq 0) { git -C $Dest checkout --quiet $Pins[$Name] }
    }
    if ($LASTEXITCODE -ne 0) { throw "cloning $Name failed" }
    Write-Host "  $Name cloned$(if (-not $Unpinned) { " at $($Pins[$Name].Substring(0, 7))" })"
}

Step "Cloning altium-mcp and LCSC2Altium into $SrcDir"
New-Item -ItemType Directory -Force -Path $SrcDir | Out-Null
Get-Pinned 'altium-mcp' 'https://github.com/coffeenmusic/altium-mcp' (Join-Path $SrcDir 'altium-mcp')
Get-Pinned 'LCSC2Altium' 'https://github.com/Gort-Power/LCSC2Altium' (Join-Path $SrcDir 'LCSC2Altium')

# ------------------------------------------------------------------ tools venv
Step 'Tools environment (~\.venvs\jlc2altium, Python 3.12 for altium-monkey)'
$Venv = Join-Path $HOME '.venvs\jlc2altium'
$VPy = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $VPy)) {
    uv venv --python 3.12 $Venv
    if ($LASTEXITCODE -ne 0) { throw 'uv venv failed' }
}
uv pip install --python $VPy 'altium-monkey==2026.9.22' 'requests>=2.31' 'olefile>=0.47' -e (Join-Path $Repo 'jlcpick')
if ($LASTEXITCODE -ne 0) { throw 'installing the tools environment failed' }

# ------------------------------------------------------------------ altium-mcp venv
Step 'altium-mcp environment'
if (-not $McpPython) {
    $candidate = Get-Command python -ErrorAction SilentlyContinue
    if ($candidate) {
        $ver = & $candidate.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($ver -match '^3\.(1[0-3])$') { $McpPython = $candidate.Source }
    }
    if (-not $McpPython) {
        uv python install 3.12 | Out-Null
        $McpPython = (uv python find 3.12).Trim()
    }
}
Write-Host "  using $McpPython"
& $McpPython (Join-Path $SrcDir 'altium-mcp\start_server.py') --build
if ($LASTEXITCODE -ne 0) { throw 'building the altium-mcp environment failed (see ~\.altium-mcp\build.log)' }

# ------------------------------------------------------------------ MCP registration
Step 'Registering the altium MCP server with Claude Code (user scope)'
$claude = (Get-Command claude -ErrorAction SilentlyContinue)
if ($claude) { $claude = $claude.Source }
else {
    $claude = Get-ChildItem (Join-Path $HOME '.vscode\extensions') -Directory -Filter 'anthropic.claude-code-*' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        ForEach-Object { Join-Path $_.FullName 'resources\native-binary\claude.exe' } |
        Where-Object { Test-Path $_ } | Select-Object -First 1
}
$serverScript = Join-Path $SrcDir 'altium-mcp\start_server.py'
if ($claude) {
    $null = & $claude mcp get altium 2>&1
    if ($LASTEXITCODE -eq 0) { Write-Host '  altium is already registered' }
    else {
        & $claude mcp add --scope user altium -- $McpPython $serverScript
        if ($LASTEXITCODE -ne 0) { Write-Warning 'claude mcp add failed; register it by hand (command below).' }
    }
}
else {
    Write-Warning "Claude Code CLI not found. Register the server yourself:`n  claude mcp add --scope user altium -- `"$McpPython`" `"$serverScript`""
}

# ------------------------------------------------------------------ skills
Step 'Skills in ~\.claude\skills'
$skills = Join-Path $HOME '.claude\skills'
New-Item -ItemType Directory -Force -Path $skills | Out-Null
foreach ($s in @('jlcpick', 'altium-capture')) {
    $link = Join-Path $skills $s
    if (Test-Path $link) { Write-Host "  $s already present (left as is)" }
    else {
        New-Item -ItemType Junction -Path $link -Target (Join-Path $Repo "skills\$s") | Out-Null
        Write-Host "  linked $s -> repo"
    }
}
Get-Pinned 'altium-scripts-skill' 'https://github.com/coffeenmusic/altium-scripts-skill' (Join-Path $skills 'altium-script')

# ------------------------------------------------------------------ shims
Step 'Command shims in ~\.local\bin'
$bin = Join-Path $HOME '.local\bin'
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$shims = [ordered]@{
    'jlcpick.cmd'         = "`"$Venv\Scripts\jlcpick.exe`" %*"
    'jlc-query.cmd'       = "`"$VPy`" `"$Repo\tools\jlc_query.py`" %*"
    'lcsc2altium-cli.cmd' = "`"$VPy`" `"$Repo\tools\lcsc2altium_cli.py`" %*"
    'fetch-jlcparts.cmd'  = "`"$VPy`" `"$Repo\tools\fetch_jlcparts.py`" %*"
    'make-misc-lib.cmd'   = "`"$VPy`" `"$Repo\capture\make_misc_lib.py`" %*"
    'altium-call.cmd'     = "`"$McpPython`" `"$Repo\tools\altium_call.py`" %*"
    'capture.cmd'         = "`"$McpPython`" `"$Repo\capture\capture.py`" %*"
}
foreach ($k in $shims.Keys) {
    Set-Content -Path (Join-Path $bin $k) -Value ("@echo off`r`n" + $shims[$k]) -Encoding ASCII
    Write-Host "  $k"
}
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $bin) {
    [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ';' + $bin), 'User')
    Write-Host "  added $bin to your user PATH"
}

# ------------------------------------------------------------------ environment
Step 'Environment variables (user)'
$vars = [ordered]@{
    'JLC2ALTIUM_HOME'     = $Repo
    'JLCPICK_DB'          = (Join-Path $HOME '.cache\jlcpick\cache.sqlite3')
    'JLCPICK_LCSC2ALTIUM' = (Join-Path $bin 'lcsc2altium-cli.cmd')
    'ALTIUM_MCP_SERVER'   = (Join-Path $SrcDir 'altium-mcp\server')
    'LCSC2ALTIUM_SRC'     = (Join-Path $SrcDir 'LCSC2Altium')
}
foreach ($k in $vars.Keys) {
    [Environment]::SetEnvironmentVariable($k, $vars[$k], 'User')
    Write-Host "  $k = $($vars[$k])"
}

# ------------------------------------------------------------------ catalogue
if ($FetchDb) {
    Step 'Downloading the jlcparts catalogue'
    & $VPy (Join-Path $Repo 'tools\fetch_jlcparts.py')
}
elseif (Test-Path $vars['JLCPICK_DB']) {
    Step 'Indexing the existing parts catalogue (skipped if already done)'
    & $VPy (Join-Path $Repo 'tools\fetch_jlcparts.py') --index-only $vars['JLCPICK_DB']
}
else {
    Write-Warning 'No parts catalogue yet: run  fetch-jlcparts  (or re-run with -FetchDb).'
}

# ------------------------------------------------------------------ self-test
Step 'Self-test'
foreach ($t in @('jlcpick\tests\test_geometry.py', 'jlcpick\tests\test_easyeda_parsing.py',
                  'jlcpick\tests\test_catalog.py', 'capture\tests\test_sheetgen.py')) {
    $last = & $VPy -X utf8 (Join-Path $Repo $t) | Select-Object -Last 1
    Write-Host "  $(Split-Path $t -Leaf): $last"
}

Write-Host ''
Write-Host 'Done. Restart Claude Code (or reload VS Code) so the altium MCP server and the skills load.' -ForegroundColor Green
