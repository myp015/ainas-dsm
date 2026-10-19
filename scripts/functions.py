# -*- coding: utf-8 -*-
#
# Copyright (C) 2025 AuxXxilium <https://github.com/AuxXxilium>
#
# This is free software, licensed under the MIT License.
# See /LICENSE for more information.
#

import os, re, yaml, click, requests, urllib3
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry  # type: ignore
import xml.etree.ElementTree as ET

# 目标 DSM build 白名单，元素为 (下载路径版本, build)。
# 下载路径版本 = major.minor[.patch]，即 Synology 下载路径里的那一级目录名，
# 例如 7.2.1-69057 对应 ("7.2.1", "69057")，7.0.1-42218 对应 ("7.0.1", "42218")。
# 只有命中白名单的 build 会被抓取；命中后该 build 下所有真实可下载的 nano（更新号）都会收录。
# 需要增删版本时只改这里。
TARGET_BUILDS = {
    ("7.0.1", "42218"),
    ("7.1.1", "42962"),
    ("7.2.1", "69057"),
}

# 候选 PAT 地址的探测参数。反推出来的地址必须逐个确认可用性，串行太慢。
# CDN 对部分地址会长时间不响应，所以第一遍用短超时抢吞吐，
# 超时/异常的在第二遍用宽松超时、低并发重试，避免把可用地址误判成不可用。
PROBE_TIMEOUT = (5, 10)          # (连接超时, 读取超时)，秒
PROBE_WORKERS = 32
PROBE_RETRY_TIMEOUT = (10, 30)
PROBE_RETRY_WORKERS = 8


@click.group()
def cli():
    pass


