"""
ConversationStore 與 EventDeduper 的單元測試。
使用記憶體字典模擬 session.storage（get/set/delete，值為 bytes，get 不存在時拋錯）。
"""

from core.storage import ConversationStore, EventDeduper


class FakeStorage:
    """模擬 Dify 外掛 session.storage 的最小介面。"""

    def __init__(self):
        self.data = {}

    def get(self, key):
        if key not in self.data:
            raise KeyError(key)
        return self.data[key]

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


SECRET = "sec"


def test_key_prefers_group_then_room_then_user():
    store = ConversationStore(FakeStorage(), SECRET)
    assert store.key({"userId": "U", "groupId": "G"}) == f"{SECRET}_G"
    assert store.key({"userId": "U", "roomId": "R"}) == f"{SECRET}_R"
    assert store.key({"userId": "U"}) == f"{SECRET}_U"


def test_key_keeps_legacy_format_for_existing_conversations():
    """0.0.5 版的 key 是 channel_secret + '_' + id，新版必須相同才能延續既有對話。"""
    store = ConversationStore(FakeStorage(), SECRET)
    assert store.key({"userId": "U123"}) == "sec_U123"


def test_conversation_id_roundtrip_and_clear():
    fs = FakeStorage()
    store = ConversationStore(fs, SECRET)
    src = {"userId": "U"}
    assert store.get_conversation_id(src) is None
    store.set_conversation_id(src, "cid-1")
    assert store.get_conversation_id(src) == "cid-1"
    store.clear_conversation(src)
    assert store.get_conversation_id(src) is None


def test_storage_errors_do_not_propagate():
    class Broken(FakeStorage):
        def set(self, key, value):
            raise RuntimeError("storage down")
    store = ConversationStore(Broken(), SECRET)
    store.set_conversation_id({"userId": "U"}, "cid")  # 不得拋錯
    assert store.get_conversation_id({"userId": "U"}) is None


def test_deduper_flags_second_delivery_of_same_event():
    dedup = EventDeduper(FakeStorage(), SECRET)
    assert dedup.is_duplicate("evt-1") is False
    assert dedup.is_duplicate("evt-1") is True
    assert dedup.is_duplicate("evt-2") is False


def test_deduper_ignores_missing_id_and_bounds_history():
    dedup = EventDeduper(FakeStorage(), SECRET, max_items=3)
    assert dedup.is_duplicate(None) is False
    for i in range(5):
        dedup.is_duplicate(f"e{i}")
    # 最舊的已被淘汰，視為未見過
    assert dedup.is_duplicate("e0") is False
    assert dedup.is_duplicate("e4") is True


# ── ImageCache：暫存 LINE 圖片供 Dify 以公開網址抓取 ──────────

from core.storage import ImageCache  # noqa: E402


def test_image_cache_put_get_roundtrip_with_mimetype():
    cache = ImageCache(FakeStorage(), SECRET)
    token = cache.put(b"\xff\xd8jpeg", "image/jpeg")
    assert token and "/" not in token
    assert cache.get(token) == (b"\xff\xd8jpeg", "image/jpeg")


def test_image_cache_rejects_tampered_or_foreign_token():
    fs = FakeStorage()
    cache = ImageCache(fs, SECRET)
    token = cache.put(b"x", "image/png")
    assert cache.get(token[:-1] + ("a" if token[-1] != "a" else "b")) is None
    # 用不同 channel secret 簽出的 token 不能讀到本端點的圖片
    assert ImageCache(fs, "other-secret").get(token) is None
    assert cache.get("garbage") is None


def test_image_cache_expires(monkeypatch):
    import core.storage as storage_mod
    now = [1_000_000.0]
    monkeypatch.setattr(storage_mod.time, "time", lambda: now[0])
    cache = ImageCache(FakeStorage(), SECRET, ttl_seconds=60)
    token = cache.put(b"x", "image/png")
    now[0] += 61
    assert cache.get(token) is None


def test_image_cache_delete_removes_entry():
    cache = ImageCache(FakeStorage(), SECRET)
    token = cache.put(b"x", "image/png")
    cache.delete(token)
    assert cache.get(token) is None
    cache.delete(token)  # 重複刪除不得拋錯
