# Use pysqlite3 for modern SQLite features (3.24+ upsert, 3.23+ TRUE/FALSE).
# Must patch sys.modules BEFORE any submodule imports aiosqlite, which
# caches its own reference to sqlite3 at import time.
import sys

import pysqlite3

sys.modules["sqlite3"] = pysqlite3