@cli.command()
@click.option("-w", "--workpath", type=str, required=True, help="Working directory (must contain configs/platforms.yml).")
@click.option("-j", "--jsonpath", type=str, required=True, help="Output YAML file path.")
def getpats(workpath, jsonpath):
    # --- Load supported platforms ---
    platforms_yml = os.path.join(workpath, "configs", "platforms.yml")
    try:
        with open(platforms_yml, "r", encoding="utf-8") as f:
            pcfg = yaml.safe_load(f) or {}
        platforms = pcfg.get("platforms", {}) if isinstance(pcfg, dict) else {}
        supported_platforms = {str(k).strip().lower() for k in platforms.keys()} if isinstance(platforms, dict) else set()
    except Exception as e:
        click.echo(f"Error loading {platforms_yml}: {e}", err=True)
        return

    if not supported_platforms:
        click.echo("No supported platforms found in platforms.yml", err=True)
        return

    # --- Session setup ---
    adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504]))
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.trust_env = False
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # --- Step 1: RSS → 机型映射 + DSM_Enterprise PAT ---
    # model_platforms: 小写裸机型名 -> [platform]，裸机型名即 mUnique 去掉 synology_<platform>_ 后的部分
    # canonical: (platform, 小写裸机型名) -> 完整机型名（如 DS918+）
    #   增量包命名 synology_<platform>_<裸机型名>.pat 与 RSS mUnique 同构，
    #   反推完整 PAT 地址时需要把裸机型名还原成完整机型名。
    # enterprise_pats: DSM_Enterprise 的 (platform, 机型名, 版本串, url)
    #   DSM_Enterprise 自身产品版本是 "1.x"，但 dsm/ 目录要用底层 DSM 版本
    #   （RSS item 里的 ReqMajorVer.ReqMinorVer.0-build-0）。
    model_platforms = {}
    canonical = {}
    enterprise_pats = []
    try:
        r = session.get("https://update7.synology.com/autoupdate/genRSS.php?include_beta=1", timeout=15)
        root = ET.fromstring(r.content)
        for item in root.iter("item"):
            req_major = item.findtext("ReqMajorVer") or ""
            req_minor = item.findtext("ReqMinorVer") or ""
            for model in item.iter("model"):
                munique = model.findtext("mUnique") or ""
                mlink   = model.findtext("mLink") or ""
                parts = munique.strip().split("_")
                if len(parts) < 3 or parts[0] != "synology":
                    continue
                platform = parts[1].lower()
                if platform not in supported_platforms:
                    continue
                bare = "_".join(parts[2:])
                if platform not in model_platforms.get(bare.lower(), []):
                    model_platforms.setdefault(bare.lower(), []).append(platform)

                # 从 PAT 文件名取完整机型名，如 DSM_DS918%2B_64570.pat -> DS918+
                fname = unquote(mlink.split("/")[-1]).replace(".pat", "")
                fparts = fname.split("_")
                cased = "_".join(fparts[1:-1]) if len(fparts) >= 3 else ""
                if cased.lower().startswith("enterprise_"):
                    cased = cased[len("Enterprise_"):]
                if cased:
                    canonical[(platform, bare.lower())] = cased

                # DSM_Enterprise：build 版本串取自 mLink 的 release 路径
                if "/download/dsm_enterprise/" in mlink.lower() and req_major and req_minor:
                    # URL: .../DSM_Enterprise/release/1.0/101188/DSM_Enterprise_PAS7700_101188.pat
                    m = re.search(r'/release/[^/]+/(\d+)/', mlink)
                    if m:
                        version_str = f"{req_major}.{req_minor}.0-{m.group(1)}-0"
                        url = mlink.replace("global.synologydownload.com", "global.download.synology.com")
                        enterprise_pats.append((platform, cased, version_str, url))
    except Exception as e:
        click.echo(f"Error fetching RSS: {e}", err=True)
        return

    if not model_platforms:
        click.echo("No models found in RSS feed", err=True)
        return

    # --- Step 2: archive 索引页 → 白名单内的版本页 ---
    # 每项为 (页面 URL, 版本串, 下载路径版本, build, nano)，版本串即 dsm/ 下的目录名
    try:
        r = session.get("https://archive.synology.com/download/Os/DSM", timeout=15, verify=False)
        r.encoding = "utf-8"
    except Exception as e:
        click.echo(f"Error fetching archive index: {e}", err=True)
        return

    index_pat = re.compile(r'href=["\']?/download/Os/[^/]+/(\d+)\.(\d+)(?:\.(\d+))?-(\d+)(?:-(\d+))?(?:-NanoPacked)?["\']?')
    seen = set()
    entries = []
    for m in index_pat.finditer(r.text):
        major, minor = int(m.group(1)), int(m.group(2))
        if (major, minor) < (7, 0):
            continue
        key = (major, minor, m.group(3) or "0", m.group(4), m.group(5) or "0")
        if key not in seen:
            seen.add(key)
            entries.append(key)
    entries.sort(key=lambda v: (v[0], v[1], v[2], int(v[3]), int(v[4])))

    pages_to_scrape = []
    for (major, minor, patch, build, nano) in entries:
        path_ver = f"{major}.{minor}" + (f".{patch}" if patch != "0" else "")
        if (path_ver, build) not in TARGET_BUILDS:
            continue
        archive_path = f"{path_ver}-{build}" + (f"-{nano}" if nano != "0" else "")
        version_str = f"{major}.{minor}.{patch}-{build}-{nano}"
        pages_to_scrape.append((f"https://archive.synology.com/download/Os/DSM/{archive_path}",
                                version_str, path_ver, build, nano))

    if not pages_to_scrape:
        click.echo("No archive pages matched TARGET_BUILDS", err=True)
        return

    # --- Step 3: 收集候选完整 PAT 地址 ---
    # 两个来源：
    #   a) 版本页直接列出的 /download/DSM/release/*.pat —— 基础版本页（nano=0）通常列全
    #   b) 版本页上的增量包 /download/DSM/criticalupdate/update_pack/<build>-<nano>/synology_<platform>_<裸机型名>.pat
    #      按同样的路径规则反推完整 PAT 地址。X.Y.Z-BUILD-N (N>=1) 的版本页只列增量包、
    #      不列完整 PAT，所以 7.1.1/7.2.1 这类版本必须靠反推才拿得到。
    # 反推出来的地址未必真实存在，Step 4 会逐个探测。
    candidates = {}  # (platform, model, version_str) -> url
    for (page_url, version_str, path_ver, build, nano) in pages_to_scrape:
        try:
            r = session.get(page_url, timeout=15, verify=False)
            r.encoding = "utf-8"
        except Exception:
            continue
        seg = f"{build}-{nano}" if nano != "0" else build

        # a) 页面直接列出的完整 release PAT
        for m in re.finditer(r'href=["\']?(https://[^"\']*\.pat|/download/[^"\']*\.pat)["\']?', r.text):
            link = m.group(1)
            if not link.startswith("http"):
                link = "https://global.synologydownload.com" + link
            link = link.split("?")[0]
            if "/download/dsm/release/" not in link.lower():
                continue
            filename = unquote(link.split("/")[-1]).replace(".pat", "")
            parts = filename.split("_")
            if len(parts) < 3:
                continue
            model_name = "_".join(parts[1:-1])
            if model_name.lower().startswith("enterprise_"):
                model_name = model_name[len("Enterprise_"):]
            model_key = model_name.lower()

            # RSS mUnique 去掉了 DS/RS/FS 等前缀（如 "3622xs+" 而非 "ds3622xs+"），
            # 先按完整名查，查不到再去前缀查
            if model_key in model_platforms:
                lookup_key = model_key
            else:
                bare = re.sub(r'^(DS|RS|FS|SA|HD|DVA|PAS)', '', model_name, flags=re.IGNORECASE).lower()
                lookup_key = bare if bare in model_platforms else None
            if not lookup_key:
                continue

            for platform in model_platforms[lookup_key]:
                candidates[(platform, model_name, version_str)] = link

        # b) 增量包反推完整 release PAT
        for m in re.finditer(r'update_pack/[0-9]+-[0-9]+/synology_([a-z0-9]+)_([^"\']+?)\.pat', r.text):
            platform = m.group(1).lower()
            if platform not in supported_platforms:
                continue
            model_name = canonical.get((platform, unquote(m.group(2)).lower()))
            if not model_name:
                continue
            # 机型名里的 "+" 按 Synology 官方链接的写法编码成 %2B，与页面直链保持一致
            candidates[(platform, model_name, version_str)] = (
                f"https://global.synologydownload.com/download/DSM/release/{path_ver}/{seg}/"
                f"DSM_{model_name.replace('+', '%2B')}_{build}.pat"
            )

    if not candidates:
        click.echo("No PAT candidates found", err=True)
        return

    # --- Step 4: 探测候选地址，只保留真正可下载的 ---
    # 反推地址里有一部分在 CDN 上返回 403/404（例如 7.2.1-69057-1 的 DS620slim，
    # 增量包存在但完整 PAT 没有）。直接收录会让 update.sh 每次都下载失败，所以先确认。
    def probe(url, timeout):
        # 只请求第一个字节即可判定可用性。返回状态码；超时/网络异常返回 None（表示可重试）。
        try:
            resp = session.get(url, timeout=timeout, verify=False, stream=True, headers={"Range": "bytes=0-0"})
            code = resp.status_code
            resp.close()
            return code
        except Exception:
            return None

    def probe_batch(sub_keys, timeout, workers):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            codes = list(pool.map(lambda k: probe(candidates[k], timeout), sub_keys))
        return dict(zip(sub_keys, codes))

    keys = list(candidates)
    codes = probe_batch(keys, PROBE_TIMEOUT, PROBE_WORKERS)

    retry_keys = [k for k in keys if codes[k] is None]
    if retry_keys:
        click.echo(f"Retrying {len(retry_keys)} timed-out probe(s)")
        codes.update(probe_batch(retry_keys, PROBE_RETRY_TIMEOUT, PROBE_RETRY_WORKERS))

    pats = {}
    kept = 0
    for key in keys:
        if codes[key] not in (200, 206):
            continue
        platform, model_name, version_str = key
        pats.setdefault(platform, {}).setdefault(model_name, {})[version_str] = {
            "url": candidates[key].replace("global.synologydownload.com", "global.download.synology.com"),
            "hash": ""
        }
        kept += 1

    click.echo(f"PAT candidates: {len(candidates)}, available: {kept}")

    # --- Inject DSM_Enterprise entries (version string derived from RSS ReqMajorVer.ReqMinorVer) ---
    seen_enterprise = set()
    for (platform, cased_name, version_str, url) in enterprise_pats:
        key = (platform, cased_name, version_str)
        if key in seen_enterprise:
            continue
        seen_enterprise.add(key)
        pats.setdefault(platform, {}).setdefault(cased_name, {})[version_str] = {
            "url": url,
            "hash": ""
        }

    # --- Write output YAML ---
    class QuotedStr(str):
        pass

    def quoted_presenter(dumper, data):
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')

    yaml.add_representer(QuotedStr, quoted_presenter)

    def quote_values(obj, level=0):
        if isinstance(obj, dict):
            return {
                (QuotedStr(k) if level in (1, 2) else k): quote_values(v, level + 1)
                for k, v in obj.items()
            }
        if isinstance(obj, str):
            return QuotedStr(obj)
        return obj

    with open(jsonpath, "w", encoding="utf-8") as f:
        yaml.dump(quote_values(pats), f, indent=2, allow_unicode=True, sort_keys=False)

    total = sum(len(vs) for ms in pats.values() for vs in ms.values())
    click.echo(f"Written {total} entries across {len(pats)} platforms to {jsonpath}")


if __name__ == "__main__":
    cli()
