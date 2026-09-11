from dify_plugin import Plugin, DifyPluginEnv
import logging
# INFO 等級可在 Dify 外掛日誌看到 LINE/Dify 呼叫失敗原因；除錯時可改 DEBUG
logging.basicConfig(level=logging.INFO)
# SDK 的 TCP reader 會把每個請求（含端點設定內的 channel secret/token）整包印在 INFO 日誌，
# 調高到 WARNING 避免秘密外洩到日誌檔；需要看原始事件時再暫時改回 INFO
logging.getLogger("dify_plugin.core.server.tcp.request_reader").setLevel(logging.WARNING)

plugin = Plugin(DifyPluginEnv(MAX_REQUEST_TIMEOUT=120))

if __name__ == '__main__':
    plugin.run()
