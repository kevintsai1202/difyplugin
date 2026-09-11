"""
外掛共用常數。
"""

# LINE Messaging API 基底網址（一般 API 與內容下載 API 是不同主機）
LINE_API = "https://api.line.me/v2"
LINE_DATA_API = "https://api-data.line.me/v2"

# LINE 硬限制：單則文字最多 5000 字、一次 reply/push 最多 5 則
LINE_MAX_TEXT = 5000
LINE_MAX_MESSAGES = 5

# 載入動畫最長 60 秒（LINE 規定 5 的倍數、5~60）
LOADING_SECONDS_TEXT = 20
LOADING_SECONDS_IMAGE = 60

# 對 Dify 的等待上限，必須低於外掛 daemon 對 endpoint 的請求上限（預設 120 秒），
# 這樣逾時會在程式內變成可處理的例外，而不是被外層無聲切斷
DIFY_TIMEOUT_SECONDS = 110

# 對 LINE API 的 HTTP 逾時（秒）
LINE_HTTP_TIMEOUT = 10
LINE_DOWNLOAD_TIMEOUT = 30

# 去重清單最多保留的事件 ID 數量
DEDUP_MAX_ITEMS = 200

# 清除對話歷史的指令
CLEAR_COMMAND = "/clearconversationhistory"

# 圖片暫存有效秒數：Dify 抓取通常在數秒內完成，10 分鐘足夠且能限制 storage 佔用
IMAGE_CACHE_TTL_SECONDS = 600
# 外掛提供圖片的端點路徑（與 endpoints/linebot_image.yaml 一致）
IMAGE_SERVE_PATH = "/img"
