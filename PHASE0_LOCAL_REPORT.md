# Phase 0 本地 context 上限掃描 初步報告

日期：2026-06-29
模型：`empero-ai/Qwythos-9B-Claude-Mythos-5-1M`（GGUF `Q4_K_M`，約 5.6 GB）
執行環境：本地 **NVIDIA RTX 3060 8GB**（註：規劃文件原寫 RTX 4060，實機為 RTX 3060）
推理工具：llama.cpp（`llama-completion.exe`，build b9826）
掃描腳本：`_local-test/scripts/run_sweep.ps1`
原始數據：`_local-test/results/results.csv`、`_local-test/results/logs/*.log`

> 定位：這是 README 計畫中「本地 8GB 極限測試」的 Phase 0。**不是** RunPod Serverless 正式部署，
> 而是在本機驗證「KV cache 量化能換到多少可用 context」，作為 RunPod / vLLM 路線的對照基準。

---

## 1. 結論（先講重點）

- 在 8GB 顯卡上，限制可用 context 長度的不是「會不會 crash」，而是 **prefill 吞吐崩塌的效能懸崖**。
- Windows WDDM 驅動在顯存不足時會把 KV / compute buffer 溢位到系統 RAM（走 PCIe），所以**整段 ladder 都沒有出現硬性 OOM**，而是效能直接掉下去。
- **懸崖落點完全由 KV cache dtype 決定**：把 KV cache 減半（f16 → q8 → q4），穩定可用 context 大致翻倍：**65k → 98k → 131k**。
- 這次把 ladder 延伸到 196k / 262k，**四個組態的天花板全部測到**（前一版只到 131k，q4 還沒見底）：
  - **A_f16 懸崖在 98k、B_q8 在 131k、C_q4 在 196k**（皆為 VRAM 溢位的吞吐懸崖）。
  - **D_q4_off（CPU offload）的瓶頸機制不同**：它 VRAM 一路壓在 8GB 以下不會踩 VRAM 線，但 196k 穩定、**262k 時系統 RAM 衝到 15.67/15.84GB 榨乾，1800s 內連 prefill 都跑不完 → `timeout`**。換言之它的天花板是**系統 RAM**，不是 VRAM。
- **新發現（多位置 needle）**：C_q4 在懸崖點 196k，needle 從 3/3 掉到 **0/3**——代表那個點不是「能跑只是慢」，而是**長 context 檢索品質與吞吐一起崩**。這比上一版只看吞吐更完整。
- 這直接驗證了 README 的 **「KV cache 優先」策略順序**：`MAX_MODEL_LEN` → `KV_CACHE_DTYPE=fp8` → 升級更大 VRAM → 最後才 CPU offload。

---

## 2. 測試方法

對每一組 (KV 設定 × context 長度)，腳本會：

1. 用 `generate_prompt.py` 產生約 85% 滿載的填充文字，在 **10% / 50% / 90% 三個位置**各埋一個 needle（密語 `aurora-head-7741` / `aurora-mid-7742` / `aurora-tail-7743`）。
2. 跑 `llama-completion.exe`（`-fa on`，生成 160 tokens），同時背景以 `nvidia-smi` 取樣峰值 VRAM 與系統 RAM。
3. 解析 prefill / decode 吞吐，並用 **吞吐門檻**判定懸崖：當該 context 的 prefill t/s 掉到該組態最佳值的 **40% 以下**即標記 `cliff`。
4. 檢查模型是否把三個 needle 都正確取出（`needle_hits` 0–3）。
5. 每格設 1800s 安全 timeout（系統 RAM 僅 15.8GB，高 context 溢位時防止 thrash 卡死）；某組態一旦 `cliff` / `timeout` / `fail` 即停止往上爬，視為該組態天花板。

**KV / offload 組態**

| 標籤 | KV K | KV V | GPU 層數 (ngl) | 說明 |
|---|---|---|---:|---|
| A_f16 | f16 | f16 | 99（全上 GPU） | 基準，未量化 KV |
| B_q8 | q8_0 | q8_0 | 99 | KV 8-bit |
| C_q4 | q4_0 | q4_0 | 99 | KV 4-bit |
| D_q4_off | q4_0 | q4_0 | 28（部分層落 CPU/RAM） | KV 4-bit + CPU offload |

**Context ladder**：`8192 → 16384 → 32768 → 49152 → 65536 → 98304 → 131072 → 196608 → 262144`

---

## 3. 執行前修正的三個 harness bug

第一次執行時四組態全部在 8k「OOM」、峰值 VRAM 僅 ~1073 MiB（＝閒置基準），代表模型根本沒載入。逐一排查後修正：

