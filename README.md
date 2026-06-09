# 工作週報紀錄網頁

使用 Python Flask 建立的簡潔週報管理系統，支援 Markdown 編輯、標籤、CRUD、看板/日曆模式與關鍵字搜尋。

## 功能

- 新增 / 修改 / 刪除週報
- Markdown 內容編寫（儲存為 Markdown、顯示為安全 HTML）
- 支援常用 Markdown 語法（含刪除線、表格）
- 自動草稿（瀏覽器 + DB）
- 標籤欄位（逗號分隔）與標籤集中管理
- 內容模板管理（新增/編輯/分類/預設模板）
- 附件上傳與下載
- 儀表板（週報數量、熱門標籤、每週趨勢）
- 匯出（Markdown）
- 首頁關鍵字搜尋（標題、標籤、內容）
- 看板模式與日曆模式切換

## 啟動方式

```bash
cd weekly_report_web
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

瀏覽器開啟 `http://127.0.0.1:5000`

## 結構

- `app.py`: Flask 主程式 + SQLite CRUD + 搜尋
- `templates/`: 頁面模板
- `static/`: CSS / JS
- `weekly_reports.db`: 首次啟動後自動建立
