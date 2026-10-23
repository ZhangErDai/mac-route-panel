"""Single-operation administrator helper; no web server runs as root."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from routing import execute


def main(request_path=None):
    try:
        if sys.platform != 'darwin' or os.geteuid() != 0:
            raise RuntimeError('路由修改必须通过 macOS 管理员授权执行。')
        path = Path(request_path if request_path is not None else sys.argv[1])
        if path.is_symlink() or path.stat().st_size > 65536:
            raise RuntimeError('操作请求文件不合法。')
        result = execute(json.loads(path.read_text()))
    except Exception as e:
        result = dict(ok=False, message=str(e))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
