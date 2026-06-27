# Contributing / 更新流程

本 repo 的更新一律走 **PR-first** 流程,不直接 push 到 `main`。

## 標準步驟

```bash
# 1. 從最新的 main 開一個功能分支
git checkout main
git pull
git checkout -b <feature-branch>

# 2. 改檔案後 commit
git add -A
git commit -m "說明這次改動"

# 3. push 分支
git push -u origin <feature-branch>

# 4. 開 PR
gh pr create --fill

# 5. review 通過後合併,並刪除分支
gh pr merge --squash --delete-branch
```

## 分支命名建議

| 類型 | 前綴 | 範例 |
|---|---|---|
| 新增內容 | `docs/` 或 `feat/` | `docs/add-benchmark-results` |
| 修正 | `fix/` | `fix/pricing-typo` |
| 設定/工具 | `chore/` | `chore/update-gitignore` |

## 注意

- `main` 為受保護的基準分支,所有改動透過 PR 進入。
- 不要把密鑰(RunPod API key 等)寫進檔案;`.gitignore` 已涵蓋常見密鑰檔。
