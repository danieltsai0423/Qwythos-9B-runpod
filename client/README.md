# 人工測試介面（Qwythos-9B）

提供一個 **OpenAI-compatible** 的手動測試介面，同一套工具同時對應計畫的兩端：

| 目標 | base URL | 用途 |
|---|---|---|
| `local` | `http://127.0.0.1:8080/v1` | 本機 llama.cpp 伺服器（零雲端成本） |
| `runpod` | `https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1` | RunPod Serverless 部署後 |

因為兩端都講 OpenAI API，從本機測試切到雲端**只需換 `base_url` 與 `api_key`**，正是 README §3/§6 的架構。

---

## A. 本機測試（現在就能用）

### 1. 啟動本機伺服器

> ⚠️ **不要在 `run_sweep.ps1` 還在跑時啟動**，兩者都要 GPU，會 OOM。

```powershell
# 從 _local-test\scripts 目錄
powershell -ExecutionPolicy Bypass -File .\serve_local.ps1
# 進階：自訂 context 與 port
powershell -ExecutionPolicy Bypass -File .\serve_local.ps1 -Context 65536 -Port 8080
```

啟動後會有兩個介面：

- **瀏覽器聊天 UI**：<http://127.0.0.1:8080>（llama.cpp 內建，最直覺的人工測試）
- **OpenAI API**：`http://127.0.0.1:8080/v1`，model 名稱 `qwythos-9b`

KV cache 預設 `q4_0`——掃描測出在本機 8GB GPU 上最佳（每 VRAM byte 換到最長 context、131k 內無效能懸崖）。context 預設 65536（約 6.8GB VRAM，安全），VRAM 有餘裕可往 131k 調高。

### 2. 用 CLI 聊天

```powershell
# 另開一個視窗
python ..\..\client\chat_client.py --target local
# 確認連線與服務的 model：
python ..\..\client\chat_client.py --target local --list-models
# 一次性煙霧測試：
python ..\..\client\chat_client.py --target local --once "你是誰？用一句話自我介紹。"
```

---

## B. RunPod 測試（endpoint 部署後）

```powershell
$env:RUNPOD_API_KEY     = "<你的 RunPod API key>"
$env:RUNPOD_ENDPOINT_ID = "<你的 endpoint id>"
python client\chat_client.py --target runpod
```

> key 不要寫進檔案；`.gitignore` 已涵蓋常見密鑰檔，這裡用環境變數帶入即可。

---

## chat_client.py 互動指令

| 指令 | 說明 |
|---|---|
| `/help` / `/info` | 顯示指令 / 目前連線與設定 |
| `/system <text>` | 設定 system prompt（會清空對話） |
| `/reset` | 清空對話（保留 system prompt） |
| `/model <name>` | 切換 model 名稱 |
| `/stream on\|off` | 切換串流輸出 |
| `/temp <float>` | 設定 temperature（預設 0.6） |
| `/maxtokens <int>` | 設定 max_tokens（0＝交給伺服器決定） |
| `/file <path>` | 把檔案內容當作下一則訊息送出（**長 context 測試用**） |
| `/paste` | 多行貼上模式，輸入只有 `/end` 的一行結束 |
| `/exit` / `/quit` | 離開 |

每次回應後會在 stderr 印出耗時、首 token 延遲、prompt/生成 token 數與 tok/s。

---

## 長 context 人工驗證小技巧

掃描用的填充 prompt 產生器可直接拿來造長文件，丟給介面測檢索：

```powershell
python _local-test\scripts\generate_prompt.py --tokens 30000 --out long.txt
# 然後在 chat_client 裡：/file long.txt
```

檔案內含 head/mid/tail 三個密語（`aurora-head-7741` / `aurora-mid-7742` / `aurora-tail-7743`），
可請模型把三個都列出來，人工確認長 context 下的檢索是否正確。
