# Qwythos-9B RunPod Serverless 按需推理部署規劃

日期：2026-06-26  
目標模型：<https://huggingface.co/empero-ai/Qwythos-9B-Claude-Mythos-5-1M>  
目標平台：RunPod Serverless  
本地使用方式：OpenAI-compatible API

## 1. 結論

可以把 Qwythos-9B 的 inference 部署到 RunPod Serverless，然後從本地端按需呼叫。設定正確時，只有本地送出請求時才會啟動 RunPod GPU worker；沒有請求且 idle timeout 到期後，worker 會自動停止，不再產生 GPU compute 費用。

建議第一版採用：

| 項目 | 建議 |
|---|---|
| Endpoint 類型 | Queue-based RunPod Serverless endpoint |
| Inference runtime | vLLM |
| Model | `empero-ai/Qwythos-9B-Claude-Mythos-5-1M` |
| Served model name | `qwythos-9b` |
| First GPU | A100 80GB |
| First context target | 64k 或 128k |
| Active workers | `0` |
| Max workers | `1` |
| Idle timeout | `5-30s` |
| FlashBoot | enabled |
| Cached model | enabled |

## 2. RunPod Serverless 是否適合

RunPod Serverless 適合這個需求，原因是：

- 可以 scale to zero：`Active workers=0` 時，沒有請求就不常駐 GPU。
- 可以從本地呼叫：透過 RunPod API 或 OpenAI-compatible endpoint。
- 可以按需使用較大 GPU：本機 RTX 4060 8GB 不適合長 context，RunPod 可以臨時租 A100/H100/H200/B200。
- 可以用 cached model 和 FlashBoot 降低冷啟動時間。

限制：

- 第一次請求會有 cold start，包含啟動容器、下載或掛載模型、載入 GPU。
- Worker 從啟動到停止都會計費，包含 cold start、執行時間、idle timeout。
- Storage / network volume 可能另計，不等同完全零成本。
- 接近 1M context 是高階 GPU 任務，不是一般 9B 短上下文部署。

## 3. 推薦架構

本地端不直接跑模型，只保留 client / agent / application。

```text
Local app / agent
  -> OpenAI-compatible request
  -> RunPod Serverless Endpoint
  -> vLLM worker
  -> Qwythos-9B on cloud GPU
```

本地 OpenAI-compatible base URL：

```text
https://api.runpod.ai/v2/{ENDPOINT_ID}/openai/v1
```

本地 model name：

```text
qwythos-9b
```

選 Queue-based endpoint 而不是一開始使用 Load Balancer，原因是長 context prefill 可能耗時較長，Queue endpoint 的 `/run`、`/runsync`、`/status`、`/stream` 模式更適合排隊與長任務追蹤。

## 4. GPU 與價格

第一輪建議使用 A100 80GB。

RunPod Serverless 官方文件目前價格：

```text
A100 80GB: $0.00076 / second
```

換算：

```text
約 $0.0456 / minute
約 $2.736 / hour
```

GPU 選擇路線：

| 階段 | GPU | 目標 |
|---|---|---|
| POC | A100 80GB | 64k / 128k context |
| 長 context | A100 80GB | 嘗試 262k |
| 高吞吐或更長 context | H100 80GB | 改善速度與穩定性 |
| 512k-1M 測試 | H200 141GB 或 B200 180GB | 做極限 context 測試 |

參考價格：

| GPU | VRAM | 每秒價格 |
|---|---:|---:|
| A6000 / A40 | 48GB | `$0.00034` |
| L40 / L40S | 48GB | `$0.00053` |
| A100 | 80GB | `$0.00076` |
| H100 | 80GB | `$0.00116` |
| H200 | 141GB | `$0.00155` |
| B200 | 180GB | `$0.00240` |

價格需在正式部署前重新核對 RunPod pricing 頁面，因為 GPU 單價可能變動。

## 5. vLLM Endpoint 設定

第一版建議環境變數：

```text
MODEL_NAME=empero-ai/Qwythos-9B-Claude-Mythos-5-1M
OPENAI_SERVED_MODEL_NAME_OVERRIDE=qwythos-9b
DTYPE=bfloat16
TRUST_REMOTE_CODE=true
MAX_MODEL_LEN=65536
GPU_MEMORY_UTILIZATION=0.90
```

長 context 壓測時再加入：

```text
KV_CACHE_DTYPE=fp8
```

逐級提升 `MAX_MODEL_LEN`：

```text
65536 -> 131072 -> 262144 -> 512000 -> 1010000
```

