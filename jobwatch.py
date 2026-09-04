#!/usr/bin/env python3
"""
jobwatch_IT_Munich
==================
每天抓一次慕尼黑的 IT 工程类职位(Cloud / DevOps / Platform / AI / Software Engineer),
只报告"今天第一次见到"的那些。面向 1 年左右工作经验,过滤掉 Senior/Lead 等资深岗和 Data 类岗位。

核心思路:不相信任何平台标注的发布日期(StepStone 之类经常滞后半个月),
而是自己建库记录每个职位 ID 第一次出现的时间。首次出现 = 新职位。

数据源:arbeitnow.com 公开接口(GitHub 服务器可达)+ 公司自家招聘系统直连。
联邦劳动局接口封了 GitHub 的 IP,只能在自己电脑上开(config 里 arbeitsagentur.enabled)。

用法:
    python jobwatch.py run                # 抓取 + 差分 + 生成报告
    python jobwatch.py discover <slug>... # 探测某公司用的是哪套招聘系统
    python jobwatch.py stats              # 看数据库里有多少条
    python jobwatch.py reset              # 清空数据库重新开始
"""

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("缺少依赖,请先运行:  pip install requests pyyaml")

try:
    import yaml
except ImportError:
    sys.exit("缺少依赖,请先运行:  pip install requests pyyaml")



def _html_unescape(s):
    return re.sub(r'\s+', ' ', html.unescape(s or '')).strip()


BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "jobs.db"
CONFIG_PATH = BASE / "config.yaml"
COMPANIES_PATH = BASE / "companies.yaml"
OUT_HTML = BASE / "digest.html"
DOCS_HTML = BASE / "docs" / "index.html"   # GitHub Pages 用
IN_CI = bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "application/json, text/xml, */*"})


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------

@dataclass
class Job:
    uid: str            # 全局唯一 key,用于差分
    source: str         # 来源标识
    company: str
    title: str
    location: str
    url: str
    posted: str = ""    # 来源自己声称的发布日期(仅供参考,不作为判断依据)
    extra: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------

def load_yaml(path, default=None):
    if not path.exists():
        return default if default is not None else {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or (default if default is not None else {})


def load_config():
    cfg = load_yaml(CONFIG_PATH)
    if not cfg:
        sys.exit(f"找不到配置文件 {CONFIG_PATH}")
    cfg.setdefault("keywords", {})
    cfg["keywords"].setdefault("include", [])
    cfg["keywords"].setdefault("exclude", [])
    cfg.setdefault("location", {})
    cfg["location"].setdefault("terms", ["münchen", "munich", "muenchen"])
    cfg["location"].setdefault("allow_remote", True)
    cfg["location"].setdefault("remote_terms", ["deutschland", "germany", "bayern", "bavaria", "europe", "emea", "eu", "dach"])
    cfg["location"].setdefault("plz_prefixes", ["80", "81", "82", "85"])
    cfg.setdefault("arbeitnow", {})
    cfg["arbeitnow"].setdefault("enabled", True)
    cfg["arbeitnow"].setdefault("pages", 8)
    cfg.setdefault("arbeitsagentur", {})
    cfg["arbeitsagentur"].setdefault("enabled", False)
    cfg["arbeitsagentur"].setdefault("city", "München")
    cfg["arbeitsagentur"].setdefault("radius_km", 25)
    cfg["arbeitsagentur"].setdefault("published_within_days", 2)
    cfg["arbeitsagentur"].setdefault("queries", [""])
    cfg.setdefault("display", {})
    cfg["display"].setdefault("hide_tags", [])
    cfg.setdefault("telegram", {})
    cfg["telegram"].setdefault("enabled", False)
    cfg.setdefault("open_browser", True)
    return cfg




# ----------------------------------------------------------------------------
# 数据库(差分的核心)
# ----------------------------------------------------------------------------

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            uid        TEXT PRIMARY KEY,
            source     TEXT,
            company    TEXT,
            title      TEXT,
            location   TEXT,
            url        TEXT,
            posted     TEXT,
            first_seen TEXT,
            igm        TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.commit()
    return conn


TAGS_VERSION = "muc-2026-09-04a"   # 改了标签规则就改这个字符串,下次运行会给库里全部职位重新打标签


def retag_all(conn):
    """标签规则变了:给库里所有职位按当前规则重打标签(cats 在入库时冻结,否则老数据永远是旧标签)。"""
    row = conn.execute("SELECT v FROM meta WHERE k='tags_version'").fetchone()
    if row and row[0] == TAGS_VERSION:
        return
    comp_tags = {}
    for c in (load_yaml(COMPANIES_PATH, default={"companies": []}).get("companies") or []):
        if c.get("name"):
            comp_tags[c["name"].lower()] = c.get("tags") or []
    n = 0
    for uid, comp, title in conn.execute("SELECT uid, company, title FROM jobs").fetchall():
        j = Job(uid=uid, source="", company=comp or "", title=title or "", location="", url="")
        cats = classify(j, comp_tags.get((comp or "").lower()))
        conn.execute("UPDATE jobs SET igm=? WHERE uid=?", (",".join(cats), uid))
        n += 1
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('tags_version', ?)", (TAGS_VERSION,))
    conn.commit()
    if n:
        print(f"标签规则已更新为 {TAGS_VERSION},重打了 {n} 条")


def is_first_run(conn):
    row = conn.execute("SELECT v FROM meta WHERE k='seeded'").fetchone()
    return row is None


def mark_seeded(conn):
    # 记下基线里最晚的 first_seen,之后凡是严格大于它的才算"新"
    row = conn.execute("SELECT COALESCE(MAX(first_seen),'') FROM jobs").fetchone()
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('seeded', ?)", (row[0],))
    conn.commit()


def split_new(conn, jobs):
    """返回数据库里还没有的职位,并把全部职位写入库。"""
    known = {r[0] for r in conn.execute("SELECT uid FROM jobs")}
    now = datetime.now(timezone.utc).isoformat()
    fresh = []
    for j in jobs:
        if j.uid in known:
            continue
        known.add(j.uid)
        fresh.append(j)
        conn.execute(
            "INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
            (j.uid, j.source, j.company, j.title, j.location, j.url,
             j.posted, now, ",".join(j.extra.get("cats") or [])),
        )
    conn.commit()
    return fresh



def parse_posted(s):
    """把各来源的发布日期解析成 date。解析不出返回 None。
    覆盖:ISO 日期、Workday 的 'Posted 5 Days Ago' / 'Posted Today' / 'Posted 30+ Days Ago'。"""
    if not s:
        return None
    s = str(s).strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s)   # 31.08.2026
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    low = s.lower()
    today = datetime.now(timezone.utc).date()
    if "today" in low or "heute" in low or "gerade" in low:
        return today
    if "yesterday" in low or "gestern" in low:
        return today - timedelta(days=1)
    m = re.search(r"(\d+)\s*\+?\s*(?:day|tag)", low)
    if m:
        return today - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\s*\+?\s*(?:week|woche)", low)
    if m:
        return today - timedelta(weeks=int(m.group(1)))
    m = re.search(r"(\d+)\s*\+?\s*(?:month|monat)", low)
    if m:
        return today - timedelta(days=30 * int(m.group(1)))
    return None


def job_age_days(j):
    """职位"有多新"。优先用来源标注的发布日期,没有就用我们首次见到的日期。"""
    today = datetime.now(timezone.utc).date()
    d = parse_posted(j.posted)
    if d:
        return (today - d).days, "posted"
    fs = j.extra.get("first_seen") or ""
    if fs:
        try:
            return (today - datetime.fromisoformat(fs).date()).days, "seen"
        except ValueError:
            pass
    return None, ""


def age_label(j):
    n, how = job_age_days(j)
    if n is None:
        return ""
    src = "发布" if how == "posted" else "发现"
    if n <= 0:
        return f"{src}于今天"
    if n == 1:
        return f"{src}于昨天"
    return f"{src}于 {n} 天前"


