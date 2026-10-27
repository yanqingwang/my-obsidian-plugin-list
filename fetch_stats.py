#!/usr/bin/env python3
"""抓取 rosswang 已发布 Obsidian 插件的下载量，生成趋势图与插件清单。

数据源：
- community-plugins.json       : 已发布插件目录（作者、仓库、简介）
- community-plugin-stats.json  : 每个插件的累计下载量

用法：
    python fetch_stats.py
    python fetch_stats.py --catalog-file /tmp/community-plugins.json \
                          --stats-file /tmp/community-plugin-stats.json

脚本只依赖 Python 标准库，适合放在 GitHub Actions 里每天跑。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

AUTHOR = "rosswang"
AUTHOR_REPO = "yanqingwang"
TIMEZONE = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent

CATALOG = "community-plugins.json"
STATS = "community-plugin-stats.json"
CATALOG_URLS = [
    f"https://api.github.com/repos/obsidianmd/obsidian-releases/contents/{CATALOG}",
    f"https://raw.githubusercontent.com/obsidianmd/obsidian-releases/master/{CATALOG}",
    f"https://cdn.jsdelivr.net/gh/obsidianmd/obsidian-releases@master/{CATALOG}",
]
STATS_URLS = [
    f"https://api.github.com/repos/obsidianmd/obsidian-releases/contents/{STATS}",
    f"https://raw.githubusercontent.com/obsidianmd/obsidian-releases/master/{STATS}",
    f"https://cdn.jsdelivr.net/gh/obsidianmd/obsidian-releases@master/{STATS}",
]
MARKET_URL = "https://community.obsidian.md/plugins/{plugin_id}"
USER_AGENT = "my-obsidian-plugin-list/1.0 (+https://github.com/yanqingwang/my-obsidian-plugin-list)"

# 中文一句话说明；新插件会自动回退到市场里的官方简介。
DESCRIPTIONS = {
    "html-effectiveness": "在 Obsidian 中直接渲染对比、流程图、幻灯片、报告、时间线等空间化 HTML 内容。",
    "md-to-html-effect": "把 Markdown 渲染成可缩放、可导出的空间化 HTML（对比、时间线、SVG 图、报告、幻灯片）。",
    "html-to-md-effect": "把 HTML 文件转换成干净的 Markdown，保留标题、列表、代码块、表格、链接和图片。",
    "joplin-server-sync": "在 Obsidian vault 与 Joplin Server 之间双向同步，支持附件、标签和链接。",
    "vault-force-sync": "把 vault 强制/增量同步到可配置的 GitHub 远程仓库，跳过超大文件并记录同步日志。",
    "nextcloud-sync-yanc": "在 Obsidian vault 与 Nextcloud 之间双向同步，支持智能/强制模式、分块上传和隐藏文件排除。",
    "quadrant-chart": "用纯 YAML 的 .mdx 文件绘制带自由文本的 N×M 象限图。",
    "embed-link-converter": "在 Obsidian wiki-link 嵌入与 Markdown 图片语法之间双向转换，支持文件夹范围、预览和自动备份。",
    "clear-unused-attachments": "按文件夹和文件类型筛选并清理未使用的图片和附件。",
}

REVIEW_SUFFIX = re.compile(
    r"\s*-\s*This plugin has (?:not )?been manually reviewed by Obsidian staff\.?\s*$",
    re.IGNORECASE,
)

# 市场页面“Downloads”区块里的数字：小数字精确展示（如 512），
# 达到千级会缩写为 “1k”/“2.3k”/“1.2m”，需要用正则抓取并还原。
MARKET_DOWNLOADS_RE = re.compile(
    r'text-muted">Downloads</div><div class="">([^<]+)</div>'
)


def parse_market_downloads(raw: str) -> int:
    """把市场页面的下载量字符串（可能带 k/m 缩写与千分位逗号）转成整数。"""
    text = (raw or "").strip().replace(",", "")
    match = re.match(r"^([\d.]+)\s*([kKmM]?)$", text)
    if not match:
        return 0
    number = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    return int(round(number))


def fetch_market_downloads(plugin_id: str) -> int:
    """从官方市场页面抓取下载量，作为 community-plugin-stats.json 缺失时的兜底。"""
    url = MARKET_URL.format(plugin_id=plugin_id)
    try:
        headers = {"User-Agent": USER_AGENT}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=45) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        match = MARKET_DOWNLOADS_RE.search(html)
        if match:
            return parse_market_downloads(match.group(1))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 市场页面抓取失败 {url}: {exc}", file=sys.stderr, flush=True)
    return 0


def norm_author(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value or "").lower()


def fetch_json(urls: list[str], token: str = "") -> dict:
    """按顺序尝试多个数据源；带指数退避。"""
    errors = []
    for url in urls:
        for attempt in range(2):
            try:
                headers = {"User-Agent": USER_AGENT}
                if "api.github.com" in url:
                    headers["Accept"] = "application/vnd.github.raw"
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                else:
                    headers["Accept"] = "application/json"
                print(f"[fetch] {url} (attempt {attempt + 1})", file=sys.stderr, flush=True)
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=45) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{url} (attempt {attempt + 1}): {exc}")
                if attempt == 0:
                    time.sleep(2)
    raise RuntimeError("所有数据源都失败：\n" + "\n".join(errors))


def load_json_file(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_payload(name: str, override: str, urls: list[str], token: str) -> dict:
    if override:
        return load_json_file(override)
    if name == CATALOG:
        return fetch_json(urls, token)
    return fetch_json(urls, token)


def clean_description(text: str) -> str:
    text = REVIEW_SUFFIX.sub("", text or "").strip()
    return re.sub(r"\s+", " ", text)


def collect_plugins(catalog: list[dict], stats: dict) -> list[dict]:
    rows = []
    for item in catalog:
        if norm_author(item.get("author", "")) != AUTHOR:
            continue
        plugin_id = item.get("id", "").strip()
        if not plugin_id:
            continue
        entry = stats.get(plugin_id) or {}
        downloads = int(entry.get("downloads") or 0)
        # community-plugin-stats.json 滞后：刚上架/新收录的插件（如 vault-agent）
        # 可能还没写入统计文件，而官方市场页已显示真实下载量。这里用市场页
        # 抓取作为兜底，避免把下载量错误地显示成 0。
        if downloads == 0:
            market_downloads = fetch_market_downloads(plugin_id)
            if market_downloads > 0:
                downloads = market_downloads
        repo = (item.get("repo") or "").strip()
        description = DESCRIPTIONS.get(plugin_id) or clean_description(
            item.get("description", "")
        )
        rows.append(
            {
                "id": plugin_id,
                "name": item.get("name") or plugin_id,
                "author": item.get("author") or "",
                "repo": repo,
                "downloads": downloads,
                "description": description,
                "market_url": MARKET_URL.format(plugin_id=plugin_id),
                "github_url": f"https://github.com/{repo}" if repo else "",
            }
        )
    rows.sort(key=lambda r: (-r["downloads"], r["name"].lower()))
    return rows


def load_history(path: Path) -> dict:
    if not path.exists():
        return {"author": AUTHOR, "snapshots": []}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    data.setdefault("snapshots", [])
    return data


def upsert_snapshot(history: dict, rows: list[dict], day: str) -> dict:
    total = sum(r["downloads"] for r in rows)
    snapshot = {
        "date": day,
        "total": total,
        "plugins": {r["id"]: r["downloads"] for r in rows},
    }
    snapshots = [s for s in history.get("snapshots", []) if s.get("date") != day]
    snapshots.append(snapshot)
    snapshots.sort(key=lambda s: s["date"])
    history["author"] = AUTHOR
    history["snapshots"] = snapshots
    history["updated"] = datetime.now(TIMEZONE).isoformat(timespec="seconds")
    return snapshot


def nice_ticks(max_value: int, count: int = 5) -> list[int]:
    if max_value <= 0:
        return [0, 1]
    raw = max_value / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = magnitude
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * magnitude
        if step >= raw:
            break
    top = math.ceil(max_value / step) * step
    return [int(i * step) for i in range(int(top / step) + 1)]


def render_chart(history: dict, rows: list[dict], out_path: Path) -> None:
    snapshots = history.get("snapshots", [])
    points = [(s.get("date", ""), int(s.get("total", 0))) for s in snapshots]
    total = points[-1][1] if points else sum(r["downloads"] for r in rows)
    latest_day = points[-1][0] if points else ""

    width, height = 960, 400
    left, right, top, bottom = 88, 36, 78, 64
    plot_w = width - left - right
    plot_h = height - top - bottom
    y_max = nice_ticks(max(total, 1))[-1]
    ticks = nice_ticks(y_max)

    def x_at(index: int) -> float:
        if len(points) <= 1:
            return left + plot_w / 2
        return left + plot_w * index / (len(points) - 1)

    def y_at(value: int) -> float:
        return top + plot_h * (1 - value / y_max) if y_max else top + plot_h

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="rosswang Obsidian 插件总下载量趋势">',
        "<defs>",
        '<linearGradient id="area" x1="0" y1="0" x2="0" y2="1">',
        '<stop offset="0%" stop-color="#7c3aed" stop-opacity="0.28"/>',
        '<stop offset="100%" stop-color="#7c3aed" stop-opacity="0.02"/>',
        "</linearGradient>",
        "</defs>",
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{left}" y="36" font-family="-apple-system, BlinkMacSystemFont, \'Segoe UI\', Helvetica, Arial, sans-serif" '
        f'font-size="22" font-weight="700" fill="#111827">rosswang · Obsidian 插件总下载量</text>',
        f'<text x="{left}" y="58" font-family="-apple-system, BlinkMacSystemFont, \'Segoe UI\', Helvetica, Arial, sans-serif" '
        f'font-size="13" fill="#6b7280">数据源 obsidianmd/obsidian-releases · 每日自动更新</text>',
    ]

    # 横向网格与 Y 轴刻度
    for tick in ticks:
        y = y_at(tick)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            f'stroke="#e5e7eb" stroke-width="1" stroke-dasharray="4 4"/>'
        )
        parts.append(
            f'<text x="{left - 12}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-family="-apple-system, BlinkMacSystemFont, \'Segoe UI\', Helvetica, Arial, sans-serif" '
            f'font-size="12" fill="#6b7280">{tick:,}</text>'
        )

    # X 轴
    axis_y = top + plot_h
    parts.append(
        f'<line x1="{left}" y1="{axis_y}" x2="{width - right}" y2="{axis_y}" stroke="#9ca3af" stroke-width="1"/>'
    )

    if points:
        coords = [(x_at(i), y_at(value)) for i, (_, value) in enumerate(points)]

        if len(coords) > 1:
            area = [f"M {coords[0][0]:.1f} {axis_y:.1f}"]
            area += [f"L {x:.1f} {y:.1f}" for x, y in coords]
            area.append(f"L {coords[-1][0]:.1f} {axis_y:.1f} Z")
            parts.append(f'<path d="{" ".join(area)}" fill="url(#area)"/>')
            polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
            parts.append(
                f'<polyline points="{polyline}" fill="none" stroke="#7c3aed" '
                f'stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>'
            )

        for x, y in coords:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="#7c3aed" stroke="#ffffff" stroke-width="2"/>')

        last_x, last_y = coords[-1]
        parts.append(
            f'<text x="{last_x:.1f}" y="{last_y - 14:.1f}" text-anchor="middle" '
            f'font-family="-apple-system, BlinkMacSystemFont, \'Segoe UI\', Helvetica, Arial, sans-serif" '
            f'font-size="14" font-weight="700" fill="#5b21b6">{total:,}</text>'
        )

        # 最多 6 个 X 轴日期标签
        if len(points) <= 6:
            label_indices = list(range(len(points)))
        else:
            label_indices = sorted({round(i * (len(points) - 1) / 5) for i in range(6)})
        for i in label_indices:
            x = x_at(i)
            label = points[i][0][5:] if len(points[i][0]) >= 10 else points[i][0]
            parts.append(
                f'<text x="{x:.1f}" y="{axis_y + 26}" text-anchor="middle" '
                f'font-family="-apple-system, BlinkMacSystemFont, \'Segoe UI\', Helvetica, Arial, sans-serif" '
                f'font-size="12" fill="#6b7280">{escape(label)}</text>'
            )
    else:
        parts.append(
            f'<text x="{left + plot_w / 2}" y="{top + plot_h / 2}" text-anchor="middle" '
            f'font-family="-apple-system, BlinkMacSystemFont, \'Segoe UI\', Helvetica, Arial, sans-serif" '
            f'font-size="14" fill="#9ca3af">暂无数据</text>'
        )

    footer = f"截至 {latest_day} · 共 {len(rows)} 个已发布插件"
    if len(points) <= 1:
        footer += " · 首日记录，后续每日累积"
    parts.append(
        f'<text x="{left}" y="{height - 18}" font-family="-apple-system, BlinkMacSystemFont, \'Segoe UI\', Helvetica, Arial, sans-serif" '
        f'font-size="12" fill="#9ca3af">{escape(footer)}</text>'
    )
    parts.append("</svg>")

    out_path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def render_readme(rows: list[dict], history: dict, snapshot: dict) -> str:
    total = snapshot["total"]
    day = snapshot["date"]
    updated = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M")
    lines = [
        "# rosswang 的 Obsidian 插件下载统计",
        "",
        "每天自动抓取 [Obsidian 社区插件市场](https://community.obsidian.md/) 中"
        f"作者为 `{AUTHOR}` 的**已发布**插件下载量，累计后生成趋势图。",
        "",
        "![总下载量趋势](chart.svg)",
        "",
        f"**总下载量：{total:,}** · {len(rows)} 个已发布插件 · 更新于 {updated}（Asia/Shanghai）",
        "",
        "## 插件列表（仅已发布）",
        "",
        "| 插件 | 市场页面 | GitHub 仓库 | 下载量 | 一句话说明 |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for row in rows:
        market = f"[市场]({row['market_url']})"
        github = f"[{row['repo']}]({row['github_url']})" if row["github_url"] else "—"
        lines.append(
            f"| **{row['name']}**<br>`{row['id']}` | {market} | {github} | "
            f"{row['downloads']:,} | {row['description']} |"
        )
    lines += [
        f"| **合计** | | | **{total:,}** | |",
        "",
        "> 下载量优先取自 Obsidian 官方仓库的 `community-plugin-stats.json`；\n"
        "> 若某插件尚未被该统计文件收录（如刚上架），则回退到官方市场页面读取真实下载量。\n"
        "插件是否发布以 `community-plugins.json` 为准，未上架/已下架插件不会出现在表中。",
        "",
        "## 数据文件",
        "",
        "- `data/history.json`：每日快照（总量 + 各插件下载量），用于绘制趋势图",
        "- `chart.svg`：总下载量趋势图",
        "- `fetch_stats.py`：抓取与生成脚本（仅用 Python 标准库）",
        "",
        "## 自动化",
        "",
        "GitHub Actions 每天 UTC 01:00（北京时间 09:00）运行一次，更新 "
        "`README.md`、`chart.svg` 和 `data/history.json` 并自动提交。",
        "",
        "本地运行：",
        "",
        "```bash",
        "python fetch_stats.py",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-file", default="", help="本地 community-plugins.json（调试用）")
    parser.add_argument("--stats-file", default="", help="本地 community-plugin-stats.json（调试用）")
    parser.add_argument("--out-dir", default=str(ROOT), help="输出目录，默认脚本所在目录")
    args = parser.parse_args()

    out_dir = Path(args.out_dir).resolve()
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(TIMEZONE)
    day = now.strftime("%Y-%m-%d")
    token = os.getenv("GITHUB_TOKEN", "")

    catalog = load_payload(CATALOG, args.catalog_file, CATALOG_URLS, token)
    stats = load_payload(STATS, args.stats_file, STATS_URLS, token)
    if not isinstance(catalog, list):
        raise SystemExit("community-plugins.json 格式异常：应为数组")
    if not isinstance(stats, dict):
        raise SystemExit("community-plugin-stats.json 格式异常：应为对象")

    rows = collect_plugins(catalog, stats)
    if not rows:
        raise SystemExit(f"没有找到作者为 {AUTHOR} 的已发布插件")

    history_path = data_dir / "history.json"
    history = load_history(history_path)
    snapshot = upsert_snapshot(history, rows, day)

    history_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    render_chart(history, rows, out_dir / "chart.svg")
    (out_dir / "README.md").write_text(
        render_readme(rows, history, snapshot), encoding="utf-8"
    )

    print(f"{day} 共 {len(rows)} 个已发布插件，总下载量 {snapshot['total']:,}")
    for row in rows:
        print(f"  {row['downloads']:>7,}  {row['name']} ({row['id']})")
    print(f"已写入：{out_dir / 'README.md'}")
    print(f"已写入：{out_dir / 'chart.svg'}")
    print(f"已写入：{history_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
