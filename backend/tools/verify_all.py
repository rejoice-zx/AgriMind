# -*- coding: utf-8 -*-
"""
AgriMind 全量可复现验收基线脚本 (verify_all.py)
依次执行：
1. 当前源码凭证模式扫描（包含未跟踪文件，不依赖 Git）
2. 本地依赖导入（不读取部署配置）
3. 后端隔离全量测试
4. 前端行为测试、类型检查与隔离生产构建
输出结果至控制台，并在 scratch/ 下新建独立验证目录保存基线快照。
任何子项失败均返回非 0 退出码，坚决杜绝假绿灯。
"""

import os
import sys
import time
import subprocess
import re
import uuid
import shutil
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 确保在 Windows 控制台输出 Unicode 字符不报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parents[2]
PYTHON_EXE = os.getenv("AGRIMIND_PYTHON", sys.executable)
NODE_CMD = os.getenv("AGRIMIND_NODE") or shutil.which("node") or "node"

ANSI_REGEX = re.compile(r'\x1b\[[0-9;]*m')

def strip_ansi(text: str) -> str:
    return ANSI_REGEX.sub('', text)

def run_command(cmd, cwd=BASE_DIR, shell=False, extra_env=None):
    start = time.time()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra_env or {})
    res = subprocess.run(
        cmd,
        cwd=cwd,
        shell=shell,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace"
    )
    elapsed = time.time() - start
    return res.returncode, strip_ansi(res.stdout), elapsed


def git_metadata(option):
    """Git identity is optional when verifying an extracted source archive."""
    try:
        code, output, _ = run_command(["git", "rev-parse", option, "HEAD"])
    except OSError:
        return "unknown"
    return output.strip() if code == 0 else "unknown"