不要一開始就設定 1M。先確認 64k / 128k 穩定，再逐級提高，否則 cold start 後很容易在初始化 KV cache 時 OOM，浪費啟動成本。

## 6. 本地使用方式

Python OpenAI SDK 範例：

```python
from openai import OpenAI

client = OpenAI(
    api_key="RUNPOD_API_KEY",
    base_url="https://api.runpod.ai/v2/{ENDPOINT_ID}/openai/v1",
)

response = client.chat.completions.create(
    model="qwythos-9b",
    messages=[
        {"role": "user", "content": "Say hello and identify yourself briefly."}
    ],
    max_tokens=256,
)

print(response.choices[0].message.content)
```

本地應用只需要把原本 OpenAI-compatible client 的 `base_url` 和 `api_key` 換成 RunPod 版本。

## 7. KV Cache / KV Offload 處理策略

### 7.1 核心判斷

Qwythos-9B 本身只有約 9B 參數，真正讓 1M context 變困難的是 KV cache。context 越長，KV cache 佔用越高；因此部署策略重點不是只看模型權重大小，而是控制 KV cache。

### 7.2 RunPod vLLM 不以 CPU KV offload 作為主方案

RunPod vLLM 方案不採用本機 `llama.cpp` 的 `--kv-offload` / `--no-kv-offload` 旗標。vLLM 的主要策略是：

1. 用 `MAX_MODEL_LEN` 控制 KV cache 配置上限。
2. 用 `GPU_MEMORY_UTILIZATION=0.90` 避免 vLLM 初始化時吃滿顯存。
3. 長 context 壓測才啟用 `KV_CACHE_DTYPE=fp8`，降低 KV cache 佔用。
4. 如需更長 context，優先升級到更大 VRAM GPU，而不是依賴 CPU offload。

### 7.3 為什麼不優先做 CPU KV offload

CPU KV offload 可以降低 VRAM 壓力，但會把大量 KV cache 存取移到 CPU/RAM/PCIe 路徑，長 context 時 latency 會明顯變差。對按需 API 來說，這通常不是好的日常配置。

因此本計畫的正式路線是：

```text
先限制 MAX_MODEL_LEN
再啟用 fp8 KV cache
再升級 GPU VRAM
最後才考慮 CPU swap / offload fallback
```

### 7.4 vLLM fallback

如果某些 vLLM worker image 支援 CPU swap，可把 `SWAP_SPACE` 作為 fallback，但只用於避免直接失敗，不作為性能方案。

建議原則：

```text
SWAP_SPACE: only for fallback
Not for daily 1M context serving
```

### 7.5 與本機 llama.cpp 的差異

本機 RTX 4060 8GB 測試時，才會考慮 `llama.cpp` 旗標：

```text
--no-kv-offload
-ctk q4_0
-ctv q4_0
```

這代表把 KV cache 壓力更多放到系統 RAM，讓 8GB VRAM 不至於直接爆掉。但這是本機極限測試手段，不是 RunPod vLLM 的主要部署策略。

## 8. 按需使用與自動關閉設定

要達成「只有使用時才開 GPU，不用時自動關閉」，Endpoint 必須這樣設：

```text
Active workers = 0
Max workers = 1
Idle timeout = 5-30s
FlashBoot = enabled
```

行為：

1. 本地送出第一個 request。
2. RunPod 啟動 worker 和 GPU。
3. vLLM 載入 Qwythos-9B。
4. 完成推理。
5. 若 idle timeout 內沒有新請求，worker 停止。
6. worker 停止後不再產生 GPU compute 費用。

需要注意：idle timeout 期間仍會計費，因為 worker 還活著。

## 9. 測試計畫

### Phase 0: 本地 8GB context 上限驗證（已完成）

正式租 GPU 前，先在本機 **RTX 3060 8GB** 用 llama.cpp（GGUF `Q4_K_M`）驗證「KV cache 量化能換到多少可用 context」，作為 vLLM 路線的對照基準。完整方法與數據見 [`PHASE0_LOCAL_REPORT.md`](PHASE0_LOCAL_REPORT.md)、原始數據 `_local-test/results/results.csv`、harness `_local-test/scripts/run_sweep.ps1`。

關鍵結論：

