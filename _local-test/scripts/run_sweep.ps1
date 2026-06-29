# Context-ceiling sweep harness for Qwythos-9B on a local 8GB NVIDIA GPU (llama.cpp).
#
# For each (KV-config x context) it: generates a ~85%-full prompt with three needles
# (head/mid/tail), samples peak VRAM via nvidia-smi while llama-completion runs, parses
# prefill/decode timings, checks needle retrieval, and appends a row to results.csv.
#
# A config stops climbing the ladder at its CEILING, defined as the first of:
#   - throughput CLIFF: prefill rate drops below cliffFrac of this config's baseline
#     (on Windows the 8GB GPU oversubscribes into system RAM instead of OOMing, so the
#      real limit is a perf collapse, not a crash);
#   - TIMEOUT: a cell exceeds cellTimeoutSec (thrashing past usable);
#   - OOM / fail: model never produced a prefill-timing line.
#
# Usage (from the scripts dir):
#   powershell -ExecutionPolicy Bypass -File .\run_sweep.ps1

$ErrorActionPreference = "Stop"
$root      = Split-Path -Parent $PSScriptRoot          # _local-test
$completion = Join-Path $root "llamacpp\llama-completion.exe"
$model     = Join-Path $root "models\Qwythos-9B-Claude-Mythos-5-1M-Q4_K_M.gguf"
$promptDir = Join-Path $root "prompts"
$logDir    = Join-Path $root "results\logs"
$csv       = Join-Path $root "results\results.csv"
$genPy     = Join-Path $root "scripts\generate_prompt.py"
New-Item -ItemType Directory -Force -Path $promptDir,$logDir | Out-Null

# Recommended sampling for this model (avoid greedy / very low temp).
$sampling = @("--temp","0.6","--top-p","0.95","--top-k","20","--repeat-penalty","1.05")
$faFlag   = @("-fa","on")
$genN     = 160              # tokens/probe: enough for 3 needle lines + brief thinking
$cliffFrac = 0.40            # prefill below 40% of a config's baseline rate = off the cliff
$cellTimeoutSec = 1800       # hard per-cell cap (s); slower than this = past usable ceiling

# KV / offload configs. ngl 99 = offload all layers to GPU.
$configs = @(
    @{ label="A_f16";      ctk="f16";  ctv="f16";  ngl=99 },
    @{ label="B_q8";       ctk="q8_0"; ctv="q8_0"; ngl=99 },
    @{ label="C_q4";       ctk="q4_0"; ctv="q4_0"; ngl=99 },
    @{ label="D_q4_off";   ctk="q4_0"; ctv="q4_0"; ngl=28 }   # spill some layers to CPU/RAM
)

# Context ladder. Each config climbs until it hits its ceiling (cliff/timeout/oom).
$ladder = @(8192, 16384, 32768, 49152, 65536, 98304, 131072, 196608, 262144)

if (-not (Test-Path $csv)) {
    "label,kv_k,kv_v,ngl,ctx,prompt_tokens,status,loaded,oom,cliff,timedout,peak_vram_mib,peak_ram_gb,prefill_s,prefill_tok_s,decode_tok_s,needle_hits,needle_ok,log" |
        Out-File -FilePath $csv -Encoding utf8
}

function Get-PromptFile([int]$ctx) {
    $tok = [int]($ctx * 0.85)
    $pf  = Join-Path $promptDir ("p{0}.txt" -f $ctx)
    if (-not (Test-Path $pf)) { & python $genPy --tokens $tok --out $pf | Out-Null }
    return $pf
}