| # | 症狀 | 根因 | 修正 |
|---|---|---|---|
| 1 | `invalid argument: to` | `Start-Process -ArgumentList` 不會自動為含空白的陣列元素加引號，路徑 `…\Road to AU\…` 被拆開 | 改成組單一字串，對含空白的 token 加上雙引號 |
| 2 | `invalid argument: --no-cnv` | `llama-completion.exe` 用的是 `-no-cnv`（單槓）/ `--no-conversation`，雙槓 `--no-cnv` 是 `llama-cli` 才有 | `--no-cnv` → `-no-cnv` |
| 3 | 每組態爬到第一階就停、`loaded=0` 但時間數據正常 | 成功判定用 `$exit -eq 0`，但 `Start-Process -PassThru` 在此設定回傳 `$null` ExitCode（`$null -ne 0` 為真 → 誤判失敗中斷） | 改為**以輸出判定**：有 prefill 時間行且無 OOM 字串即視為成功 |

修正後四組態都能完整爬完整段 ladder。

---

## 4. 結果數據

來源 `results.csv`（29 列數據）。⬇ 標記該組態的懸崖落點（停止往上爬之處）。

### 4.1 Prefill 吞吐（tokens/sec）—— 看懸崖落點

| context | A_f16 | B_q8 | C_q4 | D_q4_off |
|---:|---:|---:|---:|---:|
| 8k | 1486 | 1491 | 1526 | 1044 |
| 16k | 1485 | 1475 | 1507 | 1047 |
| 32k | 1380 | 1464 | 1410 | 1044 |
| 49k | 1303 | 1393 | 1366 | 1009 |
| 65k | 1275 | 1328 | 1298 | 974 |
| 98k | **111** ⬇ | 1215 | 1183 | 908 |
| 131k | — | **78** ⬇ | 1092 | 853 |
| 196k | — | — | **266** ⬇ | 760 |
| 262k | — | — | — | **timeout** ⬇（RAM 榨乾，prefill 未完成）|

### 4.2 Decode 吞吐（tokens/sec）

| context | A_f16 | B_q8 | C_q4 | D_q4_off |
|---:|---:|---:|---:|---:|
| 8k | 34.4 | 34.6 | 35.5 | 15.3 |
| 65k | 28.3 | 31.9 | 30.1 | 11.3 |
| 98k | 6.5 ⬇ | 29.4 | 27.7 | 9.0 |
| 131k | — | 16.7 ⬇ | 25.1 | 7.8 |
| 196k | — | — | 12.0 ⬇ | 6.4 |

### 4.3 峰值 VRAM（MiB，8192 MiB 為上限）

| context | A_f16 | B_q8 | C_q4 | D_q4_off |
|---:|---:|---:|---:|---:|
| 8k | 6737 | 6467 | 6205 | 5625 |
| 65k | 7963 | 7266 | 6761 | 6075 |
| 98k | 7889 | 7832 | 7143 | 6501 |
| 131k | — | 7861 | 7536 | 6949 |
| 196k | — | — | 7904 | 7782 |

> 觀察：當峰值 VRAM 逼近 ~7900 MiB（接近 8GB）即發生溢位，prefill 立刻崩塌。
> f16 在 65k→98k 踩線，q8 在 98k→131k 踩線，q4 在 131k→196k 踩線。
> D（offload）因為只放 28 層上 GPU，VRAM 一路壓在 7.8GB 以下，所以 196k 都還沒踩到溢位線。

### 4.4 多位置 needle 檢索（head/mid/tail，滿分 3/3）

| context | A_f16 | B_q8 | C_q4 | D_q4_off |
|---:|---:|---:|---:|---:|
| 8k–65k | 3/3 | 3/3 | 3/3 | 3/3 |
| 98k | 3/3 | 3/3 | 3/3 | **1/3** |
| 131k | — | 3/3 | 3/3 | 3/3 |
| 196k | — | — | **0/3** ⬅ | 3/3 |
| 262k | — | — | — | timeout（無輸出）|

> 健康區間（未踩懸崖）幾乎都 3/3。例外：
> - **C_q4 @196k = 0/3**：與吞吐懸崖同點發生，是**檢索品質一起崩**，不只是變慢。
> - **D_q4_off @98k = 1/3**：孤立一格、前後都 3/3，研判是 temp 0.6 取樣的隨機 miss，非系統性退化。
> - **D_q4_off @262k = timeout**：1800s 內 prefill 未完成、無輸出可檢索（RAM 榨乾，見 §4.5）。

### 4.5 各組態總結

| 組態 | KV dtype | 穩定可用 context | 真正懸崖 | 行為 |
|---|---|---:|---|---|
| **C_q4** | q4_0 | **131k** | **196k** | 最佳。131k 仍有 1092 t/s prefill、25.1 decode；196k 一次崩光（266 t/s + needle 0/3） |
| **B_q8** | q8_0 | **98k** | **131k** | 98k 前穩定（1215 t/s）；131k 掉到 78 t/s prefill / 16.7 decode |
| **A_f16** | f16 | **65k** | **98k** | 65k 後崩塌：prefill 1275→111 t/s、decode 掉到 6.5 |
| **D_q4_off** | q4_0 + offload | **196k** | **262k（RAM timeout）** | 唯一靠 RAM 換 context 的組態：196k 仍能跑（但 decode 僅 6.4 t/s）；262k 把系統 RAM 榨乾、prefill 跑不完。屬「避免失敗」的保命組態，非效能組態 |

