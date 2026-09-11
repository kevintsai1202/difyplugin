"""
core 套件：Line Bot 外掛的服務層。

- constants     常數（LINE API 網址、訊息限制、逾時）
- line_client   LINE Messaging API 封裝（簽名驗證、reply/push、載入動畫、額度）
- formatting    Dify 回答 → LINE 訊息陣列（文字切段、圖片、FlexMessage）
- storage       對話 ID 儲存與 webhook 事件去重
- dify_service  透過 SDK 呼叫 Dify Chatflow、上傳檔案

endpoints/linebot.py 只負責接收 webhook 與流程調度（controller），
實際邏輯都在這裡，方便獨立測試。
"""
