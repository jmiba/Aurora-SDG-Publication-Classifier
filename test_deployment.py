"""Deployment cache isolation and persistence without touching the user's cache."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DeploymentCacheTests(unittest.TestCase):
    # @lat: [[testing#Deployment cache persistence]]
    def test_custom_cache_survives_a_new_process(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "data" / "cache.sqlite3"
            env = {**os.environ, "SDG_CACHE_PATH": str(cache)}
            subprocess.run(
                [sys.executable, "-c", "import cache_db; "
                 "cache_db.upsert_work({'publication_key': 'deployment-test', "
                 "'title': 'Persistent publication'}); cache_db.close_connection()"],
                env=env, check=True, capture_output=True,
            )
            self.assertTrue(cache.is_file())
            subprocess.run(
                [sys.executable, "-c", "import cache_db; "
                 "assert cache_db.get_cached_publication('deployment-test')['title'] "
                 "== 'Persistent publication'; cache_db.close_connection()"],
                env=env, check=True, capture_output=True,
            )


if __name__ == "__main__":
    unittest.main()
