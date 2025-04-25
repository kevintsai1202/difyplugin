from dify_plugin import Plugin, DifyPluginEnv
import logging
logging.basicConfig(level=logging.CRITICAL)

plugin = Plugin(DifyPluginEnv(MAX_REQUEST_TIMEOUT=1000))

if __name__ == '__main__':
    plugin.run()
