"""Verify a backup into a NEW directory. Never loads deployment configuration or switches live data."""
import argparse
from pathlib import Path
import sys
from zipfile import BadZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from app.core.recovery_bundle import prepare_restore


def main():
    parser = argparse.ArgumentParser(description='在全新独立目录核验数据库与本地诊断媒体恢复，不覆盖原数据')
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--target', required=True, type=Path, help='父目录必须存在，目标目录必须不存在')
    arguments = parser.parse_args()
    try:
        result = prepare_restore(arguments.bundle, arguments.target)
    except (OSError, ValueError, BadZipFile) as error:
        parser.exit(1, '恢复未通过核验；不得切换此目录。原因：' + str(error) + '\n')
    print(f"独立副本核验通过：schema={result['schema_revision']}，文件数={len(result['files'])}。")
    print('READY.json 已生成。停服和核对原加密密钥后，由管理员单独决定数据目录切换。')


if __name__ == '__main__':
    main()
