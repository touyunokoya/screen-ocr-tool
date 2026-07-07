# 文字認識ツール セットアップスクリプト
# setup.bat から呼ばれます(直接実行も可)
param([switch]$NoPrompt)

$ErrorActionPreference = "Stop"
$toolDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvRoot = Join-Path $env:LOCALAPPDATA "ocr-tool"
$venvDir = Join-Path $venvRoot "venv"
$venvPy = Join-Path $venvDir "Scripts\python.exe"

Write-Host ""
Write-Host "===== 文字認識ツール セットアップ ====="
Write-Host "ツールの場所 : $toolDir"
Write-Host "部品の置き場 : $venvDir"
Write-Host "(OneDrive等の同期対象を避けるため、部品はLOCALAPPDATAに置きます)"
Write-Host ""

# ---- 1. Python 3.10〜3.12 を探す ----
function Test-PyVersion($exe, $extraArg) {
    try {
        if ($extraArg) { $out = & $exe $extraArg --version 2>$null } else { $out = & $exe --version 2>$null }
        if ($out -match "Python 3\.(10|11|12)") { return $true }
    } catch {}
    return $false
}

$pyExe = $null; $pyArg = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in @("-3.11", "-3.12", "-3.10")) {
        if (Test-PyVersion "py" $v) { $pyExe = "py"; $pyArg = $v; break }
    }
}
if (-not $pyExe) {
    $cand = Get-Command python -ErrorAction SilentlyContinue
    if ($cand -and (Test-PyVersion "python" $null)) { $pyExe = "python" }
}
if (-not $pyExe) {
    $direct = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    if ((Test-Path $direct) -and (Test-PyVersion $direct $null)) { $pyExe = $direct }
}
if (-not $pyExe) {
    Write-Host "Python が見つからないため、自動インストールします(数分かかります)..."
    winget install -e --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
    $direct = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    if (Test-Path $direct) { $pyExe = $direct }
    else { throw "Pythonの自動インストールに失敗しました。https://www.python.org/downloads/ から Python 3.11 を手動でインストールして再実行してください。" }
}
Write-Host "Python: OK ($pyExe $pyArg)"

# ---- 2. venv(専用のPython環境)を作る ----
if (-not (Test-Path $venvPy)) {
    Write-Host "専用環境を作成中..."
    New-Item -ItemType Directory -Force $venvRoot | Out-Null
    if ($pyArg) { & $pyExe $pyArg -m venv $venvDir } else { & $pyExe -m venv $venvDir }
}
if (-not (Test-Path $venvPy)) { throw "専用環境の作成に失敗しました。" }
Write-Host "専用環境: OK"

# ---- 3. 部品のインストール(約2〜4GBのダウンロード、10分前後) ----
Write-Host ""
Write-Host "部品をダウンロード中です。しばらくお待ちください(10分前後)..."
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $toolDir "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { throw "部品のインストールに失敗しました。ネット接続を確認して再実行してください。" }

# ---- 4. GPU自動判別(NVIDIA GPUがあれば高速版に差し替え) ----
$hasGpu = (Get-Command nvidia-smi -ErrorAction SilentlyContinue) -or (Test-Path "C:\Windows\System32\nvidia-smi.exe")
if ($hasGpu) {
    Write-Host ""
    Write-Host "NVIDIA GPU を検出しました。GPU高速版のAIエンジンに差し替えます(約2.5GB)..."
    & $venvPy -m pip install torch --index-url https://download.pytorch.org/whl/cu126 --upgrade --quiet
    if ($LASTEXITCODE -ne 0) { Write-Host "※GPU版の導入に失敗したため、標準版(CPU)のまま続行します" }
} else {
    Write-Host ""
    Write-Host "NVIDIA GPU が見つからないため、CPU版で動作します"
    Write-Host "(文章・数式モードは快適に動きます。混在モードだけ時間がかかります)"
}

# ---- 5. 動作確認 ----
& $venvPy -c "import onnxruntime, torch, surya, rapid_latex_ocr, winocr, keyboard, pystray, mss, pyperclip; print('check ok / GPU:', torch.cuda.is_available())"
if ($LASTEXITCODE -ne 0) { throw "動作確認に失敗しました。" }

# ---- 6. PDF丸ごと変換機能(任意) ----
if (-not $NoPrompt) {
    $ans = Read-Host "PDFを丸ごとテキスト化する機能も入れますか? 追加で約5GBダウンロードします (y/n)"
    if ($ans -match "^[yY]") {
        $mvenv = Join-Path $venvRoot "marker_venv"
        $mpy = Join-Path $mvenv "Scripts\python.exe"
        if (-not (Test-Path $mpy)) {
            if ($pyArg) { & $pyExe $pyArg -m venv $mvenv } else { & $pyExe -m venv $mvenv }
        }
        Write-Host "PDF変換の部品をダウンロード中(10分前後)..."
        & $mpy -m pip install --upgrade pip --quiet
        & $mpy -m pip install "marker-pdf" "surya-ocr>=0.17,<0.18" --quiet
        if ($hasGpu) {
            & $mpy -m pip install torch --index-url https://download.pytorch.org/whl/cu126 --upgrade --quiet
        }
        if (Test-Path (Join-Path $mvenv "Scripts\marker_single.exe")) {
            Write-Host "PDF変換機能を導入しました(トレイメニューの「PDFを丸ごとテキスト化…」)"
        } else {
            Write-Host "※PDF変換機能の導入に失敗しました(ツール本体はそのまま使えます)"
        }
    }
}

# ---- 7. 自動起動の登録(任意) ----
if (-not $NoPrompt) {
    $ans = Read-Host "PCを起動したとき、ツールも自動で立ち上げますか? (y/n)"
    if ($ans -match "^[yY]") {
        $ws = New-Object -ComObject WScript.Shell
        $startup = [Environment]::GetFolderPath("Startup")
        $sc = $ws.CreateShortcut((Join-Path $startup "文字認識ツール.lnk"))
        $sc.TargetPath = Join-Path $toolDir "start_ocr_tool.vbs"
        $sc.WorkingDirectory = $toolDir
        $sc.Save()
        Write-Host "自動起動を登録しました(解除は Win+R → shell:startup → ショートカット削除)"
    }
}

Write-Host ""
Write-Host "===== セットアップ完了! ====="
Write-Host "start_ocr_tool.vbs をダブルクリックすると起動します。"
Write-Host "※初回起動時のみ、AIモデルのダウンロード(約1.7GB)が自動で行われます。"
Write-Host "  右下に「字」アイコンが出たら準備OK。使い方は 使い方.md をご覧ください。"
Write-Host ""
if (-not $NoPrompt) {
    $ans = Read-Host "今すぐツールを起動しますか? (y/n)"
    if ($ans -match "^[yY]") { Start-Process "wscript.exe" "`"$(Join-Path $toolDir 'start_ocr_tool.vbs')`"" }
}