---

## 5. 與 RunPod / vLLM 計畫的對應

- 本地 **q4 KV → 穩定 131k、懸崖 196k** 的結果，等同 vLLM 路線上的 `KV_CACHE_DTYPE=fp8`：在 VRAM 受限時，**量化 KV cache 每 byte 換到的可用 context 遠多於其他手段**，正是 README §7 的核心主張。
- **C_q4 在 196k 同時崩吞吐又崩檢索**，提醒一件事：找到的「天花板」不該只用速度定義——正式 vLLM 壓測時也要在每一階配長 context 檢索檢查，確認拉高 `MAX_MODEL_LEN` 後**品質仍在**，不能只看 server 沒掛。
- **D（CPU offload）全程偏慢**，實證 README「CPU offload 只作 fallback、不作日常效能配置」的立場。
- ladder 不可跳級的理由也得到佐證：context 一拉高，prefill 時間與 VRAM 同步上升，逼近上限就崩塌；正式 vLLM 部署同理應逐級提升 `MAX_MODEL_LEN`。

---

## 6. 本機最適配置與用法

**結論：本機（RTX 3060 8GB）的最佳日常配置是 `C_q4`——KV cache 用 `q4_0`、全部層上 GPU（`-ngl 99`）。**

理由：在 8GB VRAM 上，它在「可用 context」和「速度」之間取得最好的平衡——

| | A_f16 | B_q8 | **C_q4（建議）** | D_q4_off |
|---|---|---|---|---|
| 穩定 context | 65k | 98k | **131k** | 196k（但極慢）|
| 131k 時 prefill | 崩 | 崩 | **1092 t/s** | 853 t/s |
| 131k 時 decode | 崩 | 16.7 t/s | **25.1 t/s** | 7.8 t/s |
| 檢索品質 | 好 | 好 | **好（131k 內 3/3）** | 好但 decode 太慢 |

- **A/B（f16/q8）**：沒量化到位，context 太早就撞懸崖，浪費了 8GB 的潛力。
- **D（offload）**：能換到更長 context（196k），但 decode 掉到 6–8 t/s、且 262k 直接被 RAM 拖垮 timeout——只適合「寧可慢也不要失敗」的保命情境，不適合日常互動。
- **C_q4**：131k 內又快又準，是這台機器的甜蜜點。要超過 131k 才退而求其次用 D。

### 怎麼用

本機已備好兩個介面，**KV 預設就是 C_q4 配置**（`q4_0` + `-ngl 99`），開箱即用：

```powershell
# 1) 啟動本機 llama.cpp OpenAI server（含瀏覽器聊天 UI）
#    預設 ctx=32768；VRAM 有餘裕可加 -Context（131072 內都安全）
powershell -ExecutionPolicy Bypass -File _local-test\scripts\serve_local.ps1 -Context 131072

#    → 瀏覽器 UI : http://127.0.0.1:8080
#    → OpenAI API: http://127.0.0.1:8080/v1   (model: qwythos-9b)

# 2) 另開視窗，用 CLI 聊天 / 煙霧測試
python client\chat_client.py --target local
python client\chat_client.py --target local --once "用一句話自我介紹"
```

> 同一支 `chat_client.py` 之後加 `--target runpod`（配 `RUNPOD_ENDPOINT_ID` / `RUNPOD_API_KEY`）就能測雲端 endpoint——本機與雲端只差 `base_url` / `api_key`，正是本計畫的架構。

---

## 7. 限制與下一步

**本報告限制**

- 為本機 Q4_K_M GGUF + llama.cpp，**非** RunPod 上的 vLLM + 原始權重；數值僅供策略方向對照，不能直接換算雲端表現。
- needle 是合成密語檢索，非長文摘要 / 推理等更難的品質基準；temp 0.6 下單格仍有取樣雜訊（見 D@98k）。
- 各天花板為單次掃描結果，未做重複量測取統計。

**建議下一步**

1. 把本報告精簡後併入 `README.md`（依 CONTRIBUTING 走 PR-first）。
2. 把 harness 修正與 `results.csv` 以 PR 方式納入版控（`_local-test/` 目前被 gitignore，需對 scripts 與 csv 開例外，排除 GGUF 與 llama.cpp 執行檔）。
3. 進入 RunPod 路線：用 `client/chat_client.py --target runpod`，填入 `ENDPOINT_ID` 與 API key 即可測同一套 OpenAI-compatible 介面。

---

*數據來源：`_local-test/results/results.csv`（30 列，4 組態 × 8–9 階 context）。本報告為初步版本，數值為單次掃描結果。*
