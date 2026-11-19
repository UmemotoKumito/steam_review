"""Steam Store / Review API クライアントモジュール.

URLからのAppID抽出、ゲーム基本情報の取得、最新レビューの逐次取得を担当する。
UI（Streamlit等）に依存しない純粋なロジック層として設計している。
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STEAM_APPREVIEWS_URL = "https://store.steampowered.com/appreviews/{app_id}"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# /app/12345/ や /agecheck/app/12345/、数字のみの入力に対応
_APP_ID_URL_PATTERN = re.compile(r"/app/(\d+)(?:/|\?|$)")
_APP_ID_DIGITS_PATTERN = re.compile(r"^\d+$")


class SteamAPIError(Exception):
    """Steam APIへのリクエスト失敗時に送出する例外."""


def extract_app_id(url_or_id: str) -> Optional[str]:
    """SteamストアURLまたは数字文字列からAppIDを抽出する.

    対応形式:
        - https://store.steampowered.com/app/2246340/Monster_Hunter_Wilds/
        - https://store.steampowered.com/agecheck/app/2246340/
        - https://store.steampowered.com/app/2246340
        - 2246340

    Args:
        url_or_id: ストアページURLまたはAppID文字列。

    Returns:
        抽出できたAppID文字列。抽出できない場合はNone。
    """
    if not url_or_id:
        return None

    text = url_or_id.strip()
    if _APP_ID_DIGITS_PATTERN.match(text):
        return text

    match = _APP_ID_URL_PATTERN.search(text)
    if match:
        return match.group(1)

    return None


def _request_with_retry(
    session: requests.Session,
    url: str,
    params: Dict[str, Any],
    timeout: float = 15.0,
    max_retries: int = 4,
    base_backoff: float = 1.5,
) -> Dict[str, Any]:
    """429/5xxエラーおよび一時的な通信エラーに対してリトライ付きでGETする."""
    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            response = session.get(
                url,
                params=params,
                headers=DEFAULT_HEADERS,
                timeout=timeout,
            )

            # レート制限(429)や一時的なサーバーエラー(5xx)はバックオフして再試行
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < max_retries:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        wait_sec = float(retry_after)
                    else:
                        wait_sec = base_backoff * (2 ** attempt)
                    time.sleep(min(wait_sec, 30.0))
                    continue
                raise SteamAPIError(
                    f"Steam APIがエラーを返しました (HTTP {response.status_code})"
                )

            response.raise_for_status()
            return response.json()

        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < max_retries:
                wait_sec = base_backoff * (2 ** attempt)
                time.sleep(min(wait_sec, 15.0))
                continue
            raise SteamAPIError(f"Steam APIへの接続に失敗しました: {exc}") from exc

    raise SteamAPIError(f"Steam APIのリクエストに失敗しました: {last_error}")


def fetch_app_details(app_id: str, language: str = "japanese") -> Dict[str, Any]:
    """ゲームの基本情報（タイトル・画像・総合評価サマリー等）を取得する.

    Args:
        app_id: SteamのAppID。
        language: 表示言語（既定: "japanese"）。

    Returns:
        ゲーム基本情報とレビュー総合評価を含む辞書。
    """
    with requests.Session() as session:
        # 1. ストア基本情報の取得
        detail_json = _request_with_retry(
            session,
            STEAM_APPDETAILS_URL,
            params={"appids": app_id, "l": language},
        )
        app_entry = detail_json.get(str(app_id), {})
        if not app_entry.get("success"):
            raise SteamAPIError(
                f"AppID={app_id} のゲーム情報が見つかりませんでした。URLまたはIDを確認してください。"
            )
        data = app_entry.get("data", {})

        # 2. レビュー総合評価サマリー（対象言語）の取得
        summary_json = _request_with_retry(
            session,
            STEAM_APPREVIEWS_URL.format(app_id=app_id),
            params={
                "json": 1,
                "language": language,
                "purchase_type": "all",
                "num_per_page": 0,
            },
        )
        query_summary = summary_json.get("query_summary", {})

        # 対象言語のレビュー総数が少ない場合の参考用に全言語サマリーも取得
        all_summary_json = _request_with_retry(
            session,
            STEAM_APPREVIEWS_URL.format(app_id=app_id),
            params={
                "json": 1,
                "language": "all",
                "purchase_type": "all",
                "num_per_page": 0,
            },
        )
        all_query_summary = all_summary_json.get("query_summary", {})

    total_reviews = query_summary.get("total_reviews", 0)
    total_positive = query_summary.get("total_positive", 0)
    positive_rate = (
        round((total_positive / total_reviews) * 100, 1) if total_reviews > 0 else None
    )

    all_total = all_query_summary.get("total_reviews", 0)
    all_pos = all_query_summary.get("total_positive", 0)
    all_rate = round((all_pos / all_total) * 100, 1) if all_total > 0 else None

    return {
        "app_id": str(app_id),
        "name": data.get("name", f"App {app_id}"),
        "header_image": data.get("header_image", ""),
        "short_description": data.get("short_description", ""),
        "developers": data.get("developers", []),
        "publishers": data.get("publishers", []),
        "release_date": data.get("release_date", {}).get("date", "不明"),
        "store_url": f"https://store.steampowered.com/app/{app_id}/",
        "language": language,
        # 選択言語の総合評価
        "review_score_desc": query_summary.get("review_score_desc", "データなし"),
        "total_reviews": total_reviews,
        "total_positive": total_positive,
        "total_negative": query_summary.get("total_negative", 0),
        "positive_rate": positive_rate,
        # 全言語の総合評価（参考表示用）
        "all_review_score_desc": all_query_summary.get("review_score_desc", "データなし"),
        "all_total_reviews": all_total,
        "all_positive_rate": all_rate,
    }


def fetch_reviews(
    app_id: str,
    language: str = "japanese",
    max_reviews: int = 1500,
    interval_sec: float = 0.5,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Steam Review APIから最新レビューをcursorページ送りで取得する.

    Steam API仕様上、cursorは直前のレスポンスに依存するため逐次取得を行う。
    重複除外（recommendationid基準）、429リトライ、リクエスト間隔制御を備える。

    Args:
        app_id: SteamのAppID。
        language: 取得言語（既定: "japanese"。全言語は "all"）。
        max_reviews: 取得上限件数（既定: 1500件）。
        interval_sec: リクエスト間の待機秒数（既定: 0.5秒）。
        progress_callback: 進捗通知用コールバック fn(fetched_count, max_reviews)。

    Returns:
        (正規化済みレビュー辞書のリスト, query_summary辞書)
    """
    reviews: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    query_summary: Dict[str, Any] = {}

    cursor = "*"
    url = STEAM_APPREVIEWS_URL.format(app_id=app_id)
    page_count = 0
    # 無限ループ防止のための最大ページ数ガード
    max_pages = max(1, (max_reviews // 100) + 10)

    with requests.Session() as session:
        while len(reviews) < max_reviews and page_count < max_pages:
            remaining = max_reviews - len(reviews)
            num_per_page = min(100, remaining)

            params = {
                "json": 1,
                "filter": "recent",
                "language": language,
                "review_type": "all",
                "purchase_type": "all",
                "num_per_page": num_per_page,
                "cursor": cursor,
            }

            payload = _request_with_retry(session, url, params=params)
            if payload.get("success") != 1:
                raise SteamAPIError("レビュー取得APIが失敗ステータスを返しました。")

            if not query_summary and isinstance(payload.get("query_summary"), dict):
                query_summary = payload["query_summary"]

            batch = payload.get("reviews", [])
            if not batch:
                break

            added_in_page = 0
            for item in batch:
                rec_id = str(item.get("recommendationid", ""))
                if not rec_id or rec_id in seen_ids:
                    continue
                seen_ids.add(rec_id)

                author = item.get("author", {}) or {}
                reviews.append(
                    {
                        "recommendationid": rec_id,
                        "review": (item.get("review") or "").strip(),
                        "voted_up": bool(item.get("voted_up", False)),
                        "timestamp_created": int(item.get("timestamp_created", 0)),
                        "playtime_forever_hours": round(
                            float(author.get("playtime_forever", 0)) / 60.0, 1
                        ),
                        "votes_up": int(item.get("votes_up", 0)),
                        "language": item.get("language", language),
                    }
                )
                added_in_page += 1
                if len(reviews) >= max_reviews:
                    break

            page_count += 1
            if progress_callback is not None:
                progress_callback(len(reviews), max_reviews)

            next_cursor = payload.get("cursor")
            if not next_cursor or next_cursor == cursor or added_in_page == 0:
                break

            cursor = next_cursor
            if len(reviews) < max_reviews and interval_sec > 0:
                time.sleep(interval_sec)

    return reviews, query_summary
