#!/usr/bin/env python3
"""
==============================================================================
StockLab - CSS 死规则清理工具 (tools/clean_css.py)
==============================================================================

【功能用途】
   扫描 templates/ 与 static/ 中实际使用的 CSS 类名，
   对比 components.css 规则，输出未被引用的选择器供人工确认删除。

【为什么不用自动删除】
   1. CSS 选择器可能通过 JS 动态拼接（如 `el.classList.add('metric-card' + suffix)`）
   2. 伪类/伪元素（:hover, :focus, ::before）静态扫描不到
   3. 媒体查询内的选择器在特定断点才生效
   4. 宁可保留冗余，不可误删生效规则

【使用方式】
   python tools/clean_css.py                    # 扫描并报告
   python tools/clean_css.py --apply            # 交互式确认后删除（谨慎使用）
   python tools/clean_css.py --output report.txt # 导出报告
"""

import argparse
import os
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

# ---------- 配置 ----------
COMPONENTS_CSS = BASE_DIR / "app" / "web" / "static" / "components.css"
TEMPLATES_DIR = BASE_DIR / "app" / "web" / "templates"
STATIC_JS_DIR = BASE_DIR / "app" / "web" / "static"

# 明确保留的选择器模式（即使扫描不到也保留）
KEEP_PATTERNS = [
    r"@media", r"@keyframes", r"@supports",
    r"\.hidden\b", r"\.visible\b", r"\.sr-only\b",  # 工具类常通过 JS 切换
    r"\.toast-", r"\.modal-", r"\.drawer-",  # UI 组件状态类
    r"\.chart-", r"\.echarts-",   # 图表相关
    # 注意：:hover/:focus/:active 等伪类不在此列，交给类名匹配判断
]

# ---------- 扫描器 ----------

def extract_classes_from_html(text):
    """从 HTML/模板提取 class="" 内的类名"""
    classes = set()
    # class="a b c" 或 class='a b c'
    for match in re.finditer(r'class\s*=\s*["\']([^"\']+)["\']', text):
        for cls in match.group(1).split():
            classes.add(cls)
    return classes


def extract_classes_from_js(text):
    """从 JS 提取可能的类名（字面量字符串）"""
    classes = set()
    # classList.add/remove/toggle/contains('classname')
    for match in re.finditer(r"classList\.(?:add|remove|toggle|contains)\s*\(\s*['\"]([^'\"]+)['\"]", text):
        classes.add(match.group(1))
    # element.className = 'classname'
    for match in re.finditer(r"\.className\s*=\s*['\"]([^'\"]+)['\"]", text):
        for cls in match.group(1).split():
            classes.add(cls)
    # SL.el('tag', 'classname', ...)
    for match in re.finditer(r"SL\.el\s*\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]", text):
        for cls in match.group(1).split():
            classes.add(cls)
    # innerHTML 赋值中的 class="..."
    for match in re.finditer(r"innerHTML\s*[+=]\s*['\"][^'\"]*class\s*=\s*['\"]([^'\"]+)['\"]", text):
        for cls in match.group(1).split():
            classes.add(cls)
    return classes


def parse_css_selectors(css_text):
    """解析 CSS 文件，提取每条规则的选择器"""
    # 简单但实用：按 } 分割规则，保留选择器部分
    rules = []
    # 先移除注释
    css = re.sub(r"/\*.*?\*/", "", css_text, flags=re.DOTALL)
    # 按 } 分割
    for block in css.split("}"):
        block = block.strip()
        if not block:
            continue
        # 选择器在 { 之前
        if "{" in block:
            selector_part = block.split("{")[0].strip()
            if selector_part:
                rules.append(selector_part)
    return rules


def selector_uses_class(selector, class_name):
    """判断选择器是否引用了某个类名（支持组合选择器）"""
    # 简单匹配：.classname 作为独立 token 出现
    # 注意：不处理复杂的组合选择器语义，只做字面匹配
    pattern = r"(^|[^a-zA-Z0-9_-])" + re.escape(class_name) + r"($|[^a-zA-Z0-9_-])"
    return bool(re.search(pattern, selector))


