"""Numbered schema upgrade on a verified restore copy, without deployment settings."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from app.core.copy_upgrade import upgrade_restore_copy
from sqlalchemy.exc import SQLAlchemyError


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='对含READY.json的独立恢复副本执行编号升级；不切换运行数据')
    parser.add_argument('--copy', required=True, type=Path)
    arguments = parser.parse_args()
    try:
        result = upgrade_restore_copy(arguments.copy)
    except (OSError, ValueError, KeyError, SQLAlchemyError) as error:
        # Do not echo SQL parameters or business rows from driver diagnostics.
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        parser.exit(1, '独立副本升级未通过，不得切换。原因：' + detail + '\n')
    print(f"独立副本结构升级通过：schema={result['schema_revision']}，编号步骤={result['steps']}。")
    print('UPGRADED.json 已生成；仍须停服、核对原配置和主要功能后，由管理员决定切换。')


if __name__ == '__main__':
    main()
