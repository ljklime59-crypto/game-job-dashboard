from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import time
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener


ROOT = Path(__file__).resolve().parent
BASE_URL = "https://gamejob.co.kr"
CONFIG_PATH = ROOT / "config.json"


@dataclass
class Job:
    id: str
    company: str
    title: str
    info: str
    deadline: str
    url: str
    source: str
    score: int
    matched_keywords: list[str]
    priority_company: bool
    fetched_at: str
    first_seen: str = ""
    last_seen: str = ""
    seen_count: int = 1
    is_active: bool = True
    is_new: bool = False


class TokenParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[dict[str, str]] = []
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        self._link_href = attrs_dict.get("href", "")
        self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._link_href is None:
            return
        text = clean_text(" ".join(self._link_text))
        if text:
            self.tokens.append({"type": "link", "text": text, "href": self._link_href})
        self._link_href = None
        self._link_text = []

    def handle_data(self, data: str) -> None:
        pieces = [clean_text(part) for part in re.split(r"[\r\n]+", data)]
        pieces = [part for part in pieces if part]
        if not pieces:
            return
        if self._link_href is not None:
            self._link_text.extend(pieces)
            return
        for piece in pieces:
            self.tokens.append({"type": "text", "text": piece, "href": ""})


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def make_opener():
    return build_opener(HTTPCookieProcessor(CookieJar()))


def fetch_url(opener, url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36 job-dashboard/0.1"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.6,en;q=0.5",
        },
    )
    with opener.open(request, timeout=20) as response:
        body = response.read()
        return decode_body(body, response.headers.get_content_charset())


def decode_body(body: bytes, header_charset: str | None = None) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        pass

    head = body[:4000].decode("ascii", errors="ignore")
    meta = re.search(r"charset=[\"']?([\w-]+)", head, flags=re.I)
    candidates = [meta.group(1) if meta else "", header_charset or "", "cp949", "euc-kr", "utf-8"]
    for charset in [item for item in candidates if item]:
        try:
            return body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def looks_like_job_link(token: dict[str, str]) -> bool:
    if token.get("type") != "link":
        return False
    href = token.get("href", "")
    text = token.get("text", "")
    if len(text) < 5:
        return False
    if text in {"채용정보", "전체 채용정보", "다음", "이전"}:
        return False
    return bool(re.search(r"(GIB?_Read|GI_No|List_GI|Recruit/Read|Recruit/View)", href, re.I))


def looks_like_page_link(token: dict[str, str]) -> bool:
    if token.get("type") != "link":
        return False
    href = token.get("href", "")
    text = token.get("text", "")
    return bool(re.search(r"_GI_Job_List|Page=\d+", href, re.I) and re.search(r"\d+|다음", text))


def parse_tokens(markup: str) -> list[dict[str, str]]:
    parser = TokenParser()
    parser.feed(markup)
    return parser.tokens


def token_text(token: dict[str, str]) -> str:
    return clean_text(token.get("text", ""))


def previous_company(tokens: list[dict[str, str]], index: int) -> str:
    ignored = {
        "홈",
        "채용정보",
        "커뮤니티",
        "기업정보",
        "인재정보",
        "로그인",
        "회원가입",
        "직종별",
        "지역별",
        "경력별",
        "전체 채용정보",
    }
    for token in reversed(tokens[max(0, index - 8):index]):
        text = token_text(token)
        if not text or text in ignored:
            continue
        if looks_like_job_link(token):
            continue
        if len(text) <= 30 and not re.search(r"채용|모집|공고|검색|메뉴", text):
            return text
    return "회사명 미확인"


def following_details(tokens: list[dict[str, str]], index: int) -> tuple[str, str]:
    texts: list[str] = []
    for token in tokens[index + 1:index + 24]:
        if looks_like_job_link(token):
            break
        text = token_text(token)
        if text and text not in {"채용공고 스크랩", "채용정보 스크랩"}:
            texts.append(text)
    deadline_parts = [
        text for text in texts if re.search(r"(채용시|상시|\d{2}/\d{2}|오늘|어제|\d+\s*(분|시간|일) 전|등록)", text)
    ]
    info_parts = [
        text
        for text in texts
        if text not in deadline_parts
        and re.search(r"(신입|경력|학력|서울|경기|정규직|계약직|인턴|모바일게임|온라인PC게임|콘솔게임|멀티플랫폼게임|웹게임)", text)
    ]
    info = " ".join(info_parts)
    deadline = " ".join(deadline_parts)
    return info, deadline


