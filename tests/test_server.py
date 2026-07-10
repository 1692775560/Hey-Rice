"""后端 HTTP 服务的离线单元测试。

只测纯逻辑(会话路由、健康检查、路径穿越防护),不真正起 HTTP 服务、不联网。
用临时目录承接会话偏好文件,避免污染仓库。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import preferences  # noqa: E402
import server  # noqa: E402


class SessionRoutingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="xiaogua_srv_")
        self._orig_session_dir = server.SESSION_DIR
        self._orig_store = preferences.STORE_PATH
        server.SESSION_DIR = os.path.join(self._tmp, "sessions")
        preferences.STORE_PATH = os.path.join(self._tmp, "default_store.json")
        server._SESSIONS.clear()

    def tearDown(self):
        server.SESSION_DIR = self._orig_session_dir
        preferences.STORE_PATH = self._orig_store
        server._SESSIONS.clear()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_default_session_uses_legacy_store(self):
        self.assertEqual(server._session_store_path("default"), preferences.STORE_PATH)

    def test_named_session_uses_isolated_file(self):
        path = server._session_store_path("alice")
        self.assertTrue(path.startswith(server.SESSION_DIR))
        self.assertTrue(path.endswith("alice.json"))

    def test_session_id_sanitized_against_path_traversal(self):
        stem = server._safe_session_stem("../../etc/passwd")
        self.assertNotIn("/", stem)
        self.assertNotIn("..", stem)
        path = os.path.abspath(server._session_store_path("../../etc/passwd"))
        # 归一化后仍必须落在 SESSION_DIR 之内,不能逃出去
        self.assertTrue(path.startswith(os.path.abspath(server.SESSION_DIR)))

    def test_empty_session_stem_falls_back_to_anon(self):
        self.assertEqual(server._safe_session_stem(""), "anon")
        self.assertEqual(server._safe_session_stem("///"), "anon")

    def test_get_session_isolation(self):
        alice = server.get_session("alice")
        bob = server.get_session("bob")
        self.assertIsNot(alice, bob)
        # 同一 id 返回同一个会话对象
        self.assertIs(server.get_session("alice"), alice)
        # 两个会话的喂饭状态互不影响
        alice.state.meal_active = True
        self.assertFalse(bob.state.meal_active)


class ResolveSessionTest(unittest.TestCase):
    def test_body_session_takes_priority(self):
        sid, set_cookie = server._resolve_session("mm_session=cookieval", {"session": "bodyval"})
        self.assertEqual(sid, "bodyval")
        self.assertFalse(set_cookie)

    def test_cookie_used_when_no_body_session(self):
        sid, set_cookie = server._resolve_session("mm_session=cookieval", {})
        self.assertEqual(sid, "cookieval")
        self.assertFalse(set_cookie)

    def test_auto_generate_when_absent(self):
        sid, set_cookie = server._resolve_session("", {})
        self.assertTrue(set_cookie)
        self.assertGreaterEqual(len(sid), 16)

    def test_malformed_cookie_falls_back_to_new(self):
        sid, set_cookie = server._resolve_session("=;=;bad", {})
        self.assertTrue(set_cookie)
        self.assertGreaterEqual(len(sid), 16)


class HealthPayloadTest(unittest.TestCase):
    def test_shape_and_no_secret_leak(self):
        payload = server._health_payload()
        self.assertTrue(payload["ok"])
        for key in ("intentModel", "chatModel", "prefModel", "apiKeyConfigured",
                    "sessions", "uptimeSeconds"):
            self.assertIn(key, payload)
        self.assertIsInstance(payload["apiKeyConfigured"], bool)
        # 不得出现任何疑似密钥字段名
        self.assertNotIn("apiKey", payload)
        self.assertNotIn("API_KEY", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
