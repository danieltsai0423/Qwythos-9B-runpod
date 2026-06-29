# Launch Qwythos-9B locally via llama.cpp's OpenAI-compatible server (+ built-in Web UI).
#
# This gives you a hands-on test interface for the SAME model the RunPod plan targets,
# without any cloud cost:
#   - Web UI (chat in a browser):     http://127.0.0.1:8080
#   - OpenAI-compatible API base URL: http://127.0.0.1:8080/v1   (model name: qwythos-9b)
#
# The API is drop-in OpenAI-compatible, so client\chat_client.py --target local talks to
# it, and so does any OpenAI SDK by pointing base_url here.
#
# KV cache defaults to q4_0 -- the sweep's best config on this 8GB GPU (longest context
# per VRAM byte with no throughput cliff through 131k). Context defaults to 65536 (~6.8GB
# VRAM, comfortably safe) for long-doc testing out of the box; raise -Context toward 131k
# if you have VRAM to spare, or lower it if anything else needs the GPU.
#
# !! DO NOT run this while run_sweep.ps1 is active -- both want the GPU and will OOM. !!
#
# Usage (from the scripts dir):
#   powershell -ExecutionPolicy Bypass -File .\serve_local.ps1
#   powershell -ExecutionPolicy Bypass -File .\serve_local.ps1 -Context 65536 -Port 8080

param(
    [int]$Context = 65536,
    [int]$Port    = 8080,
    [string]$Host_ = "127.0.0.1",
    [string]$Ctk   = "q4_0",
    [string]$Ctv   = "q4_0",
    [int]$Ngl      = 99
)

$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot          # _local-test
$server = Join-Path $root "llamacpp\llama-server.exe"
$model  = Join-Path $root "models\Qwythos-9B-Claude-Mythos-5-1M-Q4_K_M.gguf"

if (-not (Test-Path $server)) { throw "llama-server.exe not found at $server" }
if (-not (Test-Path $model))  { throw "model GGUF not found at $model" }

Write-Host "Starting Qwythos-9B (llama-server)" -ForegroundColor Cyan
Write-Host ("  ctx={0}  kv={1}/{2}  ngl={3}" -f $Context,$Ctk,$Ctv,$Ngl)
Write-Host ("  Web UI : http://{0}:{1}" -f $Host_,$Port) -ForegroundColor Green
Write-Host ("  API    : http://{0}:{1}/v1   (model: qwythos-9b)" -f $Host_,$Port) -ForegroundColor Green
Write-Host "  Ctrl+C to stop.`n"

# Quote args with spaces (model path lives under "Road to AU\..."); same fix as the sweep.
$cargs = @(
    "-m",$model, "-c",$Context, "-ngl",$Ngl, "-fa","on",
    "--cache-type-k",$Ctk, "--cache-type-v",$Ctv,
    "-a","qwythos-9b", "--jinja",
    "--host",$Host_, "--port",$Port
)
$argLine = ($cargs | ForEach-Object {
    $t = [string]$_
    if ($t.Contains([char]0x20)) { '"' + $t + '"' } else { $t }
}) -join ' '

# Run in the foreground so Ctrl+C stops it and you see the server log live.
Start-Process -FilePath $server -ArgumentList $argLine -NoNewWindow -Wait