def extract_jobs(markup: str, page_url: str, source_name: str, config: dict) -> tuple[list[dict], list[str]]:
    tokens = parse_tokens(markup)
    raw_jobs: list[dict] = []
    pagination_urls: list[str] = []
    for index, token in enumerate(tokens):
        if looks_like_page_link(token):
            pagination_urls.append(urljoin(page_url, token["href"]))
        if not looks_like_job_link(token):
            continue
        title = token_text(token).replace("채용공고 스크랩", "").strip()
        info, deadline = following_details(tokens, index)
        raw_jobs.append(
            {
                "company": previous_company(tokens, index),
                "title": title,
                "info": info,
                "deadline": deadline,
                "url": urljoin(page_url, token.get("href", "")),
                "source": source_name,
            }
        )
    if not raw_jobs:
        raw_jobs = extract_jobs_from_text(markup, page_url, source_name)
    jobs = [score_job(job, config) for job in raw_jobs]
    return jobs, unique(pagination_urls)


def extract_jobs_from_text(markup: str, page_url: str, source_name: str) -> list[dict]:
    text = re.sub(r"<script\b.*?</script>", " ", markup, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    lines = [clean_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    jobs: list[dict] = []
    for idx, line in enumerate(lines):
        if not re.search(r"채용|모집|기획|디자이너", line):
            continue
        next_lines = lines[idx + 1:idx + 4]
        info = next((item for item in next_lines if re.search(r"(신입|경력|학력|서울|경기|정규직|계약직)", item)), "")
        deadline = next((item for item in next_lines if re.search(r"(채용시|상시|\d{2}/\d{2}|\d+\s*(분|시간|일) 전)", item)), "")
        company = lines[idx - 1] if idx > 0 and len(lines[idx - 1]) <= 30 else "회사명 미확인"
        if info or deadline:
            jobs.append(
                {
                    "company": company,
                    "title": line.replace("채용공고 스크랩", "").strip(),
                    "info": info,
                    "deadline": deadline,
                    "url": page_url,
                    "source": source_name,
                }
            )
    return jobs


def score_job(raw: dict, config: dict) -> dict:
    combined = f"{raw.get('company', '')} {raw.get('title', '')} {raw.get('info', '')}"
    role_matches = find_matches(combined, config["role_keywords"])
    location_matches = find_matches(combined, config["locations"])
    exclude_matches = find_matches(combined, config["exclude_keywords"])
    priority = bool(find_matches(raw.get("company", ""), config["priority_companies"]) or find_matches(combined, config["priority_companies"]))

    score = 0
    if role_matches:
        score += 35 + min(15, (len(role_matches) - 1) * 5)
    if location_matches:
        score += 20
    if re.search(r"(신입|경력무관|주니어)", combined):
        score += 15
    if priority:
        score += 25
    if raw.get("deadline"):
        score += 5
    if re.search(r"(\d+\s*(분|시간|일) 전 등록|오늘 등록)", raw.get("deadline", "")):
        score += 5
    if exclude_matches:
        score -= 35

    return {
        **raw,
        "score": max(0, min(100, score)),
        "matched_keywords": role_matches + location_matches,
        "priority_company": priority,
        "excluded_keywords": exclude_matches,
    }


def find_matches(text: str, keywords: Iterable[str]) -> list[str]:
    normalized = text.replace(" ", "").lower()
    matches = []
    for keyword in keywords:
        key = keyword.replace(" ", "").lower()
        if key and key in normalized:
            matches.append(keyword)
    return matches


def unique(values: Iterable[str]) -> list[str]:
    seen = set()
    output = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def job_id(raw: dict) -> str:
    explicit = re.search(r"GI_No=([A-Za-z0-9_-]+)", raw.get("url", ""))
    if explicit:
        return explicit.group(1)
    key = "|".join([raw.get("company", ""), raw.get("title", ""), raw.get("url", "")])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def collect_jobs(config: dict, debug: bool = False) -> list[Job]:
    opener = make_opener()
    fetched_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    collected: dict[str, Job] = {}

    for source in config["sources"]:
        pending = [source["url"]]
        visited: set[str] = set()
        pages_read = 0
        while pending and pages_read < int(config.get("max_pages_per_source", 1)):
            url = pending.pop(0)
            if url in visited:
                continue
            visited.add(url)
            pages_read += 1
            markup = fetch_url(opener, url)
            if debug:
                debug_dir = ROOT / "data" / "debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                debug_name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10] + ".html"
                (debug_dir / debug_name).write_text(markup, encoding="utf-8")
            raw_jobs, page_links = extract_jobs(markup, url, source["name"], config)
            if debug:
                print(f"debug source={source['name']} url={url} bytes={len(markup)} raw_jobs={len(raw_jobs)} page_links={len(page_links)}")
                for sample in raw_jobs[:5]:
                    print(
                        "debug job "
                        f"score={sample['score']} company={sample['company']} "
                        f"title={sample['title']} info={sample['info']} deadline={sample['deadline']}"
                    )
            for raw in raw_jobs:
                if raw["score"] < int(config.get("min_score", 0)):
                    continue
                if not find_matches(raw.get("info", ""), config["locations"]) and not find_matches(raw.get("title", ""), config["locations"]):
                    continue
                raw["id"] = job_id(raw)
                raw["fetched_at"] = fetched_at
                collected[raw["id"]] = Job(
                    id=raw["id"],
                    company=raw["company"],
                    title=raw["title"],
                    info=raw["info"],
                    deadline=raw["deadline"],
                    url=raw["url"],
                    source=raw["source"],
                    score=raw["score"],
                    matched_keywords=raw["matched_keywords"],
                    priority_company=raw["priority_company"],
                    fetched_at=raw["fetched_at"],
                )
            for page_link in page_links:
                if same_host(url, page_link) and page_link not in visited:
                    pending.append(page_link)
            time.sleep(float(config.get("request_delay_seconds", 1.0)))

    return sorted(collected.values(), key=lambda item: (-item.score, item.company, item.title))


def same_host(left: str, right: str) -> bool:
    return urlparse(left).netloc.lower() == urlparse(right).netloc.lower()


def save_jobs(jobs: list[Job], config: dict) -> Path:
    path = ROOT / config["data_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump([asdict(job) for job in jobs], handle, ensure_ascii=False, indent=2)
    return path


def history_path(config: dict) -> Path:
    return ROOT / config.get("history_path", "data/job_history.json")


def snapshot_dir(config: dict) -> Path:
    return ROOT / config.get("snapshot_dir", "data/snapshots")


def load_jobs(config: dict) -> list[Job]:
    path = ROOT / config["data_path"]
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [Job(**item) for item in json.load(handle)]


def load_history(config: dict) -> list[Job]:
    path = history_path(config)
    if not path.exists():
        return load_jobs(config)
    with path.open("r", encoding="utf-8") as handle:
        return [Job(**item) for item in json.load(handle)]


def save_history(jobs: list[Job], config: dict) -> Path:
    path = history_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump([asdict(job) for job in jobs], handle, ensure_ascii=False, indent=2)
    return path


def save_snapshot(jobs: list[Job], config: dict, run_id: str) -> Path:
    path = snapshot_dir(config) / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump([asdict(job) for job in jobs], handle, ensure_ascii=False, indent=2)
    return path


def merge_history(current_jobs: list[Job], history_jobs: list[Job], run_label: str) -> list[Job]:
    current_by_id = {job.id: job for job in current_jobs}
    history_by_id = {job.id: job for job in history_jobs}
    merged: dict[str, Job] = {}

    for job_id, old_job in history_by_id.items():
        old_job.is_active = False
        old_job.is_new = False
        merged[job_id] = old_job

    for job in current_jobs:
        previous = history_by_id.get(job.id)
        if previous:
            job.first_seen = previous.first_seen or previous.fetched_at or run_label
            job.seen_count = int(previous.seen_count or 1) + 1
            job.is_new = False
        else:
            job.first_seen = run_label
            job.seen_count = 1
            job.is_new = True
        job.last_seen = run_label
        job.is_active = True
        merged[job.id] = job

    return sorted(
        merged.values(),
        key=lambda item: (
            not item.is_active,
            not item.is_new,
            -item.score,
            item.company,
            item.title,
        ),
    )


def render_dashboard(jobs: list[Job], config: dict, current_count: int | None = None) -> Path:
    output_path = ROOT / config["output_path"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    priority_count = sum(1 for job in jobs if job.priority_company)
    newbie_count = sum(1 for job in jobs if re.search(r"(신입|경력무관|주니어)", job.info))
    companies = sorted({job.company for job in jobs if job.company})
    rows = "\n".join(render_job_row(job) for job in jobs)
    company_options = "\n".join(f'<option value="{escape_attr(company)}">{html.escape(company)}</option>' for company in companies)
    document = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
  
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>게임 기획 채용 대시보드</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #687386;
      --line: #d9dee7;
      --accent: #0f766e;
      --accent-2: #b45309;
      --danger: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Segoe UI", "Malgun Gothic", sans-serif;
      letter-spacing: 0;
    }}
    header {{
      padding: 28px clamp(16px, 4vw, 44px) 18px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }}
    h1 {{ margin: 0 0 8px; font-size: 28px; line-height: 1.2; }}
    .sub {{ color: var(--muted); font-size: 14px; }}
    main {{ padding: 20px clamp(16px, 4vw, 44px) 44px; }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }}
    .stat strong {{ display: block; font-size: 24px; }}
    .stat span {{ color: var(--muted); font-size: 13px; }}
    .filters {{
      display: grid;
      grid-template-columns: minmax(180px, 1fr) 190px 150px 140px;
      gap: 10px;
      margin-bottom: 16px;
    }}
    input, select {{
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 8px 10px;
      font: inherit;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      padding: 12px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }}
    th {{
      font-size: 12px;
      color: var(--muted);
      background: #f0f3f7;
      white-space: nowrap;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    a {{ color: #075985; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .score {{
      display: inline-flex;
      min-width: 42px;
      height: 28px;
      align-items: center;
      justify-content: center;
      border-radius: 6px;
      background: #e6f4f1;
      color: var(--accent);
      font-weight: 700;
    }}
    .priority {{
      display: inline-flex;
      margin-left: 6px;
      padding: 2px 6px;
      border-radius: 999px;
      background: #fff4e5;
      color: var(--accent-2);
      font-size: 12px;
      font-weight: 700;
    }}
    .keywords {{ color: var(--muted); font-size: 12px; margin-top: 5px; }}
    .state {{
      min-width: 108px;
    }}
    .empty {{
      display: none;
      padding: 24px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--muted);
    }}
    @media (max-width: 900px) {{
      .stats {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
      .filters {{ grid-template-columns: 1fr 1fr; }}
      table, thead, tbody, th, td, tr {{ display: block; }}
      thead {{ display: none; }}
      tr {{
        border-bottom: 1px solid var(--line);
        padding: 10px;
      }}
      td {{
        border: 0;
        padding: 6px 0;
      }}
      td::before {{
        content: attr(data-label);
        display: block;
        color: var(--muted);
        font-size: 12px;
        margin-bottom: 3px;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>게임 기획 채용 대시보드</h1>
    <div class="sub">수집 기준: 게임잡, 게임 기획, 서울/경기, 넥슨/넷마블 우선 표시 · 마지막 생성: {html.escape(now)}</div>
  </header>
  <main>
    <section class="stats" aria-label="요약">
      <div class="stat"><strong>{len(jobs)}</strong><span>표시 공고</span></div>
      <div class="stat"><strong>{priority_count}</strong><span>넥슨/넷마블 계열</span></div>
      <div class="stat"><strong>{newbie_count}</strong><span>신입/경력무관</span></div>
      <div class="stat"><strong>{html.escape(str(config.get("min_score", "")))}</strong><span>최소 점수</span></div>
    </section>
    <section class="filters" aria-label="필터">
      <input id="search" type="search" placeholder="회사, 제목, 키워드 검색">
      <select id="company"><option value="">전체 회사</option>{company_options}</select>
      <select id="priority"><option value="">전체</option><option value="true">우선 기업</option></select>
      <select id="stateFilter"><option value="">전체 상태</option><option>관심</option><option>지원 예정</option><option>지원 완료</option><option>보류</option></select>
    </section>
    <div id="empty" class="empty">조건에 맞는 공고가 없습니다.</div>
    <table id="jobs">
      <thead>
        <tr>
          <th>점수</th>
          <th>회사</th>
          <th>공고</th>
          <th>조건</th>
          <th>마감/등록</th>
          <th>상태</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </main>
  <script>
    const controls = ["search", "company", "priority", "stateFilter"].map((id) => document.getElementById(id));
    document.querySelectorAll(".state").forEach((select) => {{
      const key = "job-state:" + select.dataset.id;
      select.value = localStorage.getItem(key) || "";
      select.addEventListener("change", () => {{
        localStorage.setItem(key, select.value);
        applyFilters();
      }});
    }});
    controls.forEach((control) => control.addEventListener("input", applyFilters));
    function applyFilters() {{
      const q = document.getElementById("search").value.trim().toLowerCase();
      const company = document.getElementById("company").value;
      const priority = document.getElementById("priority").value;
      const state = document.getElementById("stateFilter").value;
      let visible = 0;
      document.querySelectorAll("#jobs tbody tr").forEach((row) => {{
        const haystack = row.innerText.toLowerCase();
        const stateValue = row.querySelector(".state").value;
        const show =
          (!q || haystack.includes(q)) &&
          (!company || row.dataset.company === company) &&
          (!priority || row.dataset.priority === priority) &&
          (!state || stateValue === state);
        row.style.display = show ? "" : "none";
        if (show) visible += 1;
      }});
      document.getElementById("empty").style.display = visible ? "none" : "block";
    }}
    applyFilters();
  </script>
</body>
</html>
"""
    output_path.write_text(document, encoding="utf-8")
    return output_path


def render_job_row(job: Job) -> str:
    keywords = ", ".join(job.matched_keywords[:8])
    priority = '<span class="priority">우선</span>' if job.priority_company else ""
    return f"""
        <tr data-company="{escape_attr(job.company)}" data-priority="{str(job.priority_company).lower()}">
          <td data-label="점수"><span class="score">{job.score}</span></td>
          <td data-label="회사">{html.escape(job.company)}{priority}<div class="keywords">{html.escape(job.source)}</div></td>
          <td data-label="공고"><a href="{escape_attr(job.url)}" target="_blank" rel="noopener noreferrer">{html.escape(job.title)}</a><div class="keywords">{html.escape(keywords)}</div></td>
          <td data-label="조건">{html.escape(job.info or "-")}</td>
          <td data-label="마감/등록">{html.escape(job.deadline or "-")}</td>
          <td data-label="상태"><select class="state" data-id="{escape_attr(job.id)}"><option value="">미정</option><option>관심</option><option>지원 예정</option><option>지원 완료</option><option>보류</option></select></td>
        </tr>"""


def render_dashboard(jobs: list[Job], config: dict, current_count: int | None = None) -> Path:
    output_path = ROOT / config["output_path"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    active_count = sum(1 for job in jobs if job.is_active)
    new_count = sum(1 for job in jobs if job.is_new)
    inactive_count = sum(1 for job in jobs if not job.is_active)
    priority_count = sum(1 for job in jobs if job.priority_company)
    newbie_count = sum(1 for job in jobs if re.search(r"(신입|경력무관|주니어)", job.info))
    companies = sorted({job.company for job in jobs if job.company})
    rows = "\n".join(render_job_row(job) for job in jobs)
    company_options = "\n".join(
        f'<option value="{escape_attr(company)}">{html.escape(company)}</option>'
        for company in companies
    )
    current_label = current_count if current_count is not None else active_count
    document = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>게임 기획 채용 대시보드</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #687386;
      --line: #d9dee7;
      --accent: #0f766e;
      --accent-2: #b45309;
      --new: #1d4ed8;
      --old: #8a94a6;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Segoe UI", "Malgun Gothic", sans-serif;
      letter-spacing: 0;
    }}
    header {{
      padding: 28px clamp(16px, 4vw, 44px) 18px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }}
    h1 {{ margin: 0 0 8px; font-size: 28px; line-height: 1.2; }}
    .sub {{ color: var(--muted); font-size: 14px; }}
    main {{ padding: 20px clamp(16px, 4vw, 44px) 44px; }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }}
    .stat strong {{ display: block; font-size: 24px; }}
    .stat span {{ color: var(--muted); font-size: 13px; }}
    .filters {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) 190px 150px 150px 140px;
      gap: 10px;
      margin-bottom: 16px;
    }}
    input, select {{
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 8px 10px;
      font: inherit;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      padding: 12px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }}
    th {{
      font-size: 12px;
      color: var(--muted);
      background: #f0f3f7;
      white-space: nowrap;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    tr[data-active="false"] {{ color: var(--old); background: #fafbfc; }}
    a {{ color: #075985; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .score {{
      display: inline-flex;
      min-width: 42px;
      height: 28px;
      align-items: center;
      justify-content: center;
      border-radius: 6px;
      background: #e6f4f1;
      color: var(--accent);
      font-weight: 700;
    }}
    .badge {{
      display: inline-flex;
      margin-left: 6px;
      padding: 2px 6px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .priority {{ background: #fff4e5; color: var(--accent-2); }}
    .new {{ background: #e0ebff; color: var(--new); }}
    .inactive {{ background: #eef1f5; color: var(--old); }}
    .keywords {{ color: var(--muted); font-size: 12px; margin-top: 5px; }}
    .state {{ min-width: 108px; }}
    .empty {{
      display: none;
      padding: 24px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--muted);
    }}
    @media (max-width: 1050px) {{
      .stats {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
      .filters {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (max-width: 760px) {{
      table, thead, tbody, th, td, tr {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-bottom: 1px solid var(--line); padding: 10px; }}
      td {{ border: 0; padding: 6px 0; }}
      td::before {{
        content: attr(data-label);
        display: block;
        color: var(--muted);
        font-size: 12px;
        margin-bottom: 3px;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>게임 기획 채용 대시보드</h1>
    <div class="sub">수집 기준: 게임잡, 게임 기획, 서울/경기, 넥슨/넷마블 우선 표시 · 마지막 생성: {html.escape(now)}</div>
  </header>
  <main>
    <section class="stats" aria-label="요약">
      <div class="stat"><strong>{len(jobs)}</strong><span>누적 공고</span></div>
      <div class="stat"><strong>{current_label}</strong><span>이번 수집 공고</span></div>
      <div class="stat"><strong>{new_count}</strong><span>신규 공고</span></div>
      <div class="stat"><strong>{inactive_count}</strong><span>이전 수집 공고</span></div>
      <div class="stat"><strong>{priority_count}</strong><span>넥슨/넷마블 계열</span></div>
      <div class="stat"><strong>{newbie_count}</strong><span>신입/경력무관</span></div>
    </section>
    <section class="filters" aria-label="필터">
      <input id="search" type="search" placeholder="회사, 제목, 키워드 검색">
      <select id="company"><option value="">전체 회사</option>{company_options}</select>
      <select id="freshness"><option value="">전체 이력</option><option value="new">신규만</option><option value="active">현재 활성</option><option value="inactive">이전 수집</option></select>
      <select id="priority"><option value="">전체</option><option value="true">우선 기업</option></select>
      <select id="stateFilter"><option value="">전체 상태</option><option>관심</option><option>지원 예정</option><option>지원 완료</option><option>보류</option></select>
    </section>
    <div id="empty" class="empty">조건에 맞는 공고가 없습니다.</div>
    <table id="jobs">
      <thead>
        <tr>
          <th>점수</th>
          <th>회사</th>
          <th>공고</th>
          <th>조건</th>
          <th>이력</th>
          <th>상태</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </main>
  <script>
    const controls = ["search", "company", "freshness", "priority", "stateFilter"].map((id) => document.getElementById(id));
    document.querySelectorAll(".state").forEach((select) => {{
      const key = "job-state:" + select.dataset.id;
      select.value = localStorage.getItem(key) || "";
      select.addEventListener("change", () => {{
        localStorage.setItem(key, select.value);
        applyFilters();
      }});
    }});
    controls.forEach((control) => control.addEventListener("input", applyFilters));
    function applyFilters() {{
      const q = document.getElementById("search").value.trim().toLowerCase();
      const company = document.getElementById("company").value;
      const freshness = document.getElementById("freshness").value;
      const priority = document.getElementById("priority").value;
      const state = document.getElementById("stateFilter").value;
      let visible = 0;
      document.querySelectorAll("#jobs tbody tr").forEach((row) => {{
        const haystack = row.innerText.toLowerCase();
        const stateValue = row.querySelector(".state").value;
        const freshnessMatch =
          !freshness ||
          (freshness === "new" && row.dataset.new === "true") ||
          (freshness === "active" && row.dataset.active === "true") ||
          (freshness === "inactive" && row.dataset.active === "false");
        const show =
          (!q || haystack.includes(q)) &&
          (!company || row.dataset.company === company) &&
          freshnessMatch &&
          (!priority || row.dataset.priority === priority) &&
          (!state || stateValue === state);
        row.style.display = show ? "" : "none";
        if (show) visible += 1;
      }});
      document.getElementById("empty").style.display = visible ? "none" : "block";
    }}
    applyFilters();
  </script>
</body>
</html>
"""
    output_path.write_text(document, encoding="utf-8")
    return output_path


def render_job_row(job: Job) -> str:
    keywords = ", ".join(job.matched_keywords[:8])
    badges = []
    if job.priority_company:
        badges.append('<span class="badge priority">우선</span>')
    if job.is_new:
        badges.append('<span class="badge new">신규</span>')
    if not job.is_active:
        badges.append('<span class="badge inactive">이전</span>')
    badge_html = "".join(badges)
    first_seen = job.first_seen or job.fetched_at or "-"
    last_seen = job.last_seen or job.fetched_at or "-"
    history_text = f"첫 발견 {first_seen}<br>최근 확인 {last_seen}<br>{job.seen_count}회 수집"
    deadline = html.escape(job.deadline or "-")
    return f"""
        <tr data-company="{escape_attr(job.company)}" data-priority="{str(job.priority_company).lower()}" data-active="{str(job.is_active).lower()}" data-new="{str(job.is_new).lower()}">
          <td data-label="점수"><span class="score">{job.score}</span></td>
          <td data-label="회사">{html.escape(job.company)}{badge_html}<div class="keywords">{html.escape(job.source)}</div></td>
          <td data-label="공고"><a href="{escape_attr(job.url)}" target="_blank" rel="noopener noreferrer">{html.escape(job.title)}</a><div class="keywords">{html.escape(keywords)}</div></td>
          <td data-label="조건">{html.escape(job.info or "-")}<div class="keywords">{deadline}</div></td>
          <td data-label="이력">{history_text}</td>
          <td data-label="상태"><select class="state" data-id="{escape_attr(job.id)}"><option value="">미정</option><option>관심</option><option>지원 예정</option><option>지원 완료</option><option>보류</option></select></td>
        </tr>"""


def escape_attr(value: str) -> str:
    return html.escape(value, quote=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="게임잡 게임 기획 채용공고를 수집해 HTML 대시보드를 생성합니다.")
    parser.add_argument("--no-fetch", action="store_true", help="저장된 data/jobs.json만 사용해 대시보드를 다시 생성합니다.")
    parser.add_argument("--debug", action="store_true", help="수집한 HTML과 파싱 개수를 data/debug에 저장합니다.")
    args = parser.parse_args()

    config = load_config()
    current_count = None
    if args.no_fetch:
        jobs = load_history(config)
        current_count = sum(1 for job in jobs if job.is_active)
    else:
        run_dt = dt.datetime.now()
        run_label = run_dt.strftime("%Y-%m-%d %H:%M")
        run_id = run_dt.strftime("%Y-%m-%d_%H%M%S")
        previous_history = load_history(config)
        current_jobs = collect_jobs(config, debug=args.debug)
        save_jobs(current_jobs, config)
        save_snapshot(current_jobs, config, run_id)
        jobs = merge_history(current_jobs, previous_history, run_label)
        save_history(jobs, config)
        current_count = len(current_jobs)
    dashboard = render_dashboard(jobs, config, current_count=current_count)
    print(f"jobs={current_count if current_count is not None else len(jobs)}")
    print(f"history={len(jobs)}")
    print(f"dashboard={dashboard}")


if __name__ == "__main__":
    main()
