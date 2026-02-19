#!/usr/bin/env python3
"""Generate static GitHub Pages catalog for firmware release assets."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name format")
    parser.add_argument("--token", required=True, help="GitHub token for API access")
    parser.add_argument("--output-dir", default="site", help="Directory for generated files")
    return parser.parse_args()


def api_request(url: str, token: str) -> str:
    req = Request(
        url=url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "meshtastic-release-pages-generator",
        },
    )
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def api_get_json(url: str, token: str) -> Any:
    return json.loads(api_request(url, token))


def fetch_releases(repo: str, token: str) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    page = 1
    while True:
        api_url = f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
        chunk = api_get_json(api_url, token)
        if not chunk:
            break
        releases.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    visible = [rel for rel in releases if not rel.get("draft", False)]
    visible.sort(
        key=lambda rel: rel.get("published_at") or rel.get("created_at") or "",
        reverse=True,
    )
    return visible


def fmt_size(num_bytes: int) -> str:
    size = float(num_bytes)
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def parse_version_label(assets: list[dict[str, Any]]) -> str:
    for asset in assets:
        name = asset.get("name", "")
        if name.startswith("firmware-all-") and name.endswith(".zip"):
            return name[len("firmware-all-") : -len(".zip")]
    for asset in assets:
        name = asset.get("name", "")
        if name.startswith("firmware-bundle-") and name.endswith(".tar.gz"):
            return name[len("firmware-bundle-") : -len(".tar.gz")]
    return ""


def parse_timestamp(raw: str) -> tuple[str, str]:
    if not raw:
        return "", ""
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw, raw
    return parsed.strftime("%Y-%m-%d %H:%M UTC"), parsed.isoformat()


def derive_device_slug(asset_name: str, version_label: str) -> str:
    if version_label:
        m = re.match(
            rf"^firmware-(.+)-{re.escape(version_label)}(?:-(\d+))?\.zip$",
            asset_name,
        )
        if m:
            return m.group(1)
    fallback = asset_name[len("firmware-") : -len(".zip")]
    return fallback or "unknown-device"


def derive_variant_label(asset_name: str, device_slug: str, version_label: str) -> str:
    if version_label:
        pattern = rf"^firmware-{re.escape(device_slug)}-{re.escape(version_label)}(?:-(\d+))?\.zip$"
        m = re.match(pattern, asset_name)
        if m:
            suffix = m.group(1)
            return "main" if not suffix else f"variant {suffix}"
    return "archive"


def source_sort_key(source_name: str) -> tuple[int, str]:
    if source_name == "unknown-build-list":
        return (1, source_name)
    return (0, source_name)


def normalize_source_name(raw: str) -> str:
    value = raw.strip()
    if not value or value == "unknown":
        return "unknown-build-list"
    if value == "unknown-build-list":
        return value
    if value.endswith(".yaml"):
        return value
    if value.startswith("build_list"):
        return f"{value}.yaml"
    return value


def pick_source_name(meta: dict[str, str]) -> str:
    for key in ("build_list_file", "build_list_slug", "source_slug"):
        raw = str(meta.get(key, "")).strip()
        if raw and raw not in {"unknown", "unknown-build-list"}:
            return normalize_source_name(raw)
    return "unknown-build-list"


def load_release_matrix(
    matrix_asset: dict[str, Any] | None,
    token: str,
) -> dict[str, dict[str, str]]:
    if not matrix_asset:
        return {}
    url = matrix_asset.get("browser_download_url", "")
    if not url:
        return {}

    try:
        payload = api_get_json(url, token)
    except (HTTPError, URLError, TimeoutError, ValueError):
        return {}

    if not isinstance(payload, list):
        return {}

    by_archive: dict[str, dict[str, str]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        archive_name = str(item.get("archive_name", "")).strip()
        if not archive_name:
            continue
        by_archive[archive_name] = {
            "build_list_file": str(item.get("build_list_file", "")).strip(),
            "build_list_slug": str(item.get("build_list_slug", "")).strip(),
            "source_slug": str(item.get("source_slug", "")).strip(),
        }
    return by_archive


def classify_release(raw_release: dict[str, Any], token: str) -> dict[str, Any]:
    assets = raw_release.get("assets", [])
    version_label = parse_version_label(assets)

    firmware_all = None
    firmware_bundle = None
    checksums = None
    files_manifest = None
    build_info = None
    release_matrix_asset = None
    per_device_assets: list[dict[str, Any]] = []
    other_assets: list[dict[str, Any]] = []

    for asset in assets:
        name = asset.get("name", "")
        if name.startswith("firmware-all-") and name.endswith(".zip"):
            firmware_all = asset
        elif name.startswith("firmware-bundle-") and name.endswith(".tar.gz"):
            firmware_bundle = asset
        elif name == "SHA256SUMS.txt":
            checksums = asset
        elif name == "FILES.txt":
            files_manifest = asset
        elif name == "BUILD_INFO.json":
            build_info = asset
        elif name == "RELEASE_MATRIX.json":
            release_matrix_asset = asset
        elif name.startswith("firmware-") and name.endswith(".zip"):
            per_device_assets.append(asset)
        else:
            other_assets.append(asset)

    matrix_by_archive = load_release_matrix(release_matrix_asset, token)

    device_groups: dict[str, dict[str, list[dict[str, str]]]] = {}
    for asset in per_device_assets:
        asset_name = asset.get("name", "")
        device_slug = derive_device_slug(asset_name, version_label)
        variant_label = derive_variant_label(asset_name, device_slug, version_label)
        source_name = pick_source_name(matrix_by_archive.get(asset_name, {}))

        device_group = device_groups.setdefault(device_slug, {})
        source_group = device_group.setdefault(source_name, [])
        source_group.append(
            {
                "variant_label": variant_label,
                "asset_name": asset_name,
                "download_url": asset.get("browser_download_url", ""),
                "size_text": fmt_size(int(asset.get("size", 0))),
            }
        )

    for device_slug in list(device_groups.keys()):
        sorted_sources: dict[str, list[dict[str, str]]] = {}
        for source_name in sorted(device_groups[device_slug], key=source_sort_key):
            rows = device_groups[device_slug][source_name]
            rows.sort(key=lambda item: item["asset_name"])
            sorted_sources[source_name] = rows
        device_groups[device_slug] = sorted_sources

    published_raw = raw_release.get("published_at") or raw_release.get("created_at") or ""
    published_text, published_sort = parse_timestamp(published_raw)
    release_name = raw_release.get("name") or raw_release.get("tag_name") or "Untitled release"
    release_tag = raw_release.get("tag_name", "")

    unique_sources: set[str] = set()
    search_tokens = [release_name.lower(), release_tag.lower(), version_label.lower()]
    for device_slug, source_groups in device_groups.items():
        search_tokens.append(device_slug.lower())
        for source_name, rows in source_groups.items():
            unique_sources.add(source_name)
            search_tokens.append(source_name.lower())
            for row in rows:
                search_tokens.append(row["asset_name"].lower())
                search_tokens.append(row["variant_label"].lower())

    return {
        "name": release_name,
        "tag_name": release_tag,
        "html_url": raw_release.get("html_url", ""),
        "is_prerelease": bool(raw_release.get("prerelease")),
        "published_at": published_text or published_raw,
        "published_at_sort": published_sort or published_raw,
        "assets_total": len(assets),
        "devices_total": len(device_groups),
        "sources_total": len(unique_sources),
        "version_label": version_label,
        "firmware_all": firmware_all,
        "firmware_bundle": firmware_bundle,
        "checksums": checksums,
        "files_manifest": files_manifest,
        "build_info": build_info,
        "release_matrix_asset": release_matrix_asset,
        "device_groups": dict(sorted(device_groups.items(), key=lambda item: item[0])),
        "other_assets": sorted(other_assets, key=lambda item: item.get("name", "")),
        "search_blob": " ".join(token for token in search_tokens if token),
    }


def build_stats(releases: list[dict[str, Any]]) -> dict[str, int]:
    device_names: set[str] = set()
    source_names: set[str] = set()
    variants_total = 0
    for release in releases:
        groups = release.get("device_groups", {})
        device_names.update(groups.keys())
        for source_groups in groups.values():
            source_names.update(source_groups.keys())
            for rows in source_groups.values():
                variants_total += len(rows)
    return {
        "releases_total": len(releases),
        "devices_total": len(device_names),
        "sources_total": len(source_names),
        "variants_total": variants_total,
    }


def render_asset_chip(label: str, asset: dict[str, Any] | None) -> str:
    if not asset:
        return ""
    url = html.escape(asset.get("browser_download_url", ""))
    size = fmt_size(int(asset.get("size", 0)))
    title = html.escape(asset.get("name", label))
    return f'<a class="chip" href="{url}" title="{title}">{html.escape(label)} · {size}</a>'


def render_source_name(source_name: str) -> str:
    if source_name == "unknown-build-list":
        return "unknown"
    return source_name


def render_release_card(release: dict[str, Any]) -> str:
    prerelease_badge = '<span class="badge">pre-release</span>' if release["is_prerelease"] else ""
    quick_links = "".join(
        [
            render_asset_chip("All firmware BIN", release["firmware_all"]),
            render_asset_chip("Bundle", release["firmware_bundle"]),
            render_asset_chip("SHA256SUMS", release["checksums"]),
            render_asset_chip("FILES", release["files_manifest"]),
            render_asset_chip("BUILD_INFO", release["build_info"]),
            render_asset_chip("RELEASE_MATRIX", release["release_matrix_asset"]),
        ]
    )

    device_cards: list[str] = []
    for device_slug, source_groups in release["device_groups"].items():
        table_rows: list[str] = []
        search_tokens = [device_slug.lower()]
        for source_name, rows in source_groups.items():
            source_display = render_source_name(source_name)
            search_tokens.append(source_name.lower())
            for row in rows:
                search_tokens.append(row["asset_name"].lower())
                table_rows.append(
                    "<tr>"
                    f'<td><code>{html.escape(source_display)}</code></td>'
                    f'<td><a href="{html.escape(row["download_url"])}">{html.escape(row["asset_name"])}</a></td>'
                    f'<td class="muted">{html.escape(row["variant_label"])}</td>'
                    f'<td class="muted">{html.escape(row["size_text"])}</td>'
                    "</tr>"
                )

        if table_rows:
            table_html = (
                '<table class="fw-table">'
                "<thead><tr><th>Source (build_list)</th><th>Archive</th><th>Variant</th><th>Size</th></tr></thead>"
                f"<tbody>{''.join(table_rows)}</tbody>"
                "</table>"
            )
        else:
            table_html = '<p class="muted">No firmware archives for this device.</p>'

        device_cards.append(
            f"""
            <article class="device-card" data-search="{html.escape(' '.join(search_tokens))}">
              <h3>{html.escape(device_slug)}</h3>
              {table_html}
            </article>
            """
        )

    if not device_cards:
        device_cards.append('<p class="muted">No per-device firmware archives found for this release.</p>')

    return f"""
    <section class="release-card" data-search="{html.escape(release["search_blob"])}">
      <header class="release-header">
        <div class="release-title">
          <h2>{html.escape(release["name"])} {prerelease_badge}</h2>
          <p class="muted">
            Tag <code>{html.escape(release["tag_name"])}</code> ·
            Published {html.escape(release["published_at"])} ·
            Devices {release["devices_total"]} ·
            Sources {release["sources_total"]} ·
            Assets {release["assets_total"]}
          </p>
        </div>
        <a class="open-release" href="{html.escape(release["html_url"])}">Open release</a>
      </header>
      <div class="quick-links">{quick_links}</div>
      <div class="device-grid">
        {''.join(device_cards)}
      </div>
    </section>
    """


def render_html(repo: str, releases: list[dict[str, Any]]) -> str:
    stats = build_stats(releases)
    generated_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    release_blocks = "".join(render_release_card(release) for release in releases)
    if not release_blocks:
        release_blocks = '<p class="muted">No published releases available.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Meshtastic Firmware Catalog</title>
  <style>
    @import url("https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap");
    :root {{
      --surface: #f7fafc;
      --surface-card: #ffffff;
      --line: #d6dde8;
      --ink: #1c2738;
      --muted: #5a687a;
      --accent: #0c8c7c;
      --accent-strong: #0f5f84;
      --chip-bg: #ecf8f7;
      --hero-a: #1b3055;
      --hero-b: #0f6f63;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: "Space Grotesk", "Trebuchet MS", sans-serif;
      background:
        radial-gradient(circle at 8% 8%, #dff7f2 0, #dff7f200 35%),
        radial-gradient(circle at 92% 2%, #cce5ff 0, #cce5ff00 30%),
        var(--surface);
      min-height: 100vh;
    }}
    .layout {{
      max-width: 1260px;
      margin: 0 auto;
      padding: 24px 18px 36px;
    }}
    .hero {{
      background: linear-gradient(125deg, var(--hero-a), var(--hero-b));
      border-radius: 22px;
      padding: 22px;
      color: #f4f8ff;
      box-shadow: 0 18px 36px rgba(12, 42, 63, 0.25);
      margin-bottom: 16px;
    }}
    .hero h1 {{
      margin: 0 0 10px;
      font-size: clamp(26px, 3vw, 38px);
      letter-spacing: 0.02em;
    }}
    .hero p {{
      margin: 0;
      color: #d7e5ff;
      font-size: 14px;
    }}
    .hero code {{
      font-family: "IBM Plex Mono", monospace;
      background: #ffffff1a;
      border: 1px solid #ffffff3b;
      border-radius: 8px;
      padding: 1px 6px;
      color: #f4faff;
    }}
    .stats {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 14px;
    }}
    .stat {{
      border: 1px solid #ffffff30;
      background: #0b1f34b8;
      padding: 7px 12px;
      border-radius: 999px;
      font-size: 13px;
      color: #d6ecff;
    }}
    .panel {{
      background: #ffffffdd;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
      margin-bottom: 14px;
      backdrop-filter: blur(4px);
    }}
    .search-row {{
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .search-row label {{
      font-size: 14px;
      font-weight: 500;
      color: var(--muted);
    }}
    .search-input {{
      flex: 1;
      min-width: 240px;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      font-family: "IBM Plex Mono", monospace;
      color: var(--ink);
      background: #fff;
    }}
    .release-card {{
      background: var(--surface-card);
      border: 1px solid var(--line);
      border-radius: 16px;
      box-shadow: 0 10px 24px rgba(23, 33, 52, 0.07);
      padding: 14px;
      margin-bottom: 14px;
    }}
    .release-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 10px;
    }}
    .release-title h2 {{
      margin: 0 0 6px;
      font-size: clamp(18px, 2.2vw, 24px);
    }}
    .open-release {{
      display: inline-block;
      text-decoration: none;
      background: linear-gradient(135deg, var(--accent), var(--accent-strong));
      color: #f7fcff;
      border-radius: 10px;
      padding: 10px 12px;
      font-weight: 600;
      white-space: nowrap;
    }}
    .quick-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 12px;
    }}
    .chip {{
      display: inline-block;
      text-decoration: none;
      color: #0d6458;
      background: var(--chip-bg);
      border: 1px solid #b8ddd7;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      font-weight: 500;
    }}
    .device-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 10px;
    }}
    .device-card {{
      background: linear-gradient(180deg, #ffffff, #fbfdff);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px;
      overflow-x: auto;
    }}
    .device-card h3 {{
      margin: 0 0 8px;
      font-size: 15px;
      color: #132943;
    }}
    .fw-table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 480px;
    }}
    .fw-table th, .fw-table td {{
      border-top: 1px solid #e5ebf2;
      padding: 6px 7px;
      text-align: left;
      vertical-align: top;
      font-size: 12px;
    }}
    .fw-table th {{
      color: #3a4a61;
      font-weight: 600;
      background: #f7fafc;
    }}
    .fw-table a {{
      color: #0f5f84;
      text-decoration: none;
      font-family: "IBM Plex Mono", monospace;
      font-size: 12px;
    }}
    .muted {{
      color: var(--muted);
      font-size: 12px;
    }}
    .badge {{
      display: inline-block;
      vertical-align: middle;
      margin-left: 6px;
      border-radius: 999px;
      border: 1px solid #ffd383;
      background: #fff2cc;
      color: #5f3a00;
      font-size: 11px;
      padding: 3px 7px;
    }}
    code {{
      font-family: "IBM Plex Mono", monospace;
      background: #eff4fb;
      border-radius: 6px;
      padding: 1px 6px;
      font-size: 12px;
    }}
    footer {{
      margin-top: 18px;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 760px) {{
      .layout {{ padding: 16px 12px 26px; }}
      .hero {{ border-radius: 14px; }}
      .release-header {{ flex-direction: column; }}
      .open-release {{ width: 100%; text-align: center; }}
      .device-grid {{ grid-template-columns: 1fr; }}
      .fw-table {{ min-width: 420px; }}
    }}
  </style>
</head>
<body>
  <main class="layout">
    <header class="hero">
      <h1>Firmware Release Catalog</h1>
      <p>Repository <code>{html.escape(repo)}</code> · Generated {generated_at}</p>
      <div class="stats">
        <span class="stat">Releases: {stats["releases_total"]}</span>
        <span class="stat">Devices: {stats["devices_total"]}</span>
        <span class="stat">Sources: {stats["sources_total"]}</span>
        <span class="stat">Device archives: {stats["variants_total"]}</span>
      </div>
    </header>

    <section class="panel">
      <div class="search-row">
        <label for="device-search">Filter by device, source, tag, or asset name:</label>
        <input id="device-search" class="search-input" type="search" placeholder="e.g. heltec-v2_1 or build_list_svk.yaml" />
      </div>
      <p id="search-status" class="muted">Showing all releases.</p>
    </section>

    <section id="releases-list">
      {release_blocks}
    </section>

    <footer>All links point directly to GitHub Release assets.</footer>
  </main>

  <script>
    const searchInput = document.getElementById("device-search");
    const status = document.getElementById("search-status");
    const releaseCards = Array.from(document.querySelectorAll(".release-card"));
    const normalize = (value) => value.trim().toLowerCase();

    function applyFilter() {{
      const query = normalize(searchInput.value);
      let visibleReleases = 0;
      let visibleDevices = 0;

      releaseCards.forEach((releaseCard) => {{
        const releaseMatch = releaseCard.dataset.search.includes(query);
        const deviceCards = Array.from(releaseCard.querySelectorAll(".device-card"));
        let matchedDevicesInRelease = 0;

        deviceCards.forEach((deviceCard) => {{
          const deviceMatch = !query || releaseMatch || deviceCard.dataset.search.includes(query);
          deviceCard.style.display = deviceMatch ? "" : "none";
          if (deviceMatch) {{
            matchedDevicesInRelease += 1;
            visibleDevices += 1;
          }}
        }});

        const showRelease = !query || releaseMatch || matchedDevicesInRelease > 0;
        releaseCard.style.display = showRelease ? "" : "none";
        if (showRelease) {{
          visibleReleases += 1;
        }}
      }});

      if (!query) {{
        status.textContent = "Showing all releases.";
      }} else {{
        status.textContent = `Matched releases: ${{visibleReleases}}, matched devices: ${{visibleDevices}}.`;
      }}
    }}

    searchInput.addEventListener("input", applyFilter);
  </script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_releases = fetch_releases(args.repo, args.token)
    releases = [classify_release(item, args.token) for item in raw_releases]

    (output_dir / "index.html").write_text(render_html(args.repo, releases), encoding="utf-8")
    (output_dir / "releases.json").write_text(
        json.dumps(releases, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
