"""建表脚本：创建数据库表并校验连通性。

用法：
    python scripts/init_db.py            # 建表并输出结果
    python scripts/init_db.py --check    # 只做连通性检查
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import check_database, get_engine, init_db, table_names  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化数据库表")
    parser.add_argument("--check", action="store_true", help="仅检查数据库连通性")
    args = parser.parse_args()

    print(f"数据库：{settings.safe_database_url()}")
    ok, detail = check_database()
    if not ok:
        print(f"[失败] {detail}")
        print("请确认 MySQL 已启动，且 .env 中的 DATABASE_URL 可连接。")
        return 1
    print(f"[通过] {detail}")
    if args.check:
        return 0

    init_db()
    existing = set(inspect(get_engine()).get_table_names())
    print(f"模型定义表：{', '.join(table_names())}")
    for name in table_names():
        print(f"  - {name}: {'已存在' if name in existing else '缺失'}")
    missing = [name for name in table_names() if name not in existing]
    if missing:
        print(f"[失败] 仍有表未创建：{missing}")
        return 1
    print("[通过] 数据表创建完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
