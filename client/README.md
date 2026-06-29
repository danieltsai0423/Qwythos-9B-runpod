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

## C. 本機檔案 agent（讓模型操作沙箱檔案）

`chat_client.py` 只會聊天；要讓模型**實際讀寫本機檔案**（像 coding agent），需要一層「工具執行迴圈」。這裡提供兩種，能力相同、都**嚴格 jail 在單一沙箱目錄**內：

| 工具 | 介面 | 用途 |
|---|---|---|
| `file_agent.py` | CLI | 命令列直接對模型下指令操作檔案 |
| `agent_proxy.py` | 瀏覽器 | 在 llama.cpp web UI 和伺服器中間插代理，讓**瀏覽器 UI** 也能操作檔案 |

**安全邊界（兩者共用）**：所有路徑先 `realpath`（解掉 `..` 與 symlink）再驗證必須落在沙箱內，越界一律在動硬碟前拒絕；只開放 `list_dir / read_file / write_file / make_dir`，**沒有刪除、沒有 shell**。沙箱預設 `C:\Users\User\Desktop\Road to AU\mini game`，用 `--root` 可換。

### C-1. CLI agent

```powershell
python client\file_agent.py                       # 互動模式（jail 在預設沙箱）
python client\file_agent.py --once "用 Python 寫一個猜數字遊戲存成 game.py"
python client\file_agent.py --root "C:\其他\專案"   # 換沙箱
```
每個工具動作會即時印在畫面（`· write_file(game.py) -> ok`），看得到它動了什麼。

### C-2. 瀏覽器 UI 加上檔案能力（proxy）

llama.cpp 的 web UI 是編進 `llama-server.exe` 的靜態檔、改不動，所以改用「不碰 UI」的代理法：

```
瀏覽器 (開 :8081) → agent_proxy → llama-server (:8080)
                      ↑ 工具迴圈在這裡跑，jail 在沙箱
```

```powershell
# 視窗1：模型伺服器（照舊）
powershell -ExecutionPolicy Bypass -File _local-test\scripts\serve_local.ps1 -Context 65536
# 視窗2：agent proxy（預設上游 :8080、jail 在預設沙箱）
python client\agent_proxy.py
# 然後瀏覽器開 http://127.0.0.1:8081   ← 注意是 8081，不是 8080
```

UI 用相對路徑呼叫 API，所以改開 proxy 的 port 後，每則訊息都會經過 proxy：它注入檔案工具、跑完讀寫迴圈，只把最終答案（開頭加一行 `🔧 動過哪些檔`）串流回 UI。你照常聊天，檔案操作透明發生。

> ⚠️ 這是 **9B 本地模型**：簡單建檔/讀改沒問題，複雜多步驟容易亂呼叫工具或不收尾，建議盯著。

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
