"""pools.py 跨进程锁 + 原子写 config.json 的行为测试。

覆盖两次修复：
  1. remove_proxy_from_local_pool 剔除死代理后，config.json 的 proxy_pool 被
     正确重写（list 与多行 str 两种形态）。
  2. 并发写入下 config.json 始终是合法 JSON（原子替换，无半写）。
"""
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pools  # noqa: E402


class _TmpConfig:
    """把 pools._config_path 指到临时文件，用完还原。"""

    def __init__(self, tmp_path: Path):
        self._tmp = tmp_path
        self._orig = None

    def __enter__(self):
        self._orig = pools._config_path
        pools._config_path = lambda: self._tmp  # type: ignore[assignment]
        # _CrossProcConfigLock 锁路径基于 _config_path，在 __init__ 时求值，OK
        return self

    def __exit__(self, *exc):
        pools._config_path = self._orig  # type: ignore[assignment]
        return False


class RemoveProxyRewriteTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(__file__).resolve().parent / "_tmp_pools"
        self.tmpdir.mkdir(exist_ok=True)
        self.cfg = self.tmpdir / "config.json"
        # 重置内存池状态
        pools._proxy_list = []
        pools._proxy_idx = 0

    def tearDown(self):
        for p in self.tmpdir.glob("config.json*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            self.tmpdir.rmdir()
        except OSError:
            pass

    def test_removes_from_list_pool(self):
        dead = "http://1.2.3.4:8080"
        keep = "http://5.6.7.8:9090"
        self.cfg.write_text(
            json.dumps({"proxy_pool": [dead, keep]}, ensure_ascii=False),
            encoding="utf-8",
        )
        with _TmpConfig(self.cfg):
            pools._proxy_list = [dead, keep]
            removed = pools.remove_proxy_from_local_pool(dead)
        self.assertEqual(removed, 1)
        conf = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual(conf["proxy_pool"], [keep])

    def test_removes_from_multiline_str_pool(self):
        dead = "http://1.2.3.4:8080"
        keep = "http://5.6.7.8:9090"
        self.cfg.write_text(
            json.dumps({"proxy_pool": f"{dead}\n{keep}"}, ensure_ascii=False),
            encoding="utf-8",
        )
        with _TmpConfig(self.cfg):
            pools._proxy_list = [dead, keep]
            pools.remove_proxy_from_local_pool(dead)
        conf = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertNotIn("1.2.3.4", conf["proxy_pool"])
        self.assertIn("5.6.7.8", conf["proxy_pool"])

    def test_concurrent_writes_never_corrupt(self):
        """多线程并发剔除不同代理：config.json 每次读都应是合法 JSON。"""
        proxies = [f"http://10.0.0.{i}:8080" for i in range(1, 9)]
        self.cfg.write_text(
            json.dumps({"proxy_pool": list(proxies)}, ensure_ascii=False),
            encoding="utf-8",
        )
        errors: list = []

        def worker(target: str):
            try:
                with _TmpConfig(self.cfg):
                    pools._proxy_list = list(proxies)
                    pools.remove_proxy_from_local_pool(target)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(p,)) for p in proxies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"worker 异常: {errors}")
        # 最终文件必须是合法 JSON（原子替换保证无半写）
        conf = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertIn("proxy_pool", conf)


if __name__ == "__main__":
    unittest.main()
