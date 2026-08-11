"""
미국 뉴스 브리핑 봇
- 신뢰도 높은 언론사(AP, Reuters, NPR)의 RSS 수집
- Gemini 무료 API로 중요 기사 10개 선별 + 한국어 제목 번역 + 한 줄 요약
- 텔레그램 발송 (요약 실패 시 영어 원문 제목으로 폴백)

필요 환경변수 (GitHub Secrets):
  BOT_TOKEN      : 텔레그램 봇 토큰
  CHAT_ID        : 받을 채팅방 ID
  GEMINI_API_KEY : Google AI Studio API 키
"""

import os
import json
import re
import requests
import feedparser
from datetime import datetime, timezone, timedelta

# ── 설정 (여기만 고치면 소스/개수/성향 조절 가능) ──────────────────

RSS_SOURCES = {
    "Reuters": "https://news.google.com/rss/search?q=source:reuters&hl=en-US&gl=US&ceid=US:en",
    "AP": "https://news.google.com/rss/search?q=source:%22associated%20press%22&hl=en-US&gl=US&ceid=US:en",
    "NPR": "https://feeds.npr.org/1001/rss.xml",
}

PER_SOURCE = 15        # 소스당 가져올 기사 수
TOP_N = 10             # 최종 선별 개수

SELECTION_PROMPT = """당신은 뉴스 큐레이터입니다. 아래는 오늘 미국 주요 언론사(AP, Reuters, NPR)의 기사 목록입니다.

작업:
1. 이 중 가장 중요하고 영향력 있는 뉴스 {top_n}개를 선별하세요.
2. 같은 사건을 다룬 중복 기사는 하나로 합치세요 (여러 언론사가 다룬 사건일수록 중요한 뉴스입니다).
3. 특정 언론사에 쏠리지 않게 하세요.
4. 각 기사에 대해 자연스러운 한국어 제목과 한 줄 요약(한국어)을 작성하세요.

반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트, 마크다운 코드블록 없이 JSON만 출력하세요:
{{"articles": [{{"title_ko": "한국어 제목", "summary_ko": "한 줄 요약", "source": "언론사명", "link": "원문 링크"}}]}}

기사 목록:
{articles}"""

# ────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/{model}:generateContent"
)

KST = timezone(timedelta(hours=9))


def call_gemini(prompt: str) -> str | None:
    """모델 목록을 순서대로 시도. 성공하면 응답 텍스트, 전부 실패하면 None."""
    for model in GEMINI_MODELS:
        try:
            res = requests.post(
                GEMINI_URL_TEMPLATE.format(model=model),
                headers={"x-goog-api-key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=60,
            )
            if res.status_code == 404:
                print(f"[정보] 모델 {model} 없음(404), 다음 모델 시도")
                continue
            res.raise_for_status()
            return res.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            body = getattr(e, "response", None)
            detail = body.text[:500] if body is not None else str(e)
            print(f"[경고] 모델 {model} 호출 실패: {detail}")
    return None


def fetch_articles() -> list:
    """모든 RSS 소스에서 기사(source, title, link) 수집."""
    articles = []
    for source, url in RSS_SOURCES.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:PER_SOURCE]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "")
                if title and link:
                    articles.append(
                        {"source": source, "title": title, "link": link}
                    )
        except Exception as e:
            print(f"[경고] {source} 수집 실패: {e}")
    return articles


def summarize_with_gemini(articles: list) -> list | None:
    """Gemini에게 선별+번역+요약을 맡김. 실패하면 None."""
    article_lines = "\n".join(
        f"- [{a['source']}] {a['title']} ({a['link']})" for a in articles
    )
    prompt = SELECTION_PROMPT.format(top_n=TOP_N, articles=article_lines)

    text = call_gemini(prompt)
    if text is None:
        return None
    try:
        # 혹시 ```json ``` 로 감싸서 주면 벗겨내기
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        data = json.loads(text)
        items = data.get("articles", [])
        return items[:TOP_N] if items else None
    except Exception as e:
        print(f"[경고] Gemini 응답 파싱 실패: {e}")
        print(f"[디버그] 응답 앞부분: {text[:300]}")
        return None


def build_message(items: list) -> str:
    today = datetime.now(KST).strftime("%m월 %d일")
    lines = [f"🇺🇸 <b>{today} 미국 뉴스 브리핑 TOP {len(items)}</b>\n"]
    for i, it in enumerate(items, 1):
        title = it.get("title_ko", "")
        summary = it.get("summary_ko", "")
        link = it.get("link", "")
        source = it.get("source", "")
        lines.append(f'{i}. <a href="{link}"><b>{title}</b></a> ({source})')
        if summary:
            lines.append(f"   └ {summary}")
    return "\n".join(lines)


def build_fallback_message(articles: list) -> str:
    """Gemini 실패 시: 소스별로 번갈아 뽑아서 영어 원문 제목으로 발송."""
    today = datetime.now(KST).strftime("%m월 %d일")

    # 소스별로 그룹핑 후 라운드로빈으로 골고루 선택
    by_source = {}
    for a in articles:
        by_source.setdefault(a["source"], []).append(a)
    picked = []
    idx = 0
    while len(picked) < TOP_N and any(by_source.values()):
        for source in list(by_source.keys()):
            if idx < len(by_source[source]) and len(picked) < TOP_N:
                picked.append(by_source[source][idx])
        idx += 1

    lines = [
        f"🇺🇸 <b>{today} 미국 뉴스 TOP {len(picked)}</b>",
        "<i>(요약 서비스 오류로 원문 제목으로 보냅니다)</i>\n",
    ]
    for i, a in enumerate(picked, 1):
        lines.append(f'{i}. <a href="{a["link"]}">{a["title"]}</a> ({a["source"]})')
    return "\n".join(lines)


def send_telegram(text: str):
    res = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=10,
    )
    res.raise_for_status()


def main():
    articles = fetch_articles()
    print(f"수집: {len(articles)}건")
    if not articles:
        send_telegram("⚠️ 오늘은 미국 뉴스를 가져오지 못했습니다. (RSS 오류)")
        return

    items = summarize_with_gemini(articles)
    if items:
        send_telegram(build_message(items))
        print(f"발송 완료: 요약본 {len(items)}건")
    else:
        send_telegram(build_fallback_message(articles))
        print("발송 완료: 폴백(원문 제목)")


if __name__ == "__main__":
    main()
