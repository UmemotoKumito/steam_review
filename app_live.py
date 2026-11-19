"""Steamレビュー分析アプリ ライブデモ版 (Streamlit).

ストアページのURLを貼り付けるだけで、ゲーム情報の取得・最新レビューの収集・
アスペクト別の暫定キーワード集計およびグラフ表示までを完結させる。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.steam_client import (
    SteamAPIError,
    extract_app_id,
    fetch_app_details,
    fetch_reviews,
)

# ---------------------------------------------------------------------------
# ページ基本設定
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Steamレビュー分析アプリ (Live Demo)",
    page_icon="🎮",
    layout="wide",
)

# ---------------------------------------------------------------------------
# 暫定キーワード方式の定義
# ※ 将来的に taxonomy.yaml + core/classifier.py (文単位・fastembed分類) へ置換予定
# ---------------------------------------------------------------------------
ASPECT_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "id": "performance",
        "name": "動作性能・バグ",
        "description": "FPS、処理落ち、クラッシュ、最適化、バグに関する評価",
        "keywords": [
            "fps", "処理落ち", "クラッシュ", "最適化", "重い", "軽い",
            "バグ", "フリーズ", "エラー", "ロード", "フレームレート", "カクつく",
            "動作", "スペック", "落ちる",
        ],
    },
    {
        "id": "gameplay",
        "name": "ゲーム性・面白さ",
        "description": "戦闘、システム、やり込み要素、テンポなどの中核的な面白さ",
        "keywords": [
            "面白い", "おもしろい", "楽しい", "たのしい", "神ゲー", "クソゲー",
            "戦闘", "アクション", "システム", "やり込み", "やりこみ", "テンポ",
            "バランス", "難易度", "単調", "飽き", "作業", "爽快", "中毒",
        ],
    },
    {
        "id": "story",
        "name": "ストーリー・世界観",
        "description": "物語、キャラクター、演出、世界観の魅力",
        "keywords": [
            "ストーリー", "物語", "シナリオ", "キャラ", "キャラクター",
            "世界観", "演出", "感動", "伏線", "雰囲気", "翻訳", "ローカライズ",
        ],
    },
    {
        "id": "graphics_audio",
        "name": "グラフィック・音楽",
        "description": "映像美、アートデザイン、BGM、効果音の評価",
        "keywords": [
            "グラフィック", "グラ", "映像", "綺麗", "きれい", "美しい",
            "bgm", "音楽", "サウンド", "曲", "効果音", "se", "ボイス", "声優",
        ],
    },
    {
        "id": "ui_controls",
        "name": "UI・操作性",
        "description": "インターフェース、操作感、カメラ、チュートリアル",
        "keywords": [
            "ui", "操作性", "操作", "キーコンフィグ", "コントローラー", "パッド",
            "マウス", "カメラ", "チュートリアル", "見にくい", "見やすい",
            "使いにくい", "使いやすい", "不親切", "親切", "メニュー",
        ],
    },
    {
        "id": "volume_price",
        "name": "ボリューム・価格",
        "description": "プレイ時間、コスパ、価格設定、DLC・課金要素",
        "keywords": [
            "ボリューム", "価格", "値段", "セール", "コスパ", "高い", "安い",
            "時間", "短い", "長い", "課金", "dlc", "ガチャ", "フルプライス",
        ],
    },
]

SCORE_DESC_JA_MAP: Dict[str, str] = {
    "Overwhelmingly Positive": "圧倒的に好評",
    "Very Positive": "非常に好評",
    "Positive": "好評",
    "Mostly Positive": "やや好評",
    "Mixed": "賛否両論",
    "Mostly Negative": "やや不評",
    "Negative": "不評",
    "Very Negative": "非常に不評",
    "Overwhelmingly Negative": "圧倒的に不評",
}


# ---------------------------------------------------------------------------
# キャッシュ層（Supabase導入時はここを core/cache.py の呼び出しへ差し替え）
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def cached_fetch_app_details(app_id: str, language: str = "japanese") -> Dict[str, Any]:
    """ゲーム基本情報を1時間キャッシュ付きで取得する."""
    return fetch_app_details(app_id=app_id, language=language)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_fetch_reviews(
    app_id: str,
    language: str = "japanese",
    max_reviews: int = 1500,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
    """最新レビューを1時間キャッシュ付きで取得する."""
    reviews, summary = fetch_reviews(
        app_id=app_id,
        language=language,
        max_reviews=max_reviews,
        interval_sec=0.5,
    )
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return reviews, summary, fetched_at


# ---------------------------------------------------------------------------
# 暫定キーワード集計ロジック（将来的に core/classifier.py + aggregator.py へ分離）
# ---------------------------------------------------------------------------
def analyze_reviews_keyword_mode(
    reviews: List[Dict[str, Any]],
) -> Tuple[pd.DataFrame, Dict[str, pd.DataFrame]]:
    """現行の暫定キーワード方式で項目別の好評・不評件数を集計する."""
    if not reviews:
        empty_summary = pd.DataFrame(
            columns=["id", "name", "positive", "negative", "total", "positive_rate"]
        )
        return empty_summary, {}

    df = pd.DataFrame(reviews)
    df["review_lower"] = df["review"].fillna("").str.lower()

    rows: List[Dict[str, Any]] = []
    matched_by_aspect: Dict[str, pd.DataFrame] = {}

    for aspect in ASPECT_DEFINITIONS:
        keywords = [kw.lower() for kw in aspect["keywords"]]
        mask = df["review_lower"].apply(
            lambda text: any(kw in text for kw in keywords)
        )
        subset = df[mask].copy()
        matched_by_aspect[aspect["id"]] = subset

        total = int(len(subset))
        positive = int(subset["voted_up"].sum()) if total > 0 else 0
        negative = total - positive
        positive_rate = round((positive / total) * 100, 1) if total > 0 else 0.0

        rows.append(
            {
                "id": aspect["id"],
                "name": aspect["name"],
                "positive": positive,
                "negative": negative,
                "total": total,
                "positive_rate": positive_rate,
            }
        )

    return pd.DataFrame(rows), matched_by_aspect


# ---------------------------------------------------------------------------
# グラフ描画関数
# ---------------------------------------------------------------------------
def build_bar_chart(summary_df: pd.DataFrame) -> go.Figure:
    """項目別の好評・不評レビュー件数（積み上げ棒グラフ）を生成する."""
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="好評 (おすすめ)",
            x=summary_df["name"],
            y=summary_df["positive"],
            marker_color="#2E86C1",
            text=summary_df["positive"],
            textposition="inside",
        )
    )
    fig.add_trace(
        go.Bar(
            name="不評 (おすすめしない)",
            x=summary_df["name"],
            y=summary_df["negative"],
            marker_color="#E74C3C",
            text=summary_df["negative"],
            textposition="inside",
        )
    )
    fig.update_layout(
        barmode="stack",
        title="項目別の言及件数（好評 / 不評）",
        xaxis_title="評価項目",
        yaxis_title="言及レビュー数",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=20, r=20, t=60, b=20),
        height=400,
    )
    return fig


def build_radar_chart(summary_df: pd.DataFrame) -> go.Figure:
    """項目別の好評率（レーダーチャート）を生成する."""
    categories = summary_df["name"].tolist()
    values = summary_df["positive_rate"].tolist()

    # レーダーチャートを閉じるため先頭要素を末尾に追加
    if categories:
        categories = categories + [categories[0]]
        values = values + [values[0]]

    fig = go.Figure()
    fig.add_trace(
        go.Scatterpolar(
            r=values,
            theta=categories,
            fill="toself",
            fillcolor="rgba(46, 134, 193, 0.25)",
            line=dict(color="#2E86C1", width=2),
            name="好評率 (%)",
            hovertemplate="%{theta}: %{r:.1f}%<extra></extra>",
        )
    )
    fig.update_layout(
        title="項目別の好評率バランス (%)",
        polar=dict(
            radialaxis=dict(
                visible=True,
                range=[0, 100],
                ticksuffix="%",
            )
        ),
        showlegend=False,
        margin=dict(l=40, r=40, t=60, b=20),
        height=400,
    )
    return fig


# ---------------------------------------------------------------------------
# UIレイアウト
# ---------------------------------------------------------------------------
def main() -> None:
    st.title("🎮 Steamレビュー リアルタイム分析（デモ版）")
    st.caption(
        "SteamストアページのURLを貼るだけで、最新レビューの取得・項目別グラフ化までを実行します。"
        "（※現在は暫定キーワード方式で動作中）"
    )

    # サイドバー：取得設定・キャッシュ破棄
    with st.sidebar:
        st.header("⚙️ 取得設定")
        max_reviews = st.slider(
            "最大取得レビュー件数",
            min_value=100,
            max_value=2000,
            value=1500,
            step=100,
            help="数万件あるゲームでも一定時間で終わるよう上限を設けています（既定: 1,500件）。",
        )
        language_label = st.selectbox(
            "レビュー言語",
            options=["日本語のみ (japanese)", "全言語 (all)", "英語のみ (english)"],
            index=0,
            help="日本語レビューが少ないインディーゲーム等の確認用に切り替え可能です。",
        )
        language_map = {
            "日本語のみ (japanese)": "japanese",
            "全言語 (all)": "all",
            "英語のみ (english)": "english",
        }
        selected_language = language_map[language_label]

        st.divider()
        st.subheader("キャッシュ管理")
        st.caption("取得結果は1時間キャッシュされ、同じゲームは2回目以降すぐ表示されます。")
        if st.button("🗑️ キャッシュを破棄して再取得可能にする", use_container_width=True):
            st.cache_data.clear()
            st.success("キャッシュを破棄しました。")

        st.divider()
        st.markdown(
            "**現在の実装ステータス**\n"
            "- ✅ URLからAppID抽出\n"
            "- ✅ Steam APIリアルタイム取得\n"
            "- 🟡 項目分類（暫定キーワード方式）\n"
            "- ⏳ Gemini AI要約（連携待ち）\n"
            "- ⏳ Supabase永続キャッシュ（連携待ち）"
        )

    # URL入力フォーム（st.formで不要な再実行を抑制）
    with st.form("steam_url_form"):
        col_input, col_btn = st.columns([5, 1])
        with col_input:
            url_input = st.text_input(
                "SteamストアページのURL または AppID を入力してください",
                value=st.session_state.get(
                    "last_url_input",
                    "https://store.steampowered.com/app/2246340/Monster_Hunter_Wilds/",
                ),
                placeholder="例: https://store.steampowered.com/app/2246340/Monster_Hunter_Wilds/",
            )
        with col_btn:
            st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
            submitted = st.form_submit_button("分析する", type="primary", use_container_width=True)

    if submitted:
        st.session_state["last_url_input"] = url_input
        st.session_state["active_target"] = url_input

    target = st.session_state.get("active_target")
    if not target:
        st.info("上のフォームにSteamストアページのURL（またはAppID）を入力して「分析する」を押してください。")
        return

    app_id = extract_app_id(target)
    if not app_id:
        st.error(
            "URLからAppIDを抽出できませんでした。"
            "`https://store.steampowered.com/app/数字/...` 形式のURL、または数字のみのAppIDを入力してください。"
        )
        return

    # -----------------------------------------------------------------------
    # 段階1: ゲーム基本情報を先に表示（〜数秒）
    # -----------------------------------------------------------------------
    info_container = st.container()
    try:
        with st.spinner("ゲーム情報を取得中..."):
            details = cached_fetch_app_details(app_id=app_id, language=selected_language)
    except SteamAPIError as exc:
        st.error(f"ゲーム情報の取得に失敗しました: {exc}")
        return

    with info_container:
        st.divider()
        col_img, col_meta = st.columns([1.2, 2.5])
        with col_img:
            if details.get("header_image"):
                st.image(details["header_image"], use_container_width=True)
        with col_meta:
            st.subheader(f"{details['name']} (AppID: {app_id})")
            if details.get("short_description"):
                st.write(details["short_description"])

            score_raw = details.get("review_score_desc", "データなし")
            score_disp = SCORE_DESC_JA_MAP.get(score_raw, score_raw)
            pos_rate_str = (
                f"{details['positive_rate']}%"
                if details["positive_rate"] is not None
                else "-"
            )

            m1, m2, m3 = st.columns(3)
            m1.metric("総合評価（選択言語）", score_disp)
            m2.metric("好評率", pos_rate_str)
            m3.metric("総レビュー数", f"{details['total_reviews']:,} 件")

            dev_text = ", ".join(details.get("developers", [])) or "不明"
            st.caption(
                f"開発元: {dev_text} ｜ 発売日: {details['release_date']} ｜ "
                f"[ストアページを開く]({details['store_url']})"
            )

    # -----------------------------------------------------------------------
    # 段階2: 最新レビューの取得・集計・グラフ表示（LLMなし）
    # -----------------------------------------------------------------------
    st.divider()
    try:
        with st.status(
            f"最新レビューを取得・分析中（最大 {max_reviews:,} 件 / 言語: {selected_language}）...",
            expanded=True,
        ) as status:
            st.write("Steam Review APIからレビューを取得しています（キャッシュがある場合は即座に完了します）...")
            reviews, _, fetched_at = cached_fetch_reviews(
                app_id=app_id,
                language=selected_language,
                max_reviews=max_reviews,
            )
            st.write(f"取得完了: {len(reviews):,} 件のレビューを分析します。")
            summary_df, matched_by_aspect = analyze_reviews_keyword_mode(reviews)
            status.update(
                label=f"分析完了：{len(reviews):,} 件の最新レビューを集計しました（取得時刻: {fetched_at}）",
                state="complete",
                expanded=False,
            )
    except SteamAPIError as exc:
        st.error(f"レビューの取得中にエラーが発生しました: {exc}")
        return

    if not reviews:
        st.warning(
            "選択した条件ではレビューが見つかりませんでした。"
            "左サイドバーの「レビュー言語」を「全言語 (all)」に変更して再度お試しください。"
        )
        return

    # 取得サンプル全体のサマリー指標
    total_fetched = len(reviews)
    pos_fetched = sum(1 for r in reviews if r["voted_up"])
    neg_fetched = total_fetched - pos_fetched
    fetched_pos_rate = round((pos_fetched / total_fetched) * 100, 1)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("分析対象レビュー数", f"{total_fetched:,} 件")
    c2.metric("おすすめ（好評）", f"{pos_fetched:,} 件")
    c3.metric("おすすめしない（不評）", f"{neg_fetched:,} 件")
    c4.metric("最新レビュー好評率", f"{fetched_pos_rate}%")

    # 棒グラフとレーダーチャートを並列表示
    col_bar, col_radar = st.columns(2)
    with col_bar:
        st.plotly_chart(build_bar_chart(summary_df), use_container_width=True)
    with col_radar:
        st.plotly_chart(build_radar_chart(summary_df), use_container_width=True)

    with st.expander("📊 項目別集計テーブルを見る"):
        display_df = summary_df[["name", "total", "positive", "negative", "positive_rate"]].rename(
            columns={
                "name": "評価項目",
                "total": "言及数",
                "positive": "好評",
                "negative": "不評",
                "positive_rate": "好評率 (%)",
            }
        )
        st.dataframe(display_df, use_container_width=True, hide_index=True)

    # -----------------------------------------------------------------------
    # 段階3: AI要約エリア（メンバー1のGemini連携待ちプレースホルダー）
    # -----------------------------------------------------------------------
    st.divider()
    st.subheader("🤖 AIアスペクト別要約（連携待ちプレースホルダー）")
    st.info(
        "ここに各評価項目の代表レビュー（20〜30件）から生成した **Gemini AI要約（1リクエスト・JSON出力）** が表示されます。\n\n"
        "- 現在は `core/summarizer.py` および Supabase キャッシュの連携待ちです。\n"
        "- API枠切れや混雑時も、上のグラフ表示は止めずにこのエリアだけフォールバック表示する設計です。"
    )

    placeholder_cols = st.columns(3)
    for idx, aspect in enumerate(ASPECT_DEFINITIONS):
        row = summary_df[summary_df["id"] == aspect["id"]].iloc[0]
        with placeholder_cols[idx % 3]:
            with st.container(border=True):
                st.markdown(f"**{aspect['name']}**")
                st.caption(
                    f"言及: {int(row['total'])}件 / 好評率: {row['positive_rate']}%"
                )
                st.markdown(
                    "_AI要約未接続：ここに「良い点」「悪い点」の要約テキストが入ります。_"
                )

    # -----------------------------------------------------------------------
    # おまけ：項目別に拾われた実レビューの確認タブ（精度検証用）
    # -----------------------------------------------------------------------
    st.divider()
    st.subheader("💬 項目別ピックアップレビュー（現行キーワード分類の確認用）")
    st.caption(
        "※現行方式では「バグは多いが神ゲー」のようなレビューも全体の voted_up で集計されます。"
        "次のステップで文単位＋埋め込み分類（fastembed）に置き換えて改善します。"
    )

    tabs = st.tabs([aspect["name"] for aspect in ASPECT_DEFINITIONS])
    for tab, aspect in zip(tabs, ASPECT_DEFINITIONS):
        with tab:
            subset = matched_by_aspect.get(aspect["id"], pd.DataFrame())
            if subset.empty:
                st.write("この項目に一致するレビューはありませんでした。")
                continue

            col_pos, col_neg = st.columns(2)
            with col_pos:
                st.markdown("##### 👍 好評レビュー（上位5件・参考になった順）")
                pos_df = subset[subset["voted_up"]].sort_values(
                    by="votes_up", ascending=False
                ).head(5)
                if pos_df.empty:
                    st.caption("該当なし")
                for _, r in pos_df.iterrows():
                    with st.container(border=True):
                        st.caption(
                            f"プレイ時間: {r['playtime_forever_hours']}時間 ｜ 参考になった: {r['votes_up']}"
                        )
                        text = r["review"]
                        st.write(text[:300] + ("..." if len(text) > 300 else ""))

            with col_neg:
                st.markdown("##### 👎 不評レビュー（上位5件・参考になった順）")
                neg_df = subset[~subset["voted_up"]].sort_values(
                    by="votes_up", ascending=False
                ).head(5)
                if neg_df.empty:
                    st.caption("該当なし")
                for _, r in neg_df.iterrows():
                    with st.container(border=True):
                        st.caption(
                            f"プレイ時間: {r['playtime_forever_hours']}時間 ｜ 参考になった: {r['votes_up']}"
                        )
                        text = r["review"]
                        st.write(text[:300] + ("..." if len(text) > 300 else ""))


if __name__ == "__main__":
    main()
