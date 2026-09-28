# Odds Scraper API（VPS 自爬服務）

用 Playwright 即場爬 OddsPortal 賠率嘅小服務，供 Polymarket tracker 嘅「場外賠率快照」關鍵字搜尋用。

## 架構

```
tracker（私人版／公開版）
   │  GET /api/snapshot?q=arsenal+chelsea   (header: X-API-Token)
   ▼
VPS 上的 FastAPI → Playwright(Chromium) → oddsportal.com → JSON
```

## VPS 規格

- OS：Ubuntu 22.04 / 24.04
- 最低：2 vCPU / **2GB RAM**（Chromium 食 RAM，1GB 好易爆）
- 地區決定你睇到咩莊家（同之前手動快照一樣，係 IP 地域過濾）：
  - **歐洲機** → Pinnacle、歐洲莊家
  - **英國機** → William Hill、Betfair 等英國莊家
  - **美國機** → DraftKings、FanDuel、BetMGM 等（之前 sample 就係咁）
- 供應商唔拘：Hetzner、DigitalOcean、Vultr 等最平嘅 plan 就夠

## 部署

### A. Zeabur（建議：自動有 HTTPS domain）

Zeabur 冇 SSH，唔使傳 key。用 Dockerfile 部署：

1. **開 2 個 project**（region 開咗改唔到，唔好揀錯）：
   - Project 1：region 選 **Frankfurt（eu-central）** → 做 aggregator
   - Project 2：region 選 **California（us-west）** → 做 leaf
2. 每個 project → Add Service → 上傳呢個目錄（`Dockerfile`、`app.py`、`scraper.py`、`requirements.txt`）或連 GitHub repo，Zeabur 會自動用 Dockerfile 起。
3. 每個 service 設環境變數：
   - `API_TOKEN`＝自己作個長密碼（兩個 service 用同一個）
   - `REGION`＝`eu`（Frankfurt）／`us`（California）
   - `PEERS`＝只喺 **eu** 嗰個填 `https://<us-service>.zeabur.app`（us 嗰個唔使填）
   - `PORT` 唔使理，Zeabur 會自動注入。
4. 每個 service 開一條 public domain（Zeabur 自動配 HTTPS）。
5. 驗證（瀏覽器開都得）：
   ```
   https://<eu-service>.zeabur.app/api/health
   ```

### B. 普通 VPS（約 10 分鐘）

1. 開好 VPS，記低 IP。
2. 將 `app.py`、`scraper.py`、`requirements.txt`、`deploy.sh` 傳上 VPS（或叫 Muse 幫你 deploy——畀部機嘅 SSH access 我就得，public key 問我攞）。
3. VPS 上執行：
   ```bash
   API_TOKEN='自己作個長密碼' sudo -E bash deploy.sh
   ```
4. 開 firewall port `8077`（或前面加 nginx/Caddy 做 HTTPS，建議）。
5. 驗證：
   ```bash
   curl -H "X-API-Token: 你的密碼" "http://<VPS-IP>:8077/api/snapshot?q=arsenal+chelsea"
   ```

## API

| Endpoint | 用途 |
|---|---|
| `GET /api/health` | 健康檢查（唔使 token） |
| `GET /api/search?q=關鍵字` | 回傳相關比賽列表（揀啱邊場） |
| `GET /api/odds?url=<比賽頁URL>` | 指定比賽嘅 1X2 賠率快照 |
| `GET /api/snapshot?q=關鍵字` | 一步到位：搜尋＋攞第一場嘅賠率 |

`/api/snapshot` 回傳嘅 `snapshot` 物件同 tracker 入面嘅快照格式完全一致（`home`/`away`/`books`/`cons`/`best`…），tracker 可以直接 render。

結果會 cache 5 分鐘（`CACHE_TTL`），唔會每次打都去爬。

## 多地區合併（攞齊所有莊家）

OddsPortal 按訪客 IP 地區過濾莊家，單一地區睇唔齊。要「所有莊家」就要喺多個地區各開一部，一部做 aggregator 合併：

1. 每個地區部署一個 service（同一組 `API_TOKEN`），其中一部做 aggregator（建議歐洲）：
   - Zeabur：在 eu 嗰個 service 嘅環境變數加 `PEERS='https://<us-service>.zeabur.app'`。
   - 普通 VPS：`REGION=eu PEERS='http://<us-ip>:8077' API_TOKEN='...' sudo -E bash deploy.sh`。
   其餘做 leaf，唔使設 `PEERS`（設咗都唔會遞歸，但冇必要）。
2. Tracker 只需連 aggregator 嗰部（`REGION=eu` 嗰部）。
3. `/api/snapshot` 會並行問晒所有地區，按莊家名合併（去重），再重新計綜合概率同最佳賠率；回傳有 `"regions": N` 話你知合併咗幾多個地區。某地區失敗唔會拖死成個 request（skip 咗佢）。

建議起 2 部先（EU＋US），已覆蓋絕大部分主流莊家（Pinnacle、Bet365、DraftKings 等）；唔夠先加 UK。

## Tracker 設定

兩個版本嘅「場外賠率快照」區都有「搜尋設定」：
填 **API 地址**（如 `http://<VPS-IP>:8077`）同 **Token**，存在瀏覽器 localStorage（唔會跟住公開版 HTML 周圍去）。填好之後打關鍵字撳「搜尋賠率」就得。

## 常見問題

- **Cloudflare 擋爬蟲**：scraper 已做基本反偵測（user-agent、webdriver 隱藏），但機房 IP 始終有風險。第一次 deploy 可能要一齊 debug（例如轉 headless=False + xvfb）。
- **OddsPortal 改版**：scraper 係按「莊家名＋3 個賠率」嘅通用 table 結構去 parse，唔係 hardcode 死 selector；真係大改版先至要執。
- **慢**：一次完整搜尋＋爬大約 15–45 秒，屬正常；頁面會顯示載入中。
- **地區**：想睇 Pinnacle 就開歐洲機；之前嘅利物浦 sample 係美國視角，得 4 間美國莊家。