def should_keep_selector(selector):
    """判断选择器是否命中保留模式"""
    for pattern in KEEP_PATTERNS:
        if re.search(pattern, selector):
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description="CSS 死规则扫描器")
    parser.add_argument("--apply", action="store_true", help="交互式确认后删除（谨慎）")
    parser.add_argument("--output", type=str, help="导出报告到文件")
    parser.add_argument("--min-confidence", type=float, default=0.8, help="最小置信度阈值")
    args = parser.parse_args()

    # 1. 收集所有使用到的类名
    used_classes = set()

    # 1.1 模板文件
    for tpl in TEMPLATES_DIR.rglob("*.html"):
        used_classes.update(extract_classes_from_html(tpl.read_text(encoding="utf-8")))

    # 1.2 JS 文件
    for js in STATIC_JS_DIR.glob("*.js"):
        used_classes.update(extract_classes_from_js(js.read_text(encoding="utf-8")))

    print(f"[扫描] 模板 + JS 共发现类名: {len(used_classes)} 个")

    # 2. 解析 components.css
    css_text = COMPONENTS_CSS.read_text(encoding="utf-8")
    selectors = parse_css_selectors(css_text)
    print(f"[解析] components.css 共 {len(selectors)} 条规则")

    # 3. 逐规则判断
    unused = []
    used_count = 0
    kept_by_pattern = 0

    for selector in selectors:
        # 先检查保留模式
        if should_keep_selector(selector):
            kept_by_pattern += 1
            continue

        # 检查是否引用了任何已知类名
        referenced = False
        for cls in used_classes:
            if selector_uses_class(selector, cls):
                referenced = True
                break

        if referenced:
            used_count += 1
        else:
            # 计算置信度：选择器长度、复杂度越高，误判概率越低
            confidence = min(1.0, len(selector) / 50.0 + 0.3)
            if confidence >= args.min_confidence:
                unused.append((selector, confidence))

    print(f"[结果] 明确引用: {used_count} 条")
    print(f"[结果] 模式保留: {kept_by_pattern} 条")
    print(f"[结果] 疑似死规则: {len(unused)} 条 (置信度 >= {args.min_confidence})")

    if not unused:
        print("\n✅ 未发现疑似死规则")
        return 0

    # 4. 输出报告
    report_lines = [
        "# CSS 死规则扫描报告",
        f"生成时间: {__import__('datetime').datetime.now().isoformat()}",
        f"扫描模板: {len(list(TEMPLATES_DIR.rglob('*.html')))} 个",
        f"扫描 JS: {len(list(STATIC_JS_DIR.glob('*.js')))} 个",
        f"CSS 规则总数: {len(selectors)}",
        f"明确引用: {used_count}",
        f"模式保留: {kept_by_pattern}",
        f"疑似死规则: {len(unused)}",
        "",
        "## 疑似死规则列表 (按置信度降序)",
        "",
    ]

    for selector, conf in sorted(unused, key=lambda x: -x[1]):
        report_lines.append(f"- **置信度 {conf:.0%}**: `{selector}`")

    report = "\n".join(report_lines)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"\n📄 报告已写入: {args.output}")
    else:
        print("\n" + report)

    # 5. 交互式删除（谨慎）
    if args.apply:
        print("\n⚠️  即将进入交互式删除模式，请逐条确认！")
        print("输入 'y' 删除，'n' 保留，'q' 退出")
        new_css = css_text
        deleted = 0
        for selector, conf in sorted(unused, key=lambda x: -x[1]):
            print(f"\n--- 置信度 {conf:.0%} ---")
            print(selector)
            choice = input("删除? [y/N/q]: ").strip().lower()
            if choice == 'q':
                break
            if choice == 'y':
                # 从 CSS 中移除整条规则（简单但有效的方法）
                # 找到选择器对应的完整规则块
                pattern = re.escape(selector) + r"\s*\{[^}]*\}"
                new_css = re.sub(pattern, "", new_css, flags=re.DOTALL)
                deleted += 1
                print("  ✅ 已标记删除")
            else:
                print("  ⏭️  保留")

        if deleted > 0:
            # 清理多余空行
            new_css = re.sub(r"\n{3,}", "\n\n", new_css)
            # 备份
            backup = COMPONENTS_CSS.with_suffix(".css.bak")
            COMPONENTS_CSS.rename(backup)
            COMPONENTS_CSS.write_text(new_css, encoding="utf-8")
            print(f"\n✅ 已删除 {deleted} 条规则，原文件备份为 {backup}")
        else:
            print("\n未删除任何规则")

    return 0


if __name__ == "__main__":
    sys.exit(main())