foreach ($cfg in $configs) {
    Write-Host "`n=== Config $($cfg.label)  (ngl=$($cfg.ngl), kv=$($cfg.ctk)/$($cfg.ctv)) ===" -ForegroundColor Cyan
    $base = $null   # this config's baseline prefill rate (set on first rung)
    foreach ($ctx in $ladder) {
        $pf  = Get-PromptFile $ctx
        $log = Join-Path $logDir ("{0}_ctx{1}.log" -f $cfg.label, $ctx)
        Write-Host ("  ctx={0} ... " -f $ctx) -NoNewline

        # Background VRAM/RAM sampler.
        $stop = Join-Path $env:TEMP ("sweep_stop_{0}.flag" -f $PID)
        Remove-Item $stop -ErrorAction SilentlyContinue
        $sampler = Start-Job -ScriptBlock {
            param($stopFlag)
            $maxV = 0; $maxR = 0
            while (-not (Test-Path $stopFlag)) {
                try {
                    $rawV = & nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>$null |
                        Select-Object -First 1
                    $v = $rawV -as [int]
                    if ($v -gt $maxV) { $maxV = $v }
                } catch {}
                try {
                    $os = Get-CimInstance Win32_OperatingSystem
                    $used = [math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory)/1MB,2)
                    if ($used -gt $maxR) { $maxR = $used }
                } catch {}
                Start-Sleep -Milliseconds 400
            }
            [PSCustomObject]@{ vram=$maxV; ram=$maxR }
        } -ArgumentList $stop

        $cargs = @("-m",$model,"-c",$ctx,"-ngl",$cfg.ngl) + $faFlag +
                @("--cache-type-k",$cfg.ctk,"--cache-type-v",$cfg.ctv,
                  "-f",$pf,"-n",$genN,"-no-cnv","--no-display-prompt") + $sampling
        # Start-Process -ArgumentList does NOT auto-quote array elements containing
        # spaces, and the model/prompt paths live under "Road to AU\...". Build a
        # single arg string, quoting any token with whitespace, so paths survive intact.
        $argLine = ($cargs | ForEach-Object {
            $s = [string]$_
            if ($s -match '\s') { '"' + $s + '"' } else { $s }
        }) -join ' '
        $p = Start-Process -FilePath $completion -ArgumentList $argLine -NoNewWindow -PassThru `
                 -RedirectStandardOutput "$log.out" -RedirectStandardError "$log.err"
        # Per-cell timeout: a cell thrashing KV into system RAM past this is, by
        # definition, past the usable ceiling on this box -- kill it and mark fail.
        $timedout = $false
        if (-not $p.WaitForExit($cellTimeoutSec * 1000)) {
            $timedout = $true
            try { $p.Kill() } catch {}
            $p.WaitForExit()
        }

        New-Item -ItemType File -Path $stop | Out-Null
        $peak = Receive-Job -Job $sampler -Wait -AutoRemoveJob
        Remove-Item $stop -ErrorAction SilentlyContinue

        $out = (Get-Content "$log.out" -Raw -ErrorAction SilentlyContinue) + "`n" +
               (Get-Content "$log.err" -Raw -ErrorAction SilentlyContinue)
        Set-Content -Path $log -Value $out -Encoding utf8
        Remove-Item "$log.out","$log.err" -ErrorAction SilentlyContinue

        $oom = ($out -match "out of memory|failed to allocate|CUDA error|cudaMalloc")
        # llama.cpp timing lines.
        $ptok=""; $ps=""; $ptoks=""; $dtoks=""
        if ($out -match "prompt eval time\s*=\s*([\d\.]+)\s*ms\s*/\s*(\d+)\s*tokens.*?([\d\.]+)\s*tokens per second") {
            $ps=[math]::Round([double]$Matches[1]/1000,2); $ptok=$Matches[2]; $ptoks=$Matches[3]
        }
        if ($out -match "\beval time\s*=\s*[\d\.]+\s*ms\s*/\s*\d+\s*runs.*?([\d\.]+)\s*tokens per second") {
            $dtoks=$Matches[1]
        }
        if ($out -match "\[\s*Prompt:\s*([\d\.]+)\s*t/s\s*\|\s*Generation:\s*([\d\.]+)\s*t/s\s*\]") {
            if ($ptoks -eq "") { $ptoks=$Matches[1] }
            if ($dtoks -eq "") { $dtoks=$Matches[2] }
        }

        # Multi-position needle retrieval (head / middle / tail). Case-insensitive
        # substring count, independent of the model's output formatting.
        $needleKeys = @("aurora-head-7741","aurora-mid-7742","aurora-tail-7743")
        $lout = $out.ToLower(); $hits = 0
        foreach ($k in $needleKeys) { if ($lout.Contains($k)) { $hits++ } }
        $needleOk = if ($hits -eq 3) { 1 } else { 0 }

        # Ran = produced a prefill-timing line. This (not the unreliable Start-Process
        # exit code) is the load signal.
        $ran = ($ptoks -ne "")

        # Throughput cliff: baseline is this config's first (smallest-ctx) prefill
        # rate; a drop below cliffFrac of it means we've fallen off the cliff.
        if ($ran -and $null -eq $base) { $base = [double]$ptoks }
        $cliff = ($ran -and $null -ne $base -and ([double]$ptoks -lt ($cliffFrac * $base)))

        $loaded = ($ran -and -not $oom -and -not $timedout)
        if     ($oom)      { $status = "oom" }
        elseif ($timedout) { $status = "timeout" }
        elseif (-not $ran) { $status = "fail" }
        elseif ($cliff)    { $status = "cliff" }
        else               { $status = "ok" }

        $row = @($cfg.label,$cfg.ctk,$cfg.ctv,$cfg.ngl,$ctx,$ptok,$status,
                 [int]$loaded,[int][bool]$oom,[int][bool]$cliff,[int][bool]$timedout,
                 $peak.vram,$peak.ram,$ps,$ptoks,$dtoks,$hits,$needleOk,
                 (Split-Path $log -Leaf)) -join ","
        Add-Content -Path $csv -Value $row -Encoding utf8

        if ($status -eq "ok") {
            Write-Host ("ok     prefill={0} t/s  decode={1} t/s  vram={2}MiB  needle={3}/3" -f $ptoks,$dtoks,$peak.vram,$hits) -ForegroundColor Green
        } else {
            Write-Host ("{0} (prefill={1} t/s, vram peak {2} MiB, needle {3}/3) -> ceiling for {4}" -f $status.ToUpper(),$ptoks,$peak.vram,$hits,$cfg.label) -ForegroundColor Yellow
            break  # stop climbing this config at its ceiling
        }
    }
}
Write-Host "`nDone. Results: $csv" -ForegroundColor Cyan
