# tw-gig-dispatch｜地推／外包夥伴派工系統

一群按件計酬的夥伴（工讀生、兼職地推、外包業務）去拜訪店家，你需要知道：**誰負責哪幾家、拜訪結果如何、這個月該付誰多少錢**。

```
匯入店家名單 ──▶ 依區域自動派工 ──▶ 夥伴手機開網頁看任務
                                         │
                                         ▼
                                    拜訪 → 回報結果（聯絡人、電話、備註）
                                         │
                           ┌─────────────┴─────────────┐
                           ▼                           ▼
                      回報費自動計算               管理員核實（成交填實收金額）
                                                       │
                                                       ▼
                                              每月結算 CSV（匯款／簽收用）
```

- 夥伴**不用註冊、不用裝 App**：手機開網址、輸入 6 碼代碼就能看任務。
- **一家店同時只派給一個人**，不會兩人重複跑同一家搶獎金。
- **防灌水**：沒留聯絡人電話的回報不計酬、每人每天有上限、要管理員核實才入帳、成交要實收達門檻才發成交獎金。
- 獎金規則寫在 `rules.json`，改數字就好，不用改程式。
- 資料存在你電腦的 SQLite，不上傳到別人的雲端。

> 範例的店家、電話、地址都是虛構的。獎金規則是可調整的預設值；你和夥伴之間是承攬、居間還是僱傭，原則上依實際約定與工作情形認定，相關勞動、扣繳與申報義務請洽專業人士確認。

---

## 適合誰

- 用兼職夥伴**掃街開發店家**的新品牌（飲料、食品、清潔用品、在地服務）
- 派人去**發傳單、做市調、拜訪通路**的小團隊
- 學校社團、地方創生團隊做在地店家訪查

---

## 需要哪些金鑰？

**不需要任何外部服務的金鑰。** 只要自己設一組**管理密碼**（`.env` 的 `DISPATCH_ADMIN_TOKEN`，至少 8 個字，建議 12 字以上英數混合）。
不知道怎麼想，可以讓電腦幫你產生一組：

```bash
python -c "import secrets; print(secrets.token_urlsafe(12))"
```

把印出來的那串貼到 `.env`，管理頁 `/admin` 用它登入。密碼不要給夥伴；外流就改一組新的並重新啟動。

---

## 怎麼用

**需要：** Python 3.10 以上（[python.org](https://www.python.org/downloads/)，Windows 安裝時勾選「Add python.exe to PATH」）。

1. 下載這個專案（綠色 **Code** 按鈕 → **Download ZIP**），解壓縮，在資料夾裡執行：

   ```bash
   pip install -r requirements.txt
   ```

2. 把 `.env.example` 複製成 `.env`，填 `DISPATCH_ADMIN_TOKEN`（管理密碼，至少 8 個字）。
3. 啟動：
   - **Windows**：雙擊 **`背景啟動.vbs`**（背景執行，不跳黑色視窗；紀錄在 `data\dispatch.log`）。停止用 **`停止.vbs`**。
   - **macOS／Linux**：`python3 app.py`
4. 打開 http://localhost:18801/admin ，用管理密碼登入。
5. **管理夥伴** → 新增夥伴（可填負責區域，留空＝全區都能派）→ 把產生的 10 碼代碼私下發給他（舊版的 6 碼代碼照常可用；代碼外流或換手機，按「換新代碼」舊的立刻失效）。
6. **匯入店家** → 上傳 CSV（格式見 `shops.example.csv`，可以先用它試）。
7. 回儀表板按 **自動派工**。
8. 夥伴打開 http://你的網址:18801/ 輸入代碼 → 看任務 → 拜訪完按「回報結果」。
9. 你在儀表板「待核實」按 **核實**（成交要填實收金額），月底下載**獎金結算 CSV**。

### 讓夥伴在外面用手機開

預設只開在本機（`127.0.0.1`）。要讓夥伴從外面連，可以用 [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) 開一條通道：

```bash
cloudflared tunnel --url http://localhost:18801
```

把印出來的網址傳給夥伴即可。夥伴頁只靠代碼辨識，**請別把代碼公開貼在群組**；有人離開就到「管理夥伴」把他停用（他還沒拜訪的店會退回待派）。同一個來源猜錯代碼 20 次會被暫時擋 5 分鐘。

用 cloudflared／反向代理對外時，請在 `.env` 加 `DISPATCH_TRUST_PROXY=1`（讓登入鎖定與猜碼鎖定看到真正的來源 IP；**沒有代理時不要開**，否則標頭可被偽造）；走 https 時再加 `DISPATCH_COOKIE_SECURE=1`。

---

## 店家 CSV 格式

```csv
店名,類型,區域,地址,電話,備註,優先
示範咖啡一號店,咖啡廳,北區,示範市北區示範路1號,0900-000-001,門口有冰箱,1
```

- `優先`：1 最優先，派工時先派。沒填當 3。
- 欄位名也可以用英文：`name, category, area, address, phone, note, priority`。
- 同一區域同名的店會自動略過，可以重複匯入。
- Excel 存檔請選「CSV UTF-8（逗號分隔）」。

## 獎金規則（`rules.json`）

| 設定 | 預設 | 說明 |
|---|---|---|
| `visit_fee` | 20 | 有效拜訪回報費（要有聯絡人姓名＋像樣的電話〔7 碼以上數字〕或 LINE ID；同一位夥伴重複填同一支電話，第二筆起不計；「店家不存在」不計） |
| `daily_visit_cap` | 15 | 每人每天最多幾筆算錢（以台灣時間 UTC+8 的日期計） |
| `close_bonus` | 400 | 成交獎金，管理員核實時填的實收金額達門檻才加發 |
| `min_close_amount` | 1000 | 成交獎金門檻 |
| `per_worker` | 10 | 每人同時最多幾家待拜訪 |
| `notes` | | 顯示給夥伴看的其他說明 |

---

## 給開發者

| 檔案 | 用途 |
|---|---|
| `app.py` | Flask 網頁：夥伴端、管理端（登入、CSRF 檢查、輸出自動跳脫） |
| `models.py` | SQLite 資料層、派工、獎金與結算邏輯 |
| `rules.json` | 獎金規則 |
| `tests/` | `python -m unittest discover -s tests` |

## 授權

[Apache License 2.0](LICENSE)

## 🛠️ 需要客製化？

這個工具是開源的，可以免費使用。如果你想要：
- 改成符合你們公司的流程、欄位或品牌
- 串接你現在在用的系統（LINE、Google 表單、POS、會計軟體……）
- 幫你安裝、架站、教學，或長期維護

歡迎加 LINE 跟我們聊。我們會依需求的範圍與複雜度報價，原則上以實際報價單為準。

<a href="https://lin.ee/pdYGARu"><img src="https://scdn.line-apps.com/n/line_add_friends/btn/zh-Hant.png" alt="加入好友" height="36"></a>

- 💬 LINE 官方帳號：[@812ibtir](https://lin.ee/pdYGARu)（請註明「派工系統 客製化」）
- 🏢 留白事務所：網頁設計、AI 自動化、房仲遠端助理
- 👤 作者：[林其衞 Larry](https://github.com/Larryveryhandsome)