def main() -> bool:
    print("=" * 60)
    print("  AgriMind 一键全量系统验证与基线快照开始运行")
    print("=" * 60)
    
    total_start = time.time()
    work_dir = BASE_DIR / "scratch" / ("verification-" + str(uuid.uuid4()))
    work_dir.mkdir(parents=True, exist_ok=False)
    print("VERIFY_OUTPUT=" + str(work_dir), flush=True)
    
    # 获取 git 信息
    git_commit = git_metadata("--short")
    git_branch = git_metadata("--abbrev-ref")
    
    # 1. 密钥安全扫描
    print("\n[1/6] 执行源码敏感密钥扫描...", flush=True)
    code, scan_out, _ = run_command([PYTHON_EXE,
        str(BASE_DIR / "backend/tools/export_github_source.py"), "--check"])
    key_hit_count = 0
    checked_text_files = 0
    key_scan_pass = False
    try:
        scan = json.loads(scan_out)
        if isinstance(scan, dict):
            key_hit_count = scan.get("matched_files", 0)
            checked_text_files = scan.get("checked_text_files", 0)
            key_scan_pass = (code == 0 and scan.get("status") == "pass"
                and type(key_hit_count) is int and key_hit_count == 0
                and type(checked_text_files) is int and checked_text_files > 0)
    except (ValueError, TypeError):
        pass
    if key_scan_pass:
        print(f"  [PASS] 已检查{checked_text_files}个当前源码/说明文本，未命中所检凭证模式。")
    else:
        print(f"  [FAIL] 可疑凭证文件数: {key_hit_count}，扫描退出码: {code}")

    # Test preparation must not open a developer's deployment configuration or business DB.
    print("\n[2/6] 检查本地Python依赖（不加载部署配置）...", flush=True)
    code, config_out, _ = run_command([PYTHON_EXE, "-c", "import fastapi, sqlalchemy, pydantic, pytest; print('Python dependencies available')"])
    config_pass = (code == 0)
    print(f"  [{'PASS' if config_pass else 'FAIL'}] 本地依赖导入检查完毕。")
    
    # 3. 后端单元与集成测试 (pytest)
    print("\n[3/6] 运行后端隔离全量测试...", flush=True)
    code, pytest_out, pytest_time = run_command(
        [PYTHON_EXE, str(BASE_DIR / "backend" / "tools" / "run_agri_checks.py"), "tests", "--show-capture=no",
         "--junitxml=" + str(work_dir / "backend-tests.xml")],
        extra_env={"AGRI_CHECK_SCRATCH": str(work_dir)}
    )
    (work_dir / "backend-tests.log").write_text(pytest_out, encoding="utf-8")
    pytest_pass = (code == 0)
    
    # 正则提取测试摘要，如 232 passed in 108.26s
    pytest_summary = "未知"
    pytest_match = re.search(r"(\d+\s+passed[^\r\n]*)", pytest_out)
    if pytest_match:
        pytest_summary = pytest_match.group(1).strip()
    else:
        for line in pytest_out.splitlines()[::-1]:
            if any(k in line for k in ("passed", "failed", "error")):
                pytest_summary = line.strip()
                break
    
    if pytest_summary == "未知" or not pytest_pass:
        pytest_pass = False
        print(f"  [FAIL] 测试结果: {pytest_summary} (耗时 {pytest_time:.1f}s)")
    else:
        print(f"  [PASS] 测试结果: {pytest_summary} (耗时 {pytest_time:.1f}s)")

    print("\n[4/6] 运行前端完整行为测试...", flush=True)
    code, frontend_out, _ = run_command([NODE_CMD, "--test", "--test-reporter=tap"], cwd=BASE_DIR / "frontend")
    frontend_test_pass = code == 0
    (work_dir / "frontend-tests.tap").write_text(frontend_out, encoding="utf-8")
    frontend_summary = "; ".join(line for line in frontend_out.splitlines() if re.match(r"# (tests|pass|fail|skipped) ", line))
    print(f"  [{'PASS' if frontend_test_pass else 'FAIL'}] {frontend_summary}", flush=True)

    print("\n[5/6] 运行前端类型检查...", flush=True)
    code, typecheck_out, _ = run_command([NODE_CMD, str(BASE_DIR / "frontend/node_modules/vue-tsc/bin/vue-tsc.js"),
        "--noEmit", "-p", "tsconfig.json"], cwd=BASE_DIR / "frontend")
    typecheck_pass = code == 0
    (work_dir / "typecheck.log").write_text(typecheck_out, encoding="utf-8")
    print(f"  [{'PASS' if typecheck_pass else 'FAIL'}] 前端类型检查", flush=True)

    print("\n[6/6] 运行隔离生产构建（不读取部署配置、不删除已有输出）...", flush=True)
    code, build_out, build_time = run_command([NODE_CMD, str(BASE_DIR / "frontend/tools/build_agri_frontend.mjs")],
        extra_env={"AGRI_BUILD_SCRATCH": str(work_dir)})
    (work_dir / "build.log").write_text(build_out, encoding="utf-8")
    build_pass = (code == 0)
    build_summary = "构建完成"
    for line in build_out.splitlines()[::-1]:
        if "built in" in line or "✓ built" in line:
            build_summary = line.strip()
            break
    print(f"  [{'PASS' if build_pass else 'FAIL'}] 构建结果: {build_summary} (耗时 {build_time:.1f}s)")

    # 核心门禁 (Gates: P0/P1 级工程硬标准)：任何一项失败即中断退出
    hard_gates_pass = (
        key_scan_pass
        and config_pass
        and pytest_pass
        and frontend_test_pass
        and typecheck_pass
        and build_pass
    )
    all_ok = hard_gates_pass

    total_duration = round(time.time() - total_start, 1)

    # 生成 Markdown 快照
    snapshot_lines = [
        "# AgriMind 系统实测基线快照 (Baseline Snapshot)",
        "",
        "> **本快照由 verify_all.py 在本地宿主执行生成；核心工程门禁失败将导致退出码非 0**",
        "",
        f"- **生成时间**: {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"- **Git 提交版本**: `{git_commit}` (分支: `{git_branch}`)",
        f"- **工程门禁状态**: {'✅ ALL GATES PASS' if hard_gates_pass else '❌ GATES FAILED'}",
        f"- **总耗时**: {total_duration} 秒",
        "",
        "## 一、核心工程门禁指标 (Quality Gates)",
        "",
        "| 门禁项 | 门禁标准 | 实测数值 / 状态 | 判定 |",
        "|:---|:---|:---:|:---:|",
        f"| **源码敏感密钥扫描** | 0 个文件命中且扫描成功 | {key_hit_count} 个文件匹配 | {'✅ PASS' if key_scan_pass else '❌ FAIL'} |",
        f"| **本地Python依赖导入** | 不读取部署配置 | {'成功' if config_pass else '失败'} | {'✅ PASS' if config_pass else '❌ FAIL'} |",
        f"| **后端全量测试套件** | 全部通过 | {pytest_summary} | {'✅ PASS' if pytest_pass else '❌ FAIL'} |",
        f"| **前端行为测试** | 全部通过 | {frontend_summary} | {'✅ PASS' if frontend_test_pass else '❌ FAIL'} |",
        f"| **前端类型检查** | 0 报错 | {'成功' if typecheck_pass else '失败'} | {'✅ PASS' if typecheck_pass else '❌ FAIL'} |",
        f"| **Vite生产构建** | 独立输出且不加载部署配置 | {build_summary} | {'✅ PASS' if build_pass else '❌ FAIL'} |",
        "",
        "## 二、依赖导入摘要（未检查真实运行配置或外部服务）",
        "",
        "```text",
        config_out.strip(),
        "```",
        "",
        "## 三、后端测试原始摘要",
        "",
        "```text",
        "\n".join(pytest_out.strip().splitlines()[-12:]),
        "```",
        "",
        "## 四、前端构建原始摘要",
        "",
        "```text",
        "\n".join(build_out.strip().splitlines()[-12:]),
        "```",
        ""
    ]
    
    snapshot_path = work_dir / "baseline_snapshot.md"
    with open(snapshot_path, "x", encoding="utf-8") as f:
        f.write("\n".join(snapshot_lines))
        
    print(f"\n[{'OK' if all_ok else 'FAIL'}] 基线快照已成功写入: {snapshot_path}")
    print(f"全量验收状态: {'✅ 全部通过' if all_ok else '❌ 存在失败项'}")
    print("=" * 60)
    return all_ok

if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