def all_current(conn, limit=3000):
    """库中全部职位,新入库在前。"""
    rows = conn.execute(
        "SELECT company,title,location,url,posted,source,igm,first_seen FROM jobs "
        "ORDER BY first_seen DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_job(r) for r in rows]


def _row_to_job(r):
    c, ti, l, u, po, s, igm, fs = r
    j = Job(uid="", source=s, company=c, title=ti, location=l, url=u, posted=po)
    if igm:
        j.extra["cats"] = [x for x in igm.split(",") if x]
    j.extra["first_seen"] = fs
    return j


def recent_days(conn, days=2):
    """最近 N 天内首次出现的职位,按天分组(新的在前)。
    不再排除基线:基线本身就是"那天第一次看到的",同样有参考价值。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT company,title,location,url,posted,source,igm,first_seen FROM jobs "
        "WHERE first_seen >= ? ORDER BY first_seen DESC", (cutoff,)).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r[7][:10], []).append(_row_to_job(r))
    return sorted(groups.items(), reverse=True)



# ----------------------------------------------------------------------------
# 过滤
# ----------------------------------------------------------------------------

_KW_CACHE = {}


def kw_hit(term, text):
    """整词匹配,针对德语做了三点适配:
    · 字母边界把 äöüß 也算字母
    · 允许阴性后缀:"entwickler" 也命中 Entwicklerin / Entwickler:in / Entwickler*in / Entwickler/-in
    · 以 ~ 开头的词是词根,做子串匹配:"~entwickl" 命中 Softwareentwickler / Webentwicklung / Anwendungsentwickler
    """
    term = (term or "").strip().lower()
    if not term:
        return False
    pat = _KW_CACHE.get(term)
    if pat is None:
        if term.startswith("~"):
            pat = re.compile(re.escape(term[1:]))
        else:
            pat = re.compile(r"(?<![a-z0-9äöüß])" + re.escape(term)
                             + r"(?:in|innen|[:*/]-?in(?:nen)?)?(?![a-z0-9äöüß])")
        _KW_CACHE[term] = pat
    return bool(pat.search(text))


def matches_keywords(job, kw):
    # 只看职位名。公司名不参与,否则 exclude 里的 "data" 会把 Databricks 之类整家公司误杀。
    hay = re.sub(r"\s+", " ", job.title.lower())
    inc, exc = kw.get("include") or [], kw.get("exclude") or []
    if any(kw_hit(e, hay) for e in exc):
        return False
    if not inc:
        return True
    return any(kw_hit(i, hay) for i in inc)


def matches_location(job, loc):
    text = f"{job.location}".lower()
    if not text.strip():
        return True  # 位置信息缺失时不误杀,交给关键词过滤
    if any(kw_hit(t, text) for t in loc.get("terms", [])):
        return True
    if loc.get("allow_remote") and re.search(r"remote|home\s?office|homeoffice|ortsunabh|mobiles arbeiten", text):
        rt = loc.get("remote_terms") or []
        if not rt or any(kw_hit(t, text) for t in rt):
            return True
    for p in loc.get("plz_prefixes", []):
        if re.search(rf"\b{p}\d{{3}}\b", text):
            return True
    return False


# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# 来源 1a:arbeitnow.com 公开 API(GitHub 服务器可达,含真实创建时间;德国全境,靠地点过滤留下慕尼黑)
# ----------------------------------------------------------------------------

def fetch_arbeitnow(cfg):
    ac = cfg.get("arbeitnow") or {}
    if not ac.get("enabled", True):
        return []
    out = []
    for page in range(1, int(ac.get("pages", 8)) + 1):
        try:
            r = session.get("https://www.arbeitnow.com/api/job-board-api",
                            params={"page": page}, timeout=25)
            if r.status_code != 200:
                print(f"  [arbeitnow] HTTP {r.status_code}")
                break
            items = r.json().get("data") or []
        except Exception as e:
            print(f"  [arbeitnow] 请求失败: {e}")
            break
        for j in items:
            ts = j.get("created_at")
            posted = ""
            if isinstance(ts, (int, float)):
                posted = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
            loc = j.get("location") or ""
            if j.get("remote"):
                loc += " remote deutschland"   # arbeitnow 是德国站,远程岗默认德国境内
            out.append(Job(
                uid=f"an:{j.get('slug')}",
                source="Arbeitnow",
                company=j.get("company_name") or "—",
                title=j.get("title") or "—",
                location=loc,
                url=j.get("url") or "",
                posted=posted,
            ))
        if not items:
            break
        time.sleep(0.3)
    return out


# ----------------------------------------------------------------------------
# 来源 1b:联邦劳动局官方接口。封了 GitHub 的 IP,默认关闭;在自己电脑上跑时可在 config 里开。
# ----------------------------------------------------------------------------

AA_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs"
AA_KEY = "jobboerse-jobsuche"   # 官方公开的固定 key,不是私人凭证


def fetch_arbeitsagentur(cfg):
    ac = cfg.get("arbeitsagentur") or {}
    if not ac.get("enabled"):
        return []
    out, seen = [], set()
    for query in ac.get("queries") or [""]:
        for page in range(1, 11):
            params = {"wo": ac.get("city", "München"), "umkreis": ac.get("radius_km", 25),
                      "veroeffentlichtseit": ac.get("published_within_days", 2),
                      "size": 100, "page": page, "angebotsart": 1}
            if query:
                params["was"] = query
            try:
                r = session.get(AA_URL, params=params, headers={"X-API-Key": AA_KEY}, timeout=25)
                if r.status_code != 200:
                    print(f"  [劳动局] HTTP {r.status_code}")
                    break
                data = r.json()
            except Exception as e:
                print(f"  [劳动局] 请求失败: {e}")
                break
            items = data.get("stellenangebote") or []
            for it in items:
                refnr = it.get("refnr") or it.get("hashId") or ""
                if not refnr or refnr in seen:
                    continue
                seen.add(refnr)
                ort = it.get("arbeitsort") or {}
                loc = " ".join(str(x) for x in [ort.get("plz"), ort.get("ort"), ort.get("region")] if x)
                out.append(Job(
                    uid=f"aa:{refnr}", source="Arbeitsagentur",
                    company=it.get("arbeitgeber") or "—",
                    title=it.get("titel") or it.get("beruf") or "—",
                    location=loc,
                    url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{quote(refnr, safe='')}",
                    posted=it.get("aktuelleVeroeffentlichungsdatum") or it.get("eintrittsdatum") or "",
                ))
            if len(items) < 100:
                break
            time.sleep(0.4)
    return out


# ----------------------------------------------------------------------------
# 来源 2:公司自己的 ATS 接口(比任何聚合平台都早)
# ----------------------------------------------------------------------------

def _get(url, **kw):
    """带一次重试:429 / 5xx 时等几秒再来一次(Personio 连续请求容易 429)。"""
    kw.setdefault("timeout", 20)
    r = session.get(url, **kw)
    if r.status_code == 429 or r.status_code >= 500:
        wait = 6
        try:
            wait = min(int(r.headers.get("Retry-After", wait)), 20)
        except ValueError:
            pass
        time.sleep(wait)
        r = session.get(url, **kw)
    return r


def ats_personio(slug, name):
    import xml.etree.ElementTree as ET
    r = _get(f"https://{slug}.jobs.personio.de/xml")
    r.raise_for_status()
    root = ET.fromstring(r.content)
    jobs = []
    for p in root.iter("position"):
        def t(tag):
            el = p.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        jid = t("id")
        if not jid:
            continue
        jobs.append(Job(
            uid=f"personio:{slug}:{jid}",
            source="Personio",
            company=name,
            title=t("name"),
            location=" ".join(x for x in [t("office"), t("department")] if x),
            url=f"https://{slug}.jobs.personio.de/job/{jid}",
            posted=t("createdAt"),
        ))
    return jobs


def ats_greenhouse(slug, name):
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    r.raise_for_status()
    return [Job(
        uid=f"gh:{slug}:{j['id']}",
        source="Greenhouse",
        company=name,
        title=j.get("title", ""),
        location=(j.get("location") or {}).get("name", ""),
        url=j.get("absolute_url", ""),
        posted=j.get("updated_at", ""),
    ) for j in r.json().get("jobs", [])]


def ats_lever(slug, name):
    r = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    r.raise_for_status()
    jobs = []
    for j in r.json():
        cat = j.get("categories") or {}
        ts = j.get("createdAt")
        jobs.append(Job(
            uid=f"lever:{slug}:{j.get('id')}",
            source="Lever",
            company=name,
            title=j.get("text", ""),
            location=cat.get("location", "") or "",
            url=j.get("hostedUrl", ""),
            posted=datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat() if ts else "",
        ))
    return jobs


def ats_smartrecruiters(slug, name):
    jobs, offset = [], 0
    while offset < 400:
        r = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                 params={"limit": 100, "offset": offset})
        r.raise_for_status()
        data = r.json()
        items = data.get("content") or []
        for j in items:
            loc = j.get("location") or {}
            jobs.append(Job(
                uid=f"sr:{slug}:{j.get('id')}",
                source="SmartRecruiters",
                company=name,
                title=j.get("name", ""),
                location=" ".join(str(x) for x in [loc.get("city"), loc.get("country")] if x),
                url=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                posted=(j.get("releasedDate") or "")[:10],
            ))
        if len(items) < 100:
            break
        offset += 100
    return jobs


def ats_recruitee(slug, name):
    r = _get(f"https://{slug}.recruitee.com/api/offers/")
    r.raise_for_status()
    return [Job(
        uid=f"rc:{slug}:{j.get('id')}",
        source="Recruitee",
        company=name,
        title=j.get("title", ""),
        location=" ".join(str(x) for x in [j.get("city"), j.get("country")] if x),
        url=j.get("careers_url") or j.get("careers_apply_url", ""),
        posted=(j.get("published_at") or "")[:10],
    ) for j in r.json().get("offers", [])]


def ats_ashby(slug, name):
    r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    r.raise_for_status()
    return [Job(
        uid=f"ashby:{slug}:{j.get('id')}",
        source="Ashby",
        company=name,
        title=j.get("title", ""),
        location=j.get("location", "") or "",
        url=j.get("jobUrl", ""),
        posted=(j.get("publishedAt") or "")[:10],
    ) for j in r.json().get("jobs", [])]


def ats_workable(slug, name):
    r = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    r.raise_for_status()
    return [Job(
        uid=f"wk:{slug}:{j.get('shortcode')}",
        source="Workable",
        company=name,
        title=j.get("title", ""),
        location=" ".join(str(x) for x in [j.get("city"), j.get("country")] if x),
        url=j.get("url") or j.get("application_url", ""),
        posted=(j.get("published_on") or "")[:10],
    ) for j in r.json().get("jobs", [])]


def ats_join(slug, name):
    r = _get(f"https://api.join.com/api/v1/companies/{slug}/jobs")
    r.raise_for_status()
    data = r.json()
    items = data if isinstance(data, list) else data.get("data") or data.get("jobs") or []
    return [Job(
        uid=f"join:{slug}:{j.get('id')}",
        source="JOIN",
        company=name,
        title=j.get("title", ""),
        location=str(j.get("location") or j.get("city") or ""),
        url=j.get("url", ""),
        posted=(str(j.get("publishedAt") or ""))[:10],
    ) for j in items]



def ats_successfactors(cfg_entry, name):
    """SAP SuccessFactors 的求职门户(HTML)。解析职位列表页。"""
    base = cfg_entry["url"].rstrip("/")
    jobs, seen = [], set()
    for start in (0, 25, 50, 75):
        url = f"{base}/search/?q=&locationsearch={quote(cfg_entry.get('location','München'))}&startrow={start}"
        try:
            r = session.get(url, timeout=25)
            if r.status_code != 200:
                break
            page = r.text
        except Exception:
            break
        blocks = re.findall(
            r'href="(/job/[^"]+)"[^>]*>\s*([^<]{4,120}?)\s*</a>(.{0,900}?)(?=href="/job/|</tbody>)',
            page, re.S)
        if not blocks:
            break
        n_before = len(jobs)
        for href, title, tail in blocks:
            jid = href.rstrip("/").split("/")[-1]
            if jid in seen:
                continue
            seen.add(jid)
            loc = ""
            m = re.search(r'jobLocation[^>]*>\s*([^<]{2,60})', tail)
            if m:
                loc = m.group(1).strip()
            jobs.append(Job(
                uid=f"sf:{name}:{jid}",
                source="SuccessFactors",
                company=name,
                title=_html_unescape(title),
                location=_html_unescape(loc) or cfg_entry.get("location", ""),
                url=base.split("/search")[0] + href,
                posted="",
            ))
        if len(jobs) == n_before:
            break
        time.sleep(0.4)
    return jobs


def ats_workday(cfg_entry, name):
    """Workday 需要 POST,且每家公司的 tenant/site 不同。"""
    url = cfg_entry["url"]          # 例:https://x.wd3.myworkdayjobs.com/wday/cxs/x/Careers/jobs
    base = url.split("/wday/")[0]
    jobs, offset = [], 0
    while offset < 400:
        r = session.post(url, json={"appliedFacets": {}, "limit": 20,
                                    "offset": offset,
                                    "searchText": cfg_entry.get("search", "")},
                         headers={"Content-Type": "application/json",
                                  "Accept": "application/json"},
                         timeout=25)
        r.raise_for_status()
        data = r.json()
        items = data.get("jobPostings") or []
        for j in items:
            path = j.get("externalPath", "")
            jobs.append(Job(
                uid=f"wd:{name}:{path}",
                source="Workday",
                company=name,
                title=j.get("title", ""),
                location=j.get("locationsText", "") or "",
                url=base + path,
                posted=j.get("postedOn", ""),
            ))
        if len(items) < 20:
            break
        offset += 20
    return jobs




def ats_hr4you(cfg_entry, name):
    """
    HR4YOU 没有列表接口,职位页是 generator.php?id=N。

    为了不给对方服务器造成不必要的负担:首次全量扫一遍并记住命中的 ID,
    之后每天只做两件事——复查已知 ID 是否还在(职位下架就消失),
    再在已知最大 ID 往上探一个小窗口(新职位的 ID 总是递增的)。
    请求量从每天 2000 次降到几十次。
    """
    import concurrent.futures as cf
    import json as _json

    host = cfg_entry["url"].rstrip("/")
    state_file = BASE / f"hr4you_{re.sub(r'[^a-z0-9]+', '', name.lower())}.json"
    known = []
    if state_file.exists():
        try:
            known = _json.loads(state_file.read_text())
        except Exception:
            known = []

    if known:
        window = int(cfg_entry.get("scan_ahead", 40))
        hi = max(known)
        ids = sorted(set(known) | set(range(hi + 1, hi + window + 1)))
        mode = f"增量({len(known)}个已知 + 前探{window})"
    else:
        ids = list(range(int(cfg_entry.get("id_from", 1000)),
                         int(cfg_entry.get("id_to", 3000)) + 1))
        mode = f"首次全量扫描({len(ids)})"
    print(f"  [HR4YOU:{name}] {mode},共 {len(ids)} 次请求")

    def probe(i):
        try:
            r = session.get(f"{host}/generator.php?id={i}&changelanguage=de", timeout=12)
        except Exception:
            return None
        if r.status_code != 200 or len(r.content) < 3000:
            return None
        r.encoding = "iso-8859-1"
        page = r.text
        m = re.search(r"<title>(.*?)</title>", page, re.S)
        if not m:
            return None
        title = _html_unescape(m.group(1))
        if len(title) < 6 or "fehler" in title.lower() or "error" in title.lower():
            return None
        loc = ""
        mk = re.search(r'Keywords"\s+CONTENT="[^"]*?,\s*[^,"]*,\s*([^,"]{3,40}?)\s*,', page, re.I)
        if mk:
            loc = _html_unescape(mk.group(1))
        return i, Job(uid=f"hr4you:{name}:{i}", source="HR4YOU", company=name,
                      title=title, location=loc or "München",
                      url=f"{host}/generator.php?id={i}&changelanguage=de", posted="")

    jobs, hits = [], []
    # 并发降到 4,并分批加间隔,避免给对方造成突发压力
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for n, res in enumerate(ex.map(probe, ids)):
            if res:
                hits.append(res[0])
                jobs.append(res[1])
            if n and n % 200 == 0:
                time.sleep(1.0)
    if hits:
        try:
            state_file.write_text(_json.dumps(sorted(hits)))
        except Exception:
            pass
    return jobs




def ats_phenom(cfg_entry, name):
    """
    Phenom People 平台(AMD / Keysight / 多数大型制造企业用)。
    /api/jobs 是公开 JSON,支持 location 过滤,返回精确到分钟的发布时间。
    """
    host = cfg_entry["url"].rstrip("/")
    loc = cfg_entry.get("location", "Singapore")
    jobs, page = [], 1
    while page <= 10:
        try:
            r = session.get(host + "/api/jobs",
                            params={"location": loc, "limit": 100, "page": page},
                            headers={"Accept": "application/json"}, timeout=25)
            if r.status_code != 200:
                break
            items = (r.json() or {}).get("jobs") or []
        except Exception as e:
            print(f"  [Phenom:{name}] {type(e).__name__}")
            break
        if not items:
            break
        for it in items:
            d = it.get("data") or it
            jid = d.get("slug") or d.get("req_id")
            if not jid:
                continue
            city = d.get("city") or d.get("location_name") or d.get("state") or ""
            country = d.get("country") or ""
            jobs.append(Job(
                uid=f"phenom:{name}:{jid}",
                source="Phenom",
                company=name,
                title=d.get("title") or "-",
                location=" ".join(str(x) for x in [city, country] if x) or loc,
                url=d.get("apply_url") or f"{host}/careers/job/{jid}",
                posted=str(d.get("posted_date") or d.get("create_date") or "")[:10],
            ))
        if len(items) < 100:
            break
        page += 1
        time.sleep(0.3)
    return jobs


def ats_softgarden(slug, name):
    """softgarden:德国中小企业常用。"""
    r = _get(f"https://{slug}.softgarden.io/api/rest/frontend/v3/job-postings",
             params={"limit": 200})
    r.raise_for_status()
    d = r.json()
    items = d.get("content") or d.get("jobPostings") or d.get("data") or []
    jobs = []
    for j in items:
        jid = j.get("id") or j.get("jobPostingId")
        if not jid:
            continue
        loc = j.get("jobLocation") or j.get("location") or {}
        if isinstance(loc, dict):
            loc = " ".join(str(x) for x in [loc.get("postalCode"), loc.get("city"),
                                            loc.get("name")] if x)
        jobs.append(Job(
            uid=f"sg:{slug}:{jid}",
            source="softgarden",
            company=name,
            title=j.get("jobTitle") or j.get("name") or j.get("title", ""),
            location=str(loc),
            url=j.get("jobPostingUrl") or j.get("url")
                or f"https://{slug}.softgarden.io/job/{jid}",
            posted=str(j.get("onlineDate") or j.get("createdDate") or "")[:10],
        ))
    return jobs


def ats_teamtailor(slug, name):
    r = _get(f"https://{slug}.teamtailor.com/jobs.json")
    r.raise_for_status()
    d = r.json()
    items = d if isinstance(d, list) else (d.get("jobs") or d.get("data") or [])
    return [Job(
        uid=f"tt:{slug}:{j.get('id')}",
        source="Teamtailor",
        company=name,
        title=j.get("title", ""),
        location=str(j.get("location") or ""),
        url=j.get("url", ""),
        posted=str(j.get("created_at") or "")[:10],
    ) for j in items]


ATS_FETCHERS = {
    "personio": ats_personio,
    "greenhouse": ats_greenhouse,
    "lever": ats_lever,
    "smartrecruiters": ats_smartrecruiters,
    "recruitee": ats_recruitee,
    "ashby": ats_ashby,
    "workable": ats_workable,
    "join": ats_join,
    "softgarden": ats_softgarden,
    "teamtailor": ats_teamtailor,
}


def fetch_companies():
    conf = load_yaml(COMPANIES_PATH, default={"companies": []})
    out = []
    for c in conf.get("companies") or []:
        _n_before = 0
        name = c.get("name") or c.get("slug", "?")
        ats = (c.get("ats") or "").lower()
        try:
            _n_before = len(out)
            if ats == "workday":
                out += ats_workday(c, name)
            elif ats == "successfactors":
                out += ats_successfactors(c, name)
            elif ats == "bmw":
                out += ats_bmw(c, name)
            elif ats == "phenom":
                out += ats_phenom(c, name)
            elif ats == "hr4you":
                out += ats_hr4you(c, name)
            elif ats in ATS_FETCHERS:
                out += ATS_FETCHERS[ats](c["slug"], name)
            else:
                print(f"  [跳过] {name}: 未知 ats '{ats}'")
                continue
            for _j in out[_n_before:]:
                _j.extra["cats"] = classify(_j, c.get("tags"))
            print(f"  [OK] {name} ({ats})")
        except Exception as e:
            print(f"  [失败] {name} ({ats}): {type(e).__name__} {e}")
        time.sleep(1.5 if ats == "personio" else 0.3)
    return out


# ----------------------------------------------------------------------------
# ATS 自动探测:给公司名,猜它用的哪套系统
# ----------------------------------------------------------------------------

PROBES = [
    ("personio",       lambda s: f"https://{s}.jobs.personio.de/xml"),
    ("greenhouse",     lambda s: f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs"),
    ("lever",          lambda s: f"https://api.lever.co/v0/postings/{s}?mode=json"),
    ("smartrecruiters", lambda s: f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1"),
    ("recruitee",      lambda s: f"https://{s}.recruitee.com/api/offers/"),
    ("ashby",          lambda s: f"https://api.ashbyhq.com/posting-api/job-board/{s}"),
    ("workable",       lambda s: f"https://apply.workable.com/api/v1/widget/accounts/{s}?details=true"),
]


def cmd_discover(slugs):
    print("探测中(slug 通常是公司名小写去空格,例:BMW Group -> bmwgroup)\n")
    found = []
    for slug in slugs:
        hits = []
        for ats, mk in PROBES:
            url = mk(slug)
            try:
                r = _get(url, timeout=10)
                if r.status_code != 200 or len(r.content) < 40:
                    continue
                # 粗略数一下有多少职位,避免把空壳页面当成命中
                n = len(re.findall(r'"id"\s*:|<position>', r.text))
                hits.append((ats, n))
            except Exception:
                pass
        if hits:
            for ats, n in hits:
                print(f"  ✓ {slug:<24} {ats:<16} 约 {n} 个职位")
                found.append({"name": slug, "ats": ats, "slug": slug})
        else:
            print(f"  ✗ {slug:<24} 没探到 — 手动打开它的招聘页,看 URL 跳到哪个域名")
    if found:
        print("\n把下面这段贴进 companies.yaml 的 companies: 下面(注意缩进):\n")
        print(yaml.safe_dump(found, allow_unicode=True, sort_keys=False))


# ----------------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------------

CSS = """
:root{--bg:#faf9f7;--card:#fff;--tx:#1a1a18;--mut:#6b6862;--line:#e6e3dd;--acc:#b8562f}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px;background:var(--bg);color:var(--tx);
     font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",sans-serif}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:23px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:13px;margin-bottom:26px}
.grp{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);
     margin:28px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.j{background:var(--card);border:1px solid var(--line);border-radius:9px;
   padding:13px 15px;margin-bottom:8px}
.j a{color:var(--tx);text-decoration:none;font-weight:600}
.j a:hover{color:var(--acc)}
.meta{color:var(--mut);font-size:12.5px;margin-top:4px}
.chips{margin:4px 0 20px;line-height:2.1}
.chips .cat{margin:0 6px 0 0}
.cat{display:inline-block;color:#fff;border-radius:4px;padding:2px 7px;font-size:11px;
     font-weight:600;margin-left:6px;letter-spacing:.02em}
.tag{display:inline-block;background:#f0ede7;border-radius:4px;padding:1px 6px;
     font-size:11px;color:var(--mut);margin-left:6px}
.tag.igm{background:#2f7d32;color:#fff;font-weight:700;cursor:help;letter-spacing:.04em;
         padding:2px 7px}
.empty{color:var(--mut);padding:40px 0;text-align:center}
.sec2{font-size:14px;font-weight:600;margin:26px 0 6px;color:var(--mut);
      padding-bottom:6px;border-bottom:1px solid var(--line)}
.hint{font-weight:400;font-size:12px;margin-left:6px}
details summary{cursor:pointer;color:var(--acc);font-size:13px;margin-bottom:10px}
.sec{font-size:15px;font-weight:700;margin:30px 0 6px;padding-bottom:8px;
     border-bottom:2px solid var(--tx)}
.note{background:#fff6e8;border:1px solid #f0dcc0;border-radius:8px;padding:10px 13px;
      font-size:13px;color:#6b5533;margin-bottom:18px}

.bar{margin:6px 0 4px}
.bar .row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:7px}
.bar .lbl{font-size:11px;color:var(--mut);letter-spacing:.06em;min-width:2.4em}
.bar .cat{margin:0;cursor:pointer;opacity:.9;transition:opacity .15s,box-shadow .15s}
.bar .cat:hover{opacity:1}
.cat.on{opacity:1!important;box-shadow:0 0 0 2px var(--bg),0 0 0 4px var(--tx)}
.filtering .bar .cat:not(.on){opacity:.4}
.j .cat{cursor:pointer}
.j .cat:hover{filter:brightness(1.12)}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0 14px}
.tools input{flex:1;min-width:170px;padding:7px 10px;border:1px solid var(--line);border-radius:7px;
             font:inherit;font-size:13px;background:var(--card);color:var(--tx)}
.tools .btn{background:var(--card);border:1px solid var(--line);border-radius:7px;padding:6px 10px;
            font:inherit;font-size:12px;cursor:pointer;color:var(--tx)}
.tools .btn:hover{border-color:var(--tx)}
.tools .btn.on{background:var(--tx);color:#fff;border-color:var(--tx)}
.tools .hint{flex-basis:100%;margin:0;color:var(--mut)}
.j.hide{display:none}
.list .empty{display:none;padding:22px 0}
.list.none .empty{display:block}
.n{font-variant-numeric:tabular-nums}
"""



# ----------------------------------------------------------------------------
# 岗位分类:每条职位打三类标签,可叠加
#   1. 方向(看职位名):云/DevOps/平台 · AI/ML · 软件开发
#   2. 初级友好(看职位名):Junior / Associate / Graduate / Engineer I 之类
#   3. 行业(看公司名):科技大厂 / 互联网平台 / 金融科技 / 银行 / 政府 / 咨询外包 / 中介 / 中资 / 半导体
# ----------------------------------------------------------------------------

ROLE_RULES = [
    ("云/DevOps/平台", [
        "cloud", "devops", "dev ops", "devsecops", "sre", "site reliability", "reliability engineer",
        "platform engineer", "platform engineering", "plattform", "infrastructure", "infrastruktur", "infra",
        "kubernetes", "k8s", "aws", "azure", "gcp", "release engineer", "build engineer", "ci/cd", "cicd",
        "systems engineer", "system engineer", "systemingenieur", "cloud engineer", "cloud-engineer",
        "~cloudarchit", "linux", "openshift", "terraform", "ansible",
    ]),
    ("AI/ML", [
        "ai", "a.i.", "ai/ml", "machine learning", "ml", "mlops", "llm", "genai", "gen ai",
        "generative ai", "deep learning", "nlp", "computer vision", "agentic", "ki", "künstliche intelligenz",
        "~ki-", "maschinelles lernen", "data science",   # data science 已被 exclude 挡掉,留着只为标签完整
    ]),
    ("软件开发", [
        "software", "developer", "development engineer", "programmer", "backend", "back-end", "back end",
        "frontend", "front-end", "front end", "full stack", "full-stack", "fullstack", "web",
        "mobile", "android", "ios", "java", "python", "golang", ".net", "asp.net", "dotnet", "c#", "c++", "node.js",
        "react", "sde", "technology analyst", "technology associate", "application engineer",
        "~entwickl", "~programmier", "informatiker", "fachinformatiker", "anwendungsentwickler",
    ]),
]
ROLE_NAMES = {r[0] for r in ROLE_RULES}

JUNIOR_TERMS = [
    "junior", "jr", "jr.", "associate", "graduate", "grad", "new grad", "entry level", "entry-level",
    "early career", "early careers", "young professional", "young professionals", "young talent",
    "engineer i", "engineer 1", "developer i", "developer 1", "level 1", "l1", "0-2 years", "1-2 years",
    "1-3 years", "1 year", "2 years",
    # 德语
    "einsteiger", "berufseinsteiger", "berufseinstieg", "einstieg", "absolvent", "hochschulabsolvent",
    "nachwuchs", "trainee", "traineeprogramm", "erste berufserfahrung", "1-2 jahre", "0-2 jahre",
    "1 jahr", "2 jahre",
]
JUNIOR_TAG = "初级友好"
EN_TAG = "英文职位名"
_GERMAN_MARKERS = re.compile(
    r"[äöüß]|\b(für|und|mit|im|zur|zum|der|die|das|oder|bei|von|als|schwerpunkt|bereich|standort|"
    r"raum|region|festanstellung|vollzeit|teilzeit|unbefristet|befristet|gesucht|werden)\b|"
    r"entwickl|ingenieur|informatik|mitarbeit|fachkraft|leiter|leitung|berater|kaufm|referent|"
    r"sachbearb|techniker|fachrichtung|verstärk|betreu|spezialist|abteilung|projekt\b")


def is_english_title(title):
    t = re.sub(r"\((m|w|d|f|x|gn|all genders|h|div)[^)]*\)", " ", title.lower())   # (m/w/d) 中英都用,不算德语标志
    return not _GERMAN_MARKERS.search(t)


CATEGORY_RULES = [
    ("科技大厂", [
        "google", "apple", "microsoft", "amazon", "aws", "amazon web services", "meta", "ibm", "intel",
        "nvidia", "salesforce", "adobe", "oracle", "sap", "cisco", "qualcomm", "amd", "micron", "arm",
        "huawei", "samsung", "netflix", "uber", "linkedin", "servicenow", "workday", "snowflake",
        "databricks", "mongodb", "elastic", "hashicorp", "gitlab", "github", "palantir", "cloudflare",
        "datadog", "twilio", "okta", "atlassian", "zendesk", "splunk", "vmware", "broadcom", "red hat",
        "dell", "hewlett packard", "hpe", "nokia", "ericsson", "openai", "anthropic",
        "mistral", "aleph alpha", "black forest labs", "dynatrace", "teamviewer", 
    ]),
    ("慕尼黑科技/创业", [
        "check24", "scout24", "autoscout24", "immobilienscout", "flix", "flixbus", "flixmobility", "sixt",
        "freeletics", "helsing", "isar aerospace", "konux", "proglove", "brainlab", "agile robots",
        "quantum systems", "lilium", "tado", "idnow", "finn", "scalable capital", "westwing", "contabo",
        "staffbase", "stylight", "egym", "holidaycheck", "jochen schweizer", "payback", "interhyp",
        "tacto", "ryte", "usercentrics", "personio", "celonis", "nfon",
        "zooplus", "smartlane", "twaice", "marvel fusion",
        "planqc", "iqm", "kinexon", "roboception", "franka", "magazino", "navvis", "blickfeld",
        "cluno", "tacto", "sono motors",
        "orbem", "bmw techoffice", "hubert burda", "burda", "prosieben", "prosiebensat", "joyn", "sky deutschland", "sky",
        "telefonica", "telefónica", "o2", "m-net", "giesecke", "giesecke+devrient", "g+d",
        "veridos", "build38", "myra", 
    ]),
    ("汽车与出行", [
        "bmw", "bayerische motoren werke", "audi", "man truck", "man energy", "mercedes", "daimler",
        "volkswagen", "porsche", "continental", "aumovio", "webasto", "knorr-bremse", "knorr bremse",
        "bosch", "zf", "brose", "mahle", "hella", "schaeffler", "vitesco", "dräxlmaier", "draexlmaier",
        "leoni", "valeo", "magna", "aptiv", "harman", "cariad", "traton", "krones", "kuka", "denso", "byd", "nio", "geely", "lynk", "xpeng", "catl", "horizon robotics", "momenta",
        "sono", 
    ]),
    ("半导体与硬件", [
        "infineon", "intel", "nvidia", "amd", "qualcomm", "micron", "texas instruments", "renesas", "nxp",
        "ams osram", "osram", "siltronic", "rohde & schwarz", "rohde", "apple", "arm", "mediatek",
        "marvell", "dialog semiconductor", "st micro", "stmicro", "wolfspeed", "semikron", "analog devices",
        "microchip", "onsemi", "skyworks", "lam research", "applied materials", "asml", "carl zeiss",
        "zeiss", "jenoptik", "trumpf", "semiconductor", "halbleiter", "wafer", "fpga", "asic",
    ]),
    ("航空与国防", [
        "airbus", "mtu aero", "mtu", "isar aerospace", "helsing", "quantum systems", "hensoldt", "ohb",
        "esg elektroniksystem", "iabg", "diehl", "rheinmetall", "knds", "krauss-maffei", "kmw",
        "mbda", "dlr", "deutsches zentrum für luft", "safran", "collins aerospace", "liebherr aerospace",
        "premium aerotec", "lufthansa technik", "lufthansa systems", "rolls-royce", "arianegroup",
        "the exploration company", "reflex aerospace", "rocket factory", "hyimpulse", "aerospace", "luftfahrt", "raumfahrt", "verteidigung", "defence", "defense", "wehrtechnik",
    ]),
    ("金融与保险", [
        "allianz", "munich re", "münchener rück", "ergo", "hypovereinsbank", "unicredit", "bayernlb",
        "deutsche bank", "commerzbank", "sparkasse", "genossenschaft", "dz bank", "kfw", "swiss re",
        "generali", "arag", "versicherungskammer", "lv 1871", "lv1871", "wwk", "nürnberger", "signal iduna",
        "debeka", "huk", "axa", "zurich", "hannover re", "talanx", "check24", "scalable capital", "interhyp",
        "consorsbank", "comdirect", "n26", "trade republic", "finanzchef24", "fidor", "payback", "bank", "versicherung", "fund", "asset management", "capital", "finanz", "kapital",
        
    ]),
    ("咨询与IT服务", [
        "accenture", "capgemini", "tcs", "tata consultancy", "infosys", "wipro", "cognizant", "hcl",
        "tech mahindra", "ntt data", "nttdata", "msg", "msg systems", "cancom", "bechtle", "allgeier",
        "adesso", "materna", "tng", "qaware", "iteratec", "maibornwolff", "maiborn wolff", "netlight",
        "reply", "mhp", "sopra steria", "atos", "eviden", "fujitsu", "computacenter", "dxc", "kyndryl",
        "deloitte", "pwc", "pricewaterhouse", "ey", "ernst & young", "kpmg", "bearingpoint", "zühlke",
        "zuhlke", "thoughtworks", "steadforce", "cocus", "comsysto", "codecentric", "senacor", "diva-e",
        "valtech", "avanade", "sap consulting", "itelligence", "nagarro", "globant", "epam", "endava",
        "cgi", "unisys", "dedalus", "compugroup", "datev", "software ag", "arvato", "bertelsmann",
        "t-systems", "telekom", "deutsche telekom", "ibm consulting", "mckinsey", "bcg", "boston consulting",
        "bain", "roland berger", "oliver wyman", "kearney", "strategy&", "horváth", "porsche consulting",
        "p3", "umlaut", "consulting", "beratung", "it-service", "it service", "systemhaus", 
    ]),
    ("招聘中介", [
        # 招聘中介 + 工程服务/派遣(Arbeitnehmerüberlassung),都是合同岗,真实雇主另有其人
        "hays", "robert half", "michael page", "page personnel", "randstad", "adecco", "manpower",
        "ferchau", "brunel", "amadeus fire", "amadeus-fire", "alten", "akkodis", "modis", "gulp", "solcom",
        "ratbacher", "univativ", "franklin fitch", "computer futures", "progressive", "harvey nash",
        "nash direct", "experis", "dis ag", "piening", "orizon", "tempton", "peak one", "avantgarde experts",
        "yer", "verovis", "etengo", "bertrandt", "edag", "avl", "expleo", "akka", "segula", "altran",
        "capgemini engineering", "arrk", "in-tech", "intech", "hofmann personal", "persona service",
        "trenkwalder", "tuja", "zeitkraft", "iperdi", "argo aviation", "aerotek", "sthree", "real staffing",
        "huxley", "spring professional", "lhh", "badenoch", "heidrick", "korn ferry", "mercuri urval",
        "kienbaum", "personalberatung", "personalvermittlung", "personaldienstleist", "personalservice",
        "zeitarbeit", "arbeitnehmerüberlassung", "recruiting", "recruitment", "headhunt", "staffing",
        "talent", "human resources", "hr consult", "engineering services", "engineering-dienstleist",
        "workwise", "join.com", "campusjäger",
        "campusjaeger", "get in", "get-in-it", "instaffo", "honeypot", "talent.io", "4scotty", "heyjobs",
        "hokify", "jobvalley", "studitemps", "zenjob", "coople", "gigwerk", "expertlead", "9am",
    ]),
    ("中资出海", [
        "huawei", "zte", "xiaomi", "oppo", "vivo", "honor", "byd", "nio", "xpeng", "geely", "lynk", "catl",
        "horizon robotics", "black sesame", "hesai", "robosense", "momenta", "dji", "anker", "tiktok",
        "bytedance", "alibaba", "tencent", "baidu", "lenovo", "haier", "midea", "hisense", "tcl", "boe",
        "gotion", "svolt", "eve energy", "sungrow", "huaqin", "unisoc", "spreadtrum", "sensetime",
        "cambricon", "shein", "temu", "pdd", "trip.com", "ctrip", "china", "chinese", "sino",
    ]),
    ("公共/科研", [
        "tum", "technische universität münchen", "technical university of munich", "lmu",
        "ludwig-maximilians", "hochschule münchen", "fraunhofer", "max-planck", "max planck",
        "helmholtz", "leibniz", "bundeswehr", "universität der bundeswehr", "stadt münchen",
        "landeshauptstadt münchen", "swm", "stadtwerke münchen", "mvg", "mvv", "freistaat bayern",
        "bayerisches", "landesamt", "bundesamt", "bundesministerium", "it-dienstleistungszentrum", "it@m",
        "byte", "bavarian agency", "digitalministerium", "klinikum", "universitätsklinikum", "rechts der isar",
        "lmu klinikum", "helmholtz munich", "european southern observatory", "eso", "ipp", "esa", "esoc",
        "universität", "university", "hochschule", 
        "european patent office", "epo", "europäisches patentamt", "patentamt",
    ]),
]

# 只在公司名里匹配的词(品牌名);这几个行业通名才允许在职位名里匹配
GENERIC_OK = {
    "semiconductor", "halbleiter", "wafer", "fpga", "asic", "aerospace", "luftfahrt", "raumfahrt",
    "verteidigung", "defence", "defense", "wehrtechnik",
}


def _tag_hits(text, needles):
    for n in needles:
        n = n.strip().lower()
        if len(n) < 2:
            continue
        if kw_hit(n, text):
            return True
    return False


def classify(job, company_tags=None):
    """方向 + 初级友好 看职位名;行业 优先用 companies.yaml 里的预设标签,否则按公司名判断。"""
    tags = list(company_tags or [])
    comp = re.sub(r"\s+", " ", job.company.lower())
    title = re.sub(r"\s+", " ", job.title.lower())

    # 1. 方向(可叠加;都没命中就归软件开发)
    roles = [r for r, needles in ROLE_RULES if _tag_hits(title, needles)]
    if not roles:
        roles = ["软件开发"]
    tags += [r for r in roles if r not in tags]

    # 2. 初级友好
    if _tag_hits(title, JUNIOR_TERMS) and JUNIOR_TAG not in tags:
        tags.append(JUNIOR_TAG)

    # 2b. 职位名是英文 → 英语工作环境概率高
    if is_english_title(job.title) and EN_TAG not in tags:
        tags.append(EN_TAG)

    # 3. 行业:公司名(品牌词);少数行业通名允许在职位名里匹配
    for cat, needles in CATEGORY_RULES:
        if cat in tags:
            continue
        for n in needles:
            n = n.strip().lower()
            if len(n) < 2:
                continue
            if kw_hit(n, comp) or (n in GENERIC_OK and kw_hit(n, title)):
                tags.append(cat)
                break
    return tags


TAG_COLORS = {
    # 方向
    "云/DevOps/平台": "#0369a1",
    "AI/ML": "#7c3aed",
    "软件开发": "#334155",
    # 经验 / 语言
    "初级友好": "#15803d",
    "英文职位名": "#0e7490",
    # 行业
    "科技大厂": "#1d4ed8",
    "慕尼黑科技/创业": "#0f766e",
    "汽车与出行": "#b45309",
    "半导体与硬件": "#4338ca",
    "航空与国防": "#6d28d9",
    "金融与保险": "#a16207",
    "咨询与IT服务": "#6b7280",
    "招聘中介": "#9ca3af",
    "中资出海": "#9333ea",
    "公共/科研": "#be123c",
}


def _grp(tag):
    if tag in ROLE_NAMES:
        return "role"
    if tag == JUNIOR_TAG:
        return "exp"
    if tag == EN_TAG:
        return "lang"
    return "ind"


def _tag_span(c, extra=""):
    return (f'<span class=cat data-tag="{html.escape(c)}" data-grp="{_grp(c)}" title="点击按此标签筛选" '
            f'style="background:{TAG_COLORS.get(c, "#6b7280")}">{html.escape(c)}{extra}</span>')


def _job_card(j):
    lbl = age_label(j)
    posted = f'<span class=tag>{html.escape(lbl)}</span>' if lbl else ""
    cats = j.extra.get("cats") or []
    igm = "".join(_tag_span(c) for c in cats)
    return (f'<div class=j data-tags="{html.escape("|".join(cats))}">'
            f'<a href="{html.escape(j.url)}" target=_blank rel=noopener>'
            f'{html.escape(j.title)}</a>{igm}{posted}'
            f'<div class=meta>{html.escape(j.company)} · '
            f'{html.escape(j.source)} · '
            f'{html.escape(j.location) or "地点未标注"}</div></div>')


FILTER_JS = r"""
(function(){
  var wrap=document.querySelector('.wrap');
  var chips=[].slice.call(document.querySelectorAll('.bar [data-tag]'));
  var q=document.getElementById('q'), na=document.getElementById('na'), clr=document.getElementById('clr');
  var sel={}, hideAgency=false;
  function grpOf(tag){var el=document.querySelector('[data-tag="'+tag.replace(/"/g,'\\"')+'"]');return el?el.getAttribute('data-grp'):'x';}
  function has(g,t){return sel[g]&&sel[g].indexOf(t)>=0;}
  function toggle(tag){var g=grpOf(tag);sel[g]=sel[g]||[];var i=sel[g].indexOf(tag);
    if(i>=0)sel[g].splice(i,1);else sel[g].push(tag);if(!sel[g].length)delete sel[g];apply();}
  function match(card){
    var t=(card.getAttribute('data-tags')||'').split('|');
    if(hideAgency&&t.indexOf('招聘中介')>=0)return false;
    for(var g in sel){var ok=false;for(var k=0;k<sel[g].length;k++){if(t.indexOf(sel[g][k])>=0){ok=true;break;}}if(!ok)return false;}
    var s=q.value.trim().toLowerCase();
    if(s&&card.textContent.toLowerCase().indexOf(s)<0)return false;
    return true;}
  function apply(){
    var active=Object.keys(sel).length>0, any=active||hideAgency||!!q.value.trim();
    wrap.className='wrap'+(active?' filtering':'');
    chips.forEach(function(b){b.className='cat'+(has(b.getAttribute('data-grp'),b.getAttribute('data-tag'))?' on':'');});
    na.className='btn'+(hideAgency?' on':'');
    var total=0;
    [].slice.call(document.querySelectorAll('.list')).forEach(function(l){
      var n=0;[].slice.call(l.querySelectorAll('.j')).forEach(function(c){
        var ok=match(c);c.className='j'+(ok?'':' hide');if(ok)n++;});
      l.className='list'+(n?'':' none');
      var h=document.querySelector('[data-for="'+l.id+'"]');
      if(h){h.textContent=any?(n+' / '+h.getAttribute('data-total')):h.getAttribute('data-total');}
      if(l.id==='l_fresh')total=n;
      var d=l.closest?l.closest('details'):null;if(d&&any&&n)d.open=true;});
    var p=[];var all=[];for(var g in sel)all=all.concat(sel[g]);
    if(all.length)p.push('t='+encodeURIComponent(all.join(',')));
    if(hideAgency)p.push('na=1');
    if(q.value.trim())p.push('q='+encodeURIComponent(q.value.trim()));
    try{history.replaceState(null,'',p.length?('#'+p.join('&')):location.pathname+location.search);}catch(e){}
  }
  document.addEventListener('click',function(e){
    var el=e.target;while(el&&el!==document&&!(el.getAttribute&&el.getAttribute('data-tag')))el=el.parentNode;
    if(el&&el!==document&&el.getAttribute('data-tag')){e.preventDefault();toggle(el.getAttribute('data-tag'));}});
  na.addEventListener('click',function(){hideAgency=!hideAgency;apply();});
  clr.addEventListener('click',function(){sel={};hideAgency=false;q.value='';apply();});
  q.addEventListener('input',apply);
  var h=location.hash.slice(1).split('&');
  h.forEach(function(kv){var i=kv.indexOf('=');if(i<0)return;var k=kv.slice(0,i),v=decodeURIComponent(kv.slice(i+1));
    if(k==='t')v.split(',').forEach(function(t){if(!t)return;var g=grpOf(t);sel[g]=sel[g]||[];if(sel[g].indexOf(t)<0)sel[g].push(t);});
    if(k==='na'&&v==='1')hideAgency=true;
    if(k==='q')q.value=v;});
  apply();
})();
"""


def render_html(day_groups, new_today, total_seen, first_run, cfg, fallback=None):
    """只显示 max_age_days 天内的职位;若为空,退回显示最新的若干条并说明。
    页面自带筛选:点标签(同组"或",跨组"且")、搜索框、隐藏中介;状态写在 URL # 里可收藏。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    disp = cfg.get("display") or {}
    maxage = int(disp.get("max_age_days", 5))
    hide = set(disp.get("hide_tags") or [])
    allj = [j for j in (fallback or []) if not (hide & set(j.extra.get("cats") or []))]

    fresh, older, junior_all, en_all = [], [], [], []
    for j in allj:
        n, _ = job_age_days(j)
        (fresh if (n is not None and n <= maxage) else older).append(j)
        cats = j.extra.get("cats") or []
        if JUNIOR_TAG in cats:
            junior_all.append(j)
        if EN_TAG in cats:
            en_all.append(j)

    def by_age(js):
        # 初级友好的排最前;然后直接雇主排在中介/外包前面;同组内按新旧
        def key(x):
            cats = x.extra.get("cats") or []
            n = job_age_days(x)[0]
            return (0 if JUNIOR_TAG in cats else 1, 1 if "招聘中介" in cats else 0, n if n is not None else 999)
        return sorted(js, key=key)

    def section(lid, title, js, hint="", cls="sec", limit=None, folded=False):
        js = by_age(js)
        shown = js[:limit] if limit else js
        hint_html = f"<span class=hint>{hint}</span>" if hint else ""
        out = [f'<div class={cls}>{title} · <span class=n data-for="{lid}" data-total="{len(js)}">{len(js)}</span> 条'
               f'{hint_html}</div>']
        if folded:
            out.append("<details><summary>展开查看</summary>")
        out.append(f'<div class=list id="{lid}">')
        out += [_job_card(j) for j in shown]
        if limit and len(js) > limit:
            out.append(f'<div class=note>只显示前 {limit} 条,其余 {len(js) - limit} 条略。</div>')
        out.append('<div class=empty>没有符合当前筛选的职位</div></div>')
        if folded:
            out.append("</details>")
        return out

    parts = ["<!doctype html><meta charset=utf-8>",
             '<meta name=viewport content="width=device-width,initial-scale=1">',
             "<title>慕尼黑 IT 新职位</title>", f"<style>{CSS}</style><div class=wrap>",
             "<h1>慕尼黑 · IT 新职位</h1>",
             "<div class=sub>Cloud · DevOps · Platform · AI · Software Engineer · 1 年经验友好,"
             "已过滤 Senior/Lead 及 Data 类岗位 · 英文职位单列</div>",
             f"<div class=sub>更新于 {ts} UTC · 最近 {maxage} 天 <b>{len(fresh)}</b> 条"
             f"· 库中累计 {total_seen} 条</div>"]

    # 标签条:三组(方向 / 经验 / 行业),数字是最近 maxage 天内的数量
    cnt = {}
    for j in fresh:
        for c in (j.extra.get("cats") or []):
            cnt[c] = cnt.get(c, 0) + 1
    groups = {"role": ("方向", []), "exp": ("经验", []), "lang": ("语言", []), "ind": ("行业", [])}
    for c, n in sorted(cnt.items(), key=lambda kv: -kv[1]):
        groups[_grp(c)][1].append(_tag_span(c, f" {n}"))
    bar = []
    for g in ("role", "exp", "lang", "ind"):
        label, items = groups[g]
        if items:
            bar.append(f'<div class=row><span class=lbl>{label}</span>{"".join(items)}</div>')
    parts.append(f'<div class=bar>{"".join(bar)}</div>')
    parts.append('<div class=tools>'
                 '<input id=q type=search placeholder="搜职位名 / 公司名…" autocomplete=off>'
                 '<button id=na class=btn type=button>隐藏中介/派遣/工程服务岗</button>'
                 '<button id=clr class=btn type=button>清除筛选</button>'
                 f'<span class=hint>点标签筛选:同组"或",跨组"且";数字是最近 {maxage} 天内的数量。筛选状态在网址里,可收藏。</span>'
                 '</div>')

    hidden_note = f"已隐藏 {'、'.join(sorted(hide))} 岗位(多限公民/PR);" if hide else ""
    parts.append(f'<div class=note>{hidden_note}'
                 '「招聘中介」含猎头、派遣和工程服务商(Hays / Ferchau / Brunel 等),都是合同岗,真实雇主另有其人;'
                 '「英文职位名」按职位名语言判断,英语工作环境概率高。数据源为 arbeitnow + 公司官网直连,'
                 'StepStone / LinkedIn 独家发布的岗位覆盖不到。</div>')

    if fresh:
        parts += section("l_fresh", f"最近 {maxage} 天内的职位", fresh)
    else:
        parts.append(f'<div class=note>最近 {maxage} 天没有新职位(周末常见)。下面列出最新的一批供参考。</div>')
        parts += section("l_fresh", "最新的一批", older[:25])

    if en_all:
        parts += section("l_en", "英文职位名", en_all,
                         hint="(职位名是英文,英语工作环境概率高,不限日期)", limit=200)
    if junior_all:
        parts += section("l_jr", "初级 / 毕业生友好岗位", junior_all,
                         hint="(职位名含 Junior / Associate / Graduate / Engineer I 等,不限日期)", limit=200)
    if fresh and older:
        parts += section("l_old", "更早的职位", older, hint=f"(超过 {maxage} 天,默认折叠)",
                         cls="sec2", limit=400, folded=True)

    parts.append(f"</div><script>{FILTER_JS}</script>")
    doc = "".join(parts)
    OUT_HTML.write_text(doc, encoding="utf-8")
    DOCS_HTML.parent.mkdir(exist_ok=True)
    DOCS_HTML.write_text(doc, encoding="utf-8")
    (DOCS_HTML.parent / ".nojekyll").touch()


def push_telegram(new_jobs, cfg):
    tg = cfg.get("telegram", {})
    # GitHub Actions 的 Secrets 通过环境变量进来,优先于 config
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id")
    env_enabled = bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
    if not (tg.get("enabled") or env_enabled) or not new_jobs:
        return
    if not token or not chat:
        print("  [Telegram] 缺 bot_token 或 chat_id,跳过")
        return
    lines = [f"<b>慕尼黑 IT 新职位 {len(new_jobs)} 条</b>"]
    for j in new_jobs[:30]:
        lines.append(f'· <a href="{html.escape(j.url)}">{html.escape(j.title)}</a> — '
                     f'{html.escape(j.company)}')
    if len(new_jobs) > 30:
        lines.append(f"…另有 {len(new_jobs)-30} 条,见 digest.html")
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": "\n".join(lines),
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=20)
    except Exception as e:
        print(f"  [Telegram] 推送失败: {e}")


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def cmd_run(cfg):
    print("抓取 arbeitnow…")
    raw = fetch_arbeitnow(cfg)
    print(f"  拿到 {len(raw)} 条")
    if (cfg.get("arbeitsagentur") or {}).get("enabled"):
        print("抓取联邦劳动局…")
        aa = fetch_arbeitsagentur(cfg)
        print(f"  拿到 {len(aa)} 条")
        raw += aa

    print("抓取公司 ATS…")
    company_jobs = fetch_companies()
    print(f"  拿到 {len(company_jobs)} 条")
    raw += company_jobs

    max_age = int((cfg.get("filters") or {}).get("max_posted_age_days", 365))
    for j in raw:
        if "cats" not in j.extra:
            j.extra["cats"] = classify(j)
    kept, stale = [], 0
    for j in raw:
        if not (matches_keywords(j, cfg["keywords"]) and matches_location(j, cfg["location"])):
            continue
        d = parse_posted(j.posted)
        if d and (datetime.now(timezone.utc).date() - d).days > max_age:
            stale += 1
            continue
        kept.append(j)
    if stale:
        print(f"丢弃过期职位 {stale} 条(发布超过 {max_age} 天)")
    print(f"\n过滤后 {len(kept)} / {len(raw)} 条符合关键词+地点")


    conn = db_connect()
    retag_all(conn)
    win = int((cfg.get("display") or {}).get("recent_days", 2))
    first = is_first_run(conn)
    new = split_new(conn, kept)
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    if first:
        mark_seeded(conn)
        print(f"\n首次运行:已把现有 {len(new)} 条收作基线,不算新职位。")
        print("从下一次运行起,只会显示真正新增的。")
        render_html(recent_days(conn, win), 0, total, True, cfg,
                    fallback=all_current(conn))
    else:
        hide = set((cfg.get("display") or {}).get("hide_tags") or [])
        if hide:
            hidden = [j for j in new if hide & set(j.extra.get("cats") or [])]
            new = [j for j in new if j not in hidden]
            if hidden:
                print(f"\n(隐藏 {len(hidden)} 条 {'、'.join(sorted(hide))} 岗位,仍入库)")
        print(f"\n★ 新职位 {len(new)} 条")
        for j in new[:15]:
            print(f"  · {j.title[:60]} — {j.company[:30]}")
        if len(new) > 15:
            print(f"  …另有 {len(new)-15} 条")
        render_html(recent_days(conn, win), len(new), total, False, cfg,
                    fallback=all_current(conn))
        push_telegram(new, cfg)

    conn.close()
    print(f"\n报告已生成: {OUT_HTML}")
    if cfg.get("open_browser") and not IN_CI and (first or new):
        try:
            webbrowser.open(OUT_HTML.as_uri())
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="慕尼黑 IT 职位每日差分监控")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="抓取并生成今日报告")
    d = sub.add_parser("discover", help="探测公司用的哪套 ATS")
    d.add_argument("slugs", nargs="+")
    sub.add_parser("stats")
    sub.add_parser("reset")
    args = ap.parse_args()

    if args.cmd == "discover":
        cmd_discover(args.slugs)
    elif args.cmd == "stats":
        conn = db_connect()
        n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        print(f"数据库共 {n} 条职位")
        for r in conn.execute("SELECT source, COUNT(*) FROM jobs GROUP BY source ORDER BY 2 DESC"):
            print(f"  {r[0]:<20} {r[1]}")
    elif args.cmd == "reset":
        if DB_PATH.exists():
            DB_PATH.unlink()
        print("数据库已清空,下次 run 会重新建立基线。")
    else:
        cmd_run(load_config())


if __name__ == "__main__":
    main()