- 8GB 顯卡的限制**不是 OOM crash，而是 prefill 吞吐崩塌的效能懸崖**——Windows WDDM 在顯存不足時把 KV 溢位到系統 RAM（走 PCIe），效能直接掉下去而非報錯。
- **懸崖落點由 KV cache dtype 決定，量化每減半、可用 context 約翻倍**：

  | KV dtype | 穩定可用 context | 天花板 |
  |---|---:|---:|
  | f16 | 65k | 98k（VRAM 懸崖）|
  | q8_0 | 98k | 131k（VRAM 懸崖）|
  | q4_0 | **131k** | 196k（VRAM 懸崖）|
  | q4_0 + CPU offload | 196k（但極慢）| 262k（系統 RAM 榨乾 → timeout）|

- **在懸崖點 q4_0 的 needle 檢索從 3/3 掉到 0/3**：天花板處是吞吐與檢索品質一起崩，不只是變慢。
- CPU offload（D）的瓶頸機制不同——VRAM 不會爆，但 KV 擠進系統 RAM，262k 時 16GB RAM 榨乾、prefill 跑不完，且 decode 早已掉到 6–8 t/s；實證它只能當「避免失敗」的保命 fallback。
- 這實證了本計畫的策略順序（§7）：先用量化 KV cache 換 context、CPU offload 只能保命不能當效能配置。**對應到 vLLM 即 `KV_CACHE_DTYPE=fp8` 應優先於升級 GPU**；而下方各 Phase 拉高 `MAX_MODEL_LEN` 時，必須在每一階同時檢查長 context 檢索品質，不能只看 server 沒掛。

> **本機最適配置：`q4_0` KV + 全層上 GPU（`-ngl 99`），131k context 內又快又準。**
> 開箱即用：`serve_local.ps1`（啟 llama.cpp OpenAI server，預設即此配置）＋ `client/chat_client.py` 做零成本人工測試；同一個 client 加 `--target runpod` 即可測雲端 endpoint。完整用法見 `PHASE0_LOCAL_REPORT.md` §6。

### Phase 1: 64k POC

- GPU：A100 80GB
- `MAX_MODEL_LEN=65536`
- 確認 `/models` 可列出 `qwythos-9b`
- 確認 `/chat/completions` 可正常回應
- 測 streaming
- 記錄 cold start 時間和每次請求成本

### Phase 2: 128k / 262k

- 逐步提高 `MAX_MODEL_LEN`
- 測長文摘要
- 測 needle-in-haystack
- 記錄 prefill latency、tokens/sec、OOM 情況

### Phase 3: 512k

- 如 A100 不穩，改 H100 或 H200
- 啟用 `KV_CACHE_DTYPE=fp8`
- 測 allocation + 短生成
- 不直接判定為可日常使用

### Phase 4: 接近 1M

- 優先 H200 或 B200
- 使用 async `/run`，不要用短 timeout 的 sync 路線
- 目標先是成功啟動和短生成，再做 retrieval benchmark
- 若只有能啟動但速度不可接受，標記為 experiment only

## 10. 驗收標準

最低成功標準：

- RunPod endpoint 可從本地用 OpenAI-compatible client 呼叫。
- `Active workers=0` 時，閒置後 GPU worker 自動停止。
- A100 80GB + 64k context 穩定可用。
- 成本可以按 request / idle timeout 估算。

進階成功標準：

- 262k context 可完成長文摘要和 needle-in-haystack。
- `KV_CACHE_DTYPE=fp8` 能降低顯存壓力且輸出品質可接受。
- 512k / 1M 測試有明確 benchmark 結果，而不只是能啟動。

## 11. 風險

- Qwythos 模型宣稱 1M context，但實務上完整 1M 高品質檢索和互動速度需要高階 GPU。
- vLLM 對 Qwen3.5 / 新架構支援需要以實際 worker image 測試確認。
- 冷啟動可能比預期久，尤其模型未 cache 時。
- fp8 KV cache 可能影響長 context 精度，需要用實際任務驗證。
- Load Balancer endpoint 較低延遲，但長任務 timeout 風險較高，第一版不採用。

## 12. 參考來源

- RunPod Serverless overview: <https://docs.runpod.io/serverless/overview>
- RunPod Serverless pricing: <https://docs.runpod.io/serverless/pricing>
- RunPod endpoint settings: <https://docs.runpod.io/serverless/endpoints/endpoint-configurations>
- RunPod send requests: <https://docs.runpod.io/serverless/endpoints/send-requests>
- RunPod vLLM OpenAI compatibility: <https://docs.runpod.io/serverless/vllm/openai-compatibility>
- RunPod vLLM environment variables: <https://docs.runpod.io/serverless/vllm/environment-variables>
- Qwythos-9B model card: <https://huggingface.co/empero-ai/Qwythos-9B-Claude-Mythos-5-1M>

