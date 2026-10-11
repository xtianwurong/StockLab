#!/usr/bin/env bash
# StockLab 统一入口 (Linux/macOS)
# 用法: ./run.sh [install|run|test|sync]

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/venv/bin/python"
PIP="$ROOT/venv/bin/pip"

ensure_venv() {
    [[ -x "$PY" ]] || { echo "[INFO] 创建虚拟环境..."; python3 -m venv "$ROOT/venv"; "$PIP" install -U pip; }
}

case "${1:-help}" in
    install)
        ensure_venv
        echo "[INFO] 安装依赖..."
        "$PIP" install -r "$ROOT/requirements.txt"
        echo "[OK] 完成"
        ;;
    run)
        ensure_venv
        "$PIP" install -q -r "$ROOT/requirements.txt"
        echo "[INFO] 启动 Web: http://127.0.0.1:${PORT:-8000}"
        cd "$ROOT" && exec "$PY" app/scripts/serve_web.py --port "${PORT:-8000}"
        ;;
    test)
        ensure_venv
        "$PIP" install -q -r "$ROOT/requirements.txt"
        echo "[INFO] 运行单元测试..."
        cd "$ROOT" && "$PY" -m pytest -m "not integration" --tb=short
        ;;
    sync)
        ensure_venv
        "$PIP" install -q -r "$ROOT/requirements.txt"
        echo "[INFO] 全量同步核心数据..."
        cd "$ROOT" && "$PY" app/scripts/sync_all.py
        ;;
    *)
        echo "用法: ./run.sh [install|run|test|sync]"
        echo "  install  安装依赖"
        echo "  run      启动 Web 服务 (含安装)"
        echo "  test     运行单元测试"
        echo "  sync     全量同步核心数据"
        ;;
esac