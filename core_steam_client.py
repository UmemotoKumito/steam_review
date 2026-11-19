"""Steam Store / Review API クライアントモジュール.

URLからのAppID抽出、ゲーム基本情報の取得、最新レビューの逐次取得を担当する。
UI（Streamlit等）に依存しない純粋なロジック層として設計している。

Note:
    2026年10月22日に従来の store.steampowered.com/appreviews/{appid}?json=1 が廃止されたため、
    レビュー取得には公式後継APIである IUserReviewsService/GetAppReviews/v1/ を使用する。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STEAM_REVIEWS_API_URL = (
    "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
)
STEAM_HEADER_CDN_URL = (
    "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{app_id}/header.jpg"
)

# IUserReviewsService Enum 定数
FILTER_HELPFUL = 0
FILTER_RECENT = 1
FILTER_UPDATED = 2
FILTER_FUNNY = 3

REVIEW_TYPE_ALL = 0
REVIEW_TYPE_POSITIVE = 1
REVIEW_TYPE_NEGATIVE = 2

PURCHASE_TYPE_STEAM = 0
PURCHASE_TYPE_ALL = 1
PURCHASE_TYPE_NON_STEAM = 2

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# ストア詳細APIで年齢確認ページに飛ばされるのを防ぐCookie
AGE_GATE_COOKIES = {
    "birthtime": "568022401",
    "lastagecheckage": "1-0-1988",
    "mature_content": "1",
    "wants_mature_content": "1",
}

# /app/12345/ や /agecheck/app/12345/、数字のみの入力に対応
_APP_ID_URL_PATTERN = re.compile(r"/app/(\d+)(?:/|\?|$)")
_APP_ID_DIGITS_PATTERN = re.compile(r"^\d+$")


class SteamAPIError(Exception):
    """Steam APIへのリクエスト失敗時に送出する例外."""


def _create_session() -> requests.Session:
    """年齢確認Cookieと共通ヘッダーを設定したSessionを生成する."""
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.cookies.update(AGE_GATE_COOKIES)
    return session


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
    timeout: float = 20.0,
    max_retries: int = 3,
    base_backoff: float = 1.5,
) -> Dict[str, Any]:
    """429/5xxや空レスポンス・一時的な通信エラーに対してリトライ付きでJSONを取得する."""
    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            response = session.get(
                url,
                params=params,
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
                    f"Steam APIがエラーを返しました (HTTP {response.status_code}, URL: {url})"
                )

            response.raise_for_status()

            body_text = (response.text or "").strip()
            if not body_text:
                last_error = ValueError(
                    f"空のレスポンスが返されました (HTTP {response.status_code}, URL: {url})"
                )
                if attempt < max_retries:
                    time.sleep(min(base_backoff * (2 ** attempt), 15.0))
                    continue
                raise SteamAPIError(str(last_error))

            try:
                parsed = response.json()
            except ValueError as json_exc:
                content_type = response.headers.get("Content-Type", "unknown")
                preview = body_text[:120].replace("\n", " ")
                last_error = ValueError(
                    f"JSON以外のレスポンスを受信しました "
                    f"(HTTP {response.status_code}, Content-Type: {content_type}, "
                    f"URL: {url}, 先頭: {preview!r})"
                )
                if attempt < max_retries:
                    time.sleep(min(base_backoff * (2 ** attempt), 15.0))
                    continue
                raise SteamAPIError(str(last_error)) from json_exc

            if not isinstance(parsed, dict):
                raise SteamAPIError(
                    f"想定外のJSON形式を受信しました (type={type(parsed).__name__}, URL: {url})"
                )

            return parsed

        except SteamAPIError:
            raise
        except requests.RequestException as exc:
            last_error = exc
            if attempt < max_retries:
                wait_sec = base_backoff * (2 ** attempt)
                time.sleep(min(wait_sec, 15.0))
                continue
            raise SteamAPIError(f"Steam APIへの接続に失敗しました ({url}): {exc}") from exc

    raise SteamAPIError(f"Steam APIのリクエストに失敗しました ({url}): {last_error}")


def _call_get_app_reviews(
    session: requests.Session,
    app_id: str,
    language: str = "japanese",
    num_per_page: int = 100,
    cursor: str = "*",
    review_filter: int = FILTER_RECENT,
    review_type: int = REVIEW_TYPE_ALL,
    purchase_type: int = PURCHASE_TYPE_ALL,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """公式新API IUserReviewsService/GetAppReviews/v1/ を呼び出す.

    APIキーなし（匿名）で呼び出し可能。環境変数 STEAM_API_KEY がある場合は自動で付与する。
    """
    try:
        app_id_int = int(app_id)
    except (TypeError, ValueError) as exc:
        raise SteamAPIError(f"無効なAppIDです: {app_id!r}") from exc

    query: Dict[str, Any] = {
        "appid": app_id_int,
        "filter": review_filter,
        "languages": [language] if language else ["all"],
        "review_type": review_type,
        "purchase_type": purchase_type,
        "num_per_page": max(1, min(100, int(num_per_page))),
        "cursor": cursor,
    }

    params: Dict[str, Any] = {
        "input_json": json.dumps(query, ensure_ascii=False),
    }
    api_key = os.environ.get("STEAM_API_KEY", "").strip()
    if api_key:
        params["key"] = api_key

    payload = _request_with_retry(
        session,
        STEAM_REVIEWS_API_URL,
        params=params,
        max_retries=max_retries,
    )

    # Service Interface のレスポンスは {"response": {...}} でラップされる
    if isinstance(payload.get("response"), dict):
        return payload["response"]
    return payload


def fetch_app_details(app_id: str, language: str = "japanese") -> Dict[str, Any]:
    """ゲームの基本情報（タイトル・画像・総合評価サマリー等）を取得する.

    Args:
        app_id: SteamのAppID。
        language: 表示言語（既定: "japanese"）。

    Returns:
        ゲーム基本情報とレビュー総合評価を含む辞書。
    """
    data: Dict[str, Any] = {}
    appdetails_warning: Optional[str] = None
    query_summary: Dict[str, Any] = {}
    appreviews_error: Optional[Exception] = None

    store_lang = "japanese" if language == "all" else language

    with _create_session() as session:
        # 1. ストア基本情報の取得（失敗してもアプリ全体を止めずフォールバック）
        try:
            detail_json = _request_with_retry(
                session,
                STEAM_APPDETAILS_URL,
                params={"appids": app_id, "l": store_lang, "cc": "jp"},
                max_retries=2,
            )
            app_entry = detail_json.get(str(app_id), {})
            if isinstance(app_entry, dict) and app_entry.get("success"):
                data = app_entry.get("data", {}) or {}
            else:
                appdetails_warning = (
                    f"ストア詳細情報が取得できませんでした（AppID={app_id}）。"
                )
        except SteamAPIError as exc:
            appdetails_warning = f"ストア詳細APIの取得をスキップしました: {exc}"

        time.sleep(0.3)

        # 2. 新レビューAPIから対象言語の総合評価サマリーを取得（1ページ目のみ返却）
        try:
            resp_data = _call_get_app_reviews(
                session=session,
                app_id=app_id,
                language=language,
                num_per_page=1,
                cursor="*",
                review_filter=FILTER_RECENT,
                review_type=REVIEW_TYPE_ALL,
                purchase_type=PURCHASE_TYPE_ALL,
                max_retries=3,
            )
            query_summary = resp_data.get("query_summary", {}) or {}
        except SteamAPIError as exc:
            appreviews_error = exc

    if not data and appreviews_error is not None:
        detail_msg = f" / {appdetails_warning}" if appdetails_warning else ""
        raise SteamAPIError(f"{appreviews_error}{detail_msg}")

    total_reviews = int(query_summary.get("total_reviews", 0) or 0)
    total_positive = int(query_summary.get("total_positive", 0) or 0)
    positive_rate = (
        round((total_positive / total_reviews) * 100, 1) if total_reviews > 0 else None
    )

    short_desc = data.get("short_description", "")
    if not short_desc and appdetails_warning:
        short_desc = f"※ {appdetails_warning} レビュー分析は続行します。"

    return {
        "app_id": str(app_id),
        "name": data.get("name") or f"Steam App {app_id}",
        "header_image": data.get("header_image")
        or STEAM_HEADER_CDN_URL.format(app_id=app_id),
        "short_description": short_desc,
        "developers": data.get("developers", []),
        "publishers": data.get("publishers", []),
        "release_date": (data.get("release_date") or {}).get("date", "不明"),
        "store_url": f"https://store.steampowered.com/app/{app_id}/",
        "language": language,
        # 選択言語の総合評価
        "review_score_desc": query_summary.get("review_score_desc", "データなし"),
        "total_reviews": total_reviews,
        "total_positive": total_positive,
        "total_negative": int(query_summary.get("total_negative", 0) or 0),
        "positive_rate": positive_rate,
    }


def fetch_reviews(
    app_id: str,
    language: str = "japanese",
    max_reviews: int = 1500,
    interval_sec: float = 0.5,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """IUserReviewsService/GetAppReviews/v1/ から最新レビューをcursorページ送りで取得する.

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
    page_count = 0
    # 無限ループ防止のための最大ページ数ガード
    max_pages = max(1, (max_reviews // 100) + 10)

    with _create_session() as session:
        while len(reviews) < max_reviews and page_count < max_pages:
            remaining = max_reviews - len(reviews)
            num_per_page = min(100, remaining)

            resp_data = _call_get_app_reviews(
                session=session,
                app_id=app_id,
                language=language,
                num_per_page=num_per_page,
                cursor=cursor,
                review_filter=FILTER_RECENT,
                review_type=REVIEW_TYPE_ALL,
                purchase_type=PURCHASE_TYPE_ALL,
                max_retries=3,
            )

            if not query_summary and isinstance(resp_data.get("query_summary"), dict):
                query_summary = resp_data["query_summary"]

            batch = resp_data.get("reviews", []) or []
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

            next_cursor = resp_data.get("cursor")
            if not next_cursor or next_cursor == cursor or added_in_page == 0:
                break

            cursor = next_cursor
            if len(reviews) < max_reviews and interval_sec > 0:
                time.sleep(interval_sec)

    return reviews, query_summary
