"""
StockLab 测试共享装置 (tests/conftest.py)

【为什么需要这个文件】
  迁移到 pytest 前，13 个测试文件各自在函数里手写
  `tempfile.mkdtemp()` + `try/finally: shutil.rmtree()` 建临时 DuckDB，
  5 个文件各写一遍，改一处漏一处就得手动排查；Web 层那个应用实例则是
  模块级全局 + main() 里手动赋值。这些样板全部收进下面的 fixture。

【三条 fixture 分工】
  tmp_db_path  只给目录，文件自选（迁移测试要造「残缺旧库」，不能直接用新库）
  fresh_db     给目录 + 跑完全部迁移的可写 Database（新库契约类用例直接用）
  app / client 真实库的 Flask 应用与测试客户端（仅 test_web_api.py 用）

【真实库为什么必须先停 serve_web】
  DuckDB 对同一文件只允许一个写连接，而 serve_web.py 的 store 会长期持有
  data/stocklab.duckdb。这里不静默跳过，而是在 fixture 里主动探测并给出
  可操作的报错——否则使用者只会看到一句 `Could not set lock`。

【xdist 并行跑 real_db 测试】
  每个 worker 复制一份真实库到临时文件，避免「同文件多写连接」冲突。
  只在首次创建 app 时复制，后续复用；tmp_path 自动清理。
"""

import os
import shutil
import socket
import subprocess
import sys

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


# ---------------------------------------------------------------------------
# 临时库
# ---------------------------------------------------------------------------
@pytest.fixture
def tmp_db_path(tmp_path):
    """临时 DuckDB 的存放目录（目录本身已由 tmp_path 自动清理）

    Returns:
        str: 目录绝对路径
    """
    return str(tmp_path)


@pytest.fixture
def fresh_db(tmp_db_path):
    """一个已跑完全部迁移的空库句柄

    Returns:
        tuple: (db_path, Database)
    """
    from stocklab.persistence import Database
    from stocklab.persistence.storage import initialize_database

    db_path = os.path.join(tmp_db_path, "fresh.duckdb")
    initialize_database(db_path)
    return db_path, Database(db_path)


# ---------------------------------------------------------------------------
# 真实库（仅 Web 层用）
# ---------------------------------------------------------------------------
def _ancestor_pids():
    """当前进程及其所有祖先的 pid 集合

    【为什么必须排除祖先】
    `pgrep -f serve_web.py` 匹配的是**命令行里出现过这个字符串**的任何进程，
    不只是真的在跑服务的那一个。典型误报：开发者（和 CI）习惯先执行
    `pkill -f serve_web.py` 再跑测试，而执行这条命令的 shell 自身的
    cmdline 里就带着 "serve_web.py"，于是在整个测试期间都能被 pgrep 命中 ——
    fixture 立刻误报「serve_web.py 正在运行」，而实际上根本没人在跑服务。
    """
    pids, current = set(), os.getpid()
    for _ in range(16):                      # 16 层足够覆盖 macOS / Linux 的进程树
        if current <= 1 or current in pids:
            break
        pids.add(current)
        try:
            output = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(current)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break
        if not output:
            break
        current = int(output.split()[0])
    return pids


def _serve_web_running():
    """检测 serve_web.py 是否真的在运行（它会独占本地 DuckDB 的写连接）"""
    try:
        output = subprocess.run(
            ["pgrep", "-f", "serve_web.py"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    if not output:
        return False
    mine = _ancestor_pids()
    for pid in output.split():
        if int(pid) in mine:
            continue
        # 还要确认它确实是个 python 进程在执行脚本，而不是恰好提到这个路径的
        # 编辑器 / 语言服务器 / grep 自身
        try:
            command = subprocess.run(
                ["ps", "-o", "comm=", "-p", pid],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip().lower()
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if "python" in command or command.endswith("serve_web"):
            return True
    return False


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    """真实库上的 Flask 应用（会话级，只装配一次）

    【xdist 并行支持】每个 worker 复制一份真实库到临时文件，
    避免「同文件多写连接」冲突。复制发生在首次创建 app 时，
    后续复用；tmp_path_factory 自动清理。

    Yields:
        Flask: create_app() 的结果
    """
    if _serve_web_running():
        pytest.fail(
            "serve_web.py 正在运行，它已独占 data/stocklab.duckdb 的写连接，"
            "Web 层用例无法打开该库。请先执行：pkill -f serve_web.py",
            pytrace=False,
        )

    # 复制真实库到临时文件（每个 xdist worker 独立文件）
    real_db = "data/stocklab.duckdb"
    if not os.path.exists(real_db):
        pytest.skip(f"真实库不存在: {real_db}，跳过 real_db 测试", allow_module_level=True)

    # 使用 tmp_path_factory 创建会话级临时目录（xdist 下每个 worker 独立）
    temp_dir = tmp_path_factory.mktemp("real_db")
    temp_db = temp_dir / "stocklab.duckdb"
    shutil.copy2(real_db, temp_db)

    from app.web import create_app
    from app.web import store

    # 显式传入临时库路径，避免读取默认配置
    application = create_app(db_path=str(temp_db))

    # 重置 store 的单例连接，强制指向新的临时库
    with application.app_context():
        store.reset_facade()
        store._store_conn = None  # type: ignore

    # store 的只读聚合依赖 current_app，必须整段跑在应用上下文里；
    # 这里只负责建 app，上下文由 client fixture 推入
    yield application

    # 清理环境变量（如果有）
    os.environ.pop("STOCKLAB_DB_PATH", None)


@pytest.fixture
def ctx(app):
    """把测试推进应用上下文（store 的 current_app 依赖）"""
    with app.app_context():
        yield app


@pytest.fixture
def client(ctx):
    """Flask 测试客户端（依赖 ctx，自动带上应用上下文）

    之前是模块级 _APP 全局 + main() 里手动 create_app()，失败信息里看不出
    用例之间的依赖；改成 fixture 后依赖关系显式化。
    """
    return ctx.test_client()


# ---------------------------------------------------------------------------
# 网络探针的公共设施
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def http_reachable():
    """探测外网是否可达，不可达则跳过 integration 用例并给出原因

    单列一个 fixture 而不是直接联网：本地断网/代理挂掉时，
    整个 integration 集合被 skip，而不是逐个用例超时几十秒。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(4)
    try:
        sock.connect(("www.baidu.com", 443))
        return True
    except OSError:
        return False
    finally:
        sock.close()
