import argparse
import io
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import BinaryIO, Optional

import pandas as pd


COLUMN_ALIASES = {
    "商談名": ["商談名", "Opportunity Name", "案件名"],
    "商談MRR": ["商談MRR", "MRR", "月額MRR", "MRR（円）"],
    "Close Date": ["Close Date", "完了予定日", "クローズ日", "受注予定日"],
    "注力案件": ["注力案件", "注力", "重点案件"],
    "次のステップ & 状況": ["次のステップ & 状況", "次のステップ&状況", "次のステップ", "Next Step"],
    "次回アクション日": ["次回アクション日", "次回アクション予定日", "Next Action Date"],
    "フェーズ": ["フェーズ", "Stage", "商談フェーズ"],
    "フェーズ滞在期間": ["フェーズ滞在期間", "フェーズ滞在日数", "Stage Age"],
    "商談日数": ["商談日数", "案件経過日数", "Age"],
    "初回商談日": ["【初回商談】日付", "初回商談日", "初回商談日付", "First Meeting Date"],
    "メイン競合": ["メイン競合", "競合"],
    "主リードソース": ["主リードソース", "リードソース", "Lead Source"],
    "商談所有者": ["商談所有者", "商談 所有者", "Opportunity Owner", "所有者"],
    "売上予測カテゴリ": ["売上予測カテゴリ", "Forecast Category", "予測カテゴリ"],
}

REQUIRED_COLUMNS = ["商談名", "商談MRR", "Close Date"]
OPTIONAL_DEFAULTS = {
    "注力案件": 0,
    "次のステップ & 状況": "",
    "次回アクション日": pd.NaT,
    "フェーズ": "",
    "フェーズ滞在期間": pd.NA,
    "初回商談日": pd.NaT,
}

# 上から先に一致したルールを採用する。自社運用に合わせてここだけ変更可能。
PHASE_RULES = [
    (r"^(6|受注|closed won)", 1.00),
    (r"^(5|最終|契約|稟議)", 0.90),
    (r"^(4|提案|見積)", 0.70),
    (r"^(3|評価|検証|トライアル)", 0.50),
    (r"^(2|要件|ヒアリング)", 0.30),
    (r"^(1|初回|接触)", 0.10),
    (r"^(0|失注|closed lost)", 0.00),
]
FORECAST_WEIGHTS = {"commit": 0.90, "確約": 0.90, "best case": 0.60, "最善": 0.60, "pipeline": 0.30, "パイプライン": 0.30, "omitted": 0.00, "除外": 0.00}
CLOSED_LOST_PATTERN = r"失注|closed lost|見送り|消滅"
SCENARIO_ORDER = {"min": 0, "conservative": 1, "max": 2, "除外": 99}
MONTHLY_TARGETS = {
    "2026-06": 0,
    "2026-07": 50_000,
    "2026-08": 100_000,
    "2026-09": 130_000,
}


@dataclass
class ReportResult:
    raw: pd.DataFrame
    cleaned: pd.DataFrame
    monthly_forecast: pd.DataFrame
    focus_deals: pd.DataFrame
    action_list: pd.DataFrame
    risks: pd.DataFrame
    answer_cards: dict[str, float | int | str]
    warnings: list[str]


def _read_bytes(path_or_buffer) -> tuple[bytes, str]:
    name = getattr(path_or_buffer, "name", str(path_or_buffer))
    if isinstance(path_or_buffer, (str, Path)):
        return Path(path_or_buffer).read_bytes(), name
    if hasattr(path_or_buffer, "seek"):
        path_or_buffer.seek(0)
    data = path_or_buffer.read()
    if hasattr(path_or_buffer, "seek"):
        path_or_buffer.seek(0)
    return data, name


def _read_any(path_or_buffer) -> pd.DataFrame:
    """CSV/XLSXを読み、途中にあるSalesforceのヘッダー行を検出する。"""
    data, name = _read_bytes(path_or_buffer)
    suffix = Path(name).suffix.lower()
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        raw = pd.read_excel(io.BytesIO(data), header=None)
    else:
        errors = []
        for encoding in ("utf-8-sig", "cp932", "utf-8"):
            try:
                raw = pd.read_csv(io.BytesIO(data), header=None, encoding=encoding, dtype=object)
                break
            except (UnicodeDecodeError, pd.errors.ParserError) as exc:
                errors.append(str(exc))
        else:
            raise ValueError("CSVを読み込めませんでした。UTF-8またはShift-JISで保存してください。")

    header_row = None
    known_names = {alias.casefold() for aliases in COLUMN_ALIASES.values() for alias in aliases}
    for i in range(min(len(raw), 100)):
        values = {str(v).strip().casefold() for v in raw.iloc[i].tolist() if pd.notna(v)}
        if len(values & known_names) >= 2 and any(x in values for x in ("商談名", "opportunity name", "案件名")):
            header_row = i
            break
    if header_row is None:
        raise ValueError("ヘッダー行を検出できませんでした。商談名を含むヘッダーが必要です。")

    header = [str(v).strip() if pd.notna(v) else "" for v in raw.iloc[header_row].tolist()]
    df = raw.iloc[header_row + 1 :].copy()
    df.columns = header
    df = df.dropna(how="all").loc[:, [bool(c) for c in df.columns]]
    df = df.loc[:, ~df.columns.duplicated()].copy()
    return df


def _standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    normalized = {str(c).strip().casefold(): c for c in df.columns}
    rename = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias.casefold() in normalized:
                rename[normalized[alias.casefold()]] = canonical
                break
    return df.rename(columns=rename)


def _money(series: pd.Series) -> pd.Series:
    text = series.astype("string").str.replace(r"[¥￥,$,\s]", "", regex=True).str.replace("円", "", regex=False)
    negative = text.str.match(r"^\(.*\)$", na=False)
    values = pd.to_numeric(text.str.replace(r"[()]", "", regex=True), errors="coerce").fillna(0)
    values.loc[negative] *= -1
    return values


def _flag(value) -> int:
    if pd.isna(value):
        return 0
    return int(str(value).strip().casefold() in {"1", "true", "yes", "y", "○", "〇", "◯", "有", "あり", "注力"})


def _weight(row: pd.Series) -> float:
    category = str(row.get("売上予測カテゴリ", "")).strip().casefold()
    for key, weight in FORECAST_WEIGHTS.items():
        if key in category:
            return weight
    phase = str(row.get("フェーズ", "")).strip().casefold()
    for pattern, weight in PHASE_RULES:
        if re.search(pattern, phase, flags=re.IGNORECASE):
            return weight
    return 0.50 if row.get("注力案件") == 1 else 0.30


def _risk_reasons(row: pd.Series) -> str:
    reasons = []
    if row["アクション状態"] == "未設定":
        reasons.append("次回アクション未設定")
    elif row["アクション状態"] == "期限超過":
        reasons.append("次回アクション期限超過")
    if pd.notna(row["フェーズ滞在期間"]) and row["フェーズ滞在期間"] >= 14:
        reasons.append(f"フェーズ停滞{int(row['フェーズ滞在期間'])}日")
    if pd.isna(row["Close Date"]):
        reasons.append("Close Date未設定")
    return " / ".join(reasons) if reasons else "通常"


def _suggestion(row: pd.Series) -> str:
    if row["アクション状態"] == "未設定":
        return "次回アクションと期限を設定"
    if row["アクション状態"] == "期限超過":
        return "本日中に状況確認し、次回日を更新"
    if pd.notna(row["フェーズ滞在期間"]) and row["フェーズ滞在期間"] >= 14:
        return "停滞理由と前進条件を確認"
    if not str(row.get("次のステップ & 状況", "")).strip():
        return "次のステップを具体化"
    return "予定アクションを実施"


def clean_salesforce_export(df: pd.DataFrame, today: Optional[date] = None) -> tuple[pd.DataFrame, list[str]]:
    today = today or date.today()
    cleaned = _standardize_columns(df.copy())
    missing = [c for c in REQUIRED_COLUMNS if c not in cleaned.columns]
    if missing:
        raise ValueError(f"必要列がありません: {', '.join(missing)}")
    warnings = []
    for col, default in OPTIONAL_DEFAULTS.items():
        if col not in cleaned.columns:
            cleaned[col] = default
            warnings.append(f"「{col}」列がないため既定値で処理しました。")

    cleaned["注力案件"] = cleaned["注力案件"].map(_flag)
    cleaned["商談MRR"] = _money(cleaned["商談MRR"])
    for col in ("Close Date", "次回アクション日", "初回商談日"):
        cleaned[col] = pd.to_datetime(cleaned[col], errors="coerce")
    cleaned["フェーズ滞在期間"] = pd.to_numeric(cleaned["フェーズ滞在期間"], errors="coerce")
    cleaned = cleaned[cleaned["商談名"].notna() & cleaned["商談名"].astype(str).str.strip().ne("")].copy()

    phase_text = cleaned["フェーズ"].astype("string")
    lost = phase_text.str.contains(CLOSED_LOST_PATTERN, case=False, regex=True, na=False)
    cleaned["失注"] = lost
    cleaned["カード申込"] = cleaned["商談名"].astype("string").str.contains("カード申込", na=False)
    if lost.any():
        warnings.append(f"失注・見送り {int(lost.sum())}件は活動実績に残し、着地見込みから除外しました。")
    # 活動実績の分母に使うため失注案件は保持する。カード申込の重複行のみ除外。
    cleaned = cleaned[~cleaned["カード申込"]].copy()

    cleaned["Close Month"] = cleaned["Close Date"].dt.to_period("M").astype("string")
    cleaned["重み"] = cleaned.apply(_weight, axis=1)
    cleaned["加重MRR"] = cleaned["商談MRR"] * cleaned["重み"]

    today_ts = pd.Timestamp(today)
    week_end = today_ts + pd.Timedelta(days=(6 - today_ts.weekday()))
    cleaned["アクション状態"] = "今週以降"
    cleaned.loc[cleaned["次回アクション日"].isna(), "アクション状態"] = "未設定"
    cleaned.loc[cleaned["次回アクション日"] < today_ts, "アクション状態"] = "期限超過"
    this_week = cleaned["次回アクション日"].between(today_ts, week_end)
    cleaned.loc[this_week, "アクション状態"] = "今週対応"
    cleaned["リスク理由"] = cleaned.apply(_risk_reasons, axis=1)
    cleaned["推奨アクション"] = cleaned.apply(_suggestion, axis=1)
    return cleaned, warnings


def _answer_cards(cleaned: pd.DataFrame, today: date) -> dict[str, float | int | str]:
    cleaned = cleaned[(~cleaned["失注"]) & (cleaned["商談MRR"] > 0)]
    today_ts = pd.Timestamp(today)
    current_period = today_ts.to_period("M")
    close_period = cleaned["Close Date"].dt.to_period("M")
    quarter_end = current_period + (2 - (current_period.month - 1) % 3)
    current_q = (close_period >= current_period) & (close_period <= quarter_end)
    follow = cleaned["リスク理由"].ne("通常")
    return {
        "総MRR": cleaned["商談MRR"].sum(),
        "今月MRR": cleaned.loc[close_period == current_period, "商談MRR"].sum(),
        "来月MRR": cleaned.loc[close_period == current_period + 1, "商談MRR"].sum(),
        "今四半期MRR": cleaned.loc[current_q, "商談MRR"].sum(),
        "今四半期加重MRR": cleaned.loc[current_q, "加重MRR"].sum(),
        "注力案件MRR": cleaned.loc[cleaned["注力案件"] == 1, "商談MRR"].sum(),
        "要フォロー件数": int(follow.sum()),
        "基準日": today.isoformat(),
    }


def build_report(path_or_buffer, today: Optional[date] = None) -> ReportResult:
    today = today or date.today()
    raw = _read_any(path_or_buffer)
    cleaned, warnings = clean_salesforce_export(raw, today=today)
    active = cleaned[(~cleaned["失注"]) & (cleaned["商談MRR"] > 0)].copy()
    monthly = (active.dropna(subset=["Close Date"]).groupby("Close Month", as_index=False)
        .agg(件数=("商談名", "count"), MRR合計=("商談MRR", "sum"), 注力案件MRR=("商談MRR", lambda s: s[cleaned.loc[s.index, "注力案件"] == 1].sum()), 加重MRR=("加重MRR", "sum"))
        .sort_values("Close Month"))
    monthly["通常案件MRR"] = monthly["MRR合計"] - monthly["注力案件MRR"]

    display_cols = ["商談名", "商談MRR", "加重MRR", "Close Date", "フェーズ", "次回アクション日", "アクション状態", "リスク理由", "推奨アクション", "次のステップ & 状況", "商談所有者", "メイン競合"]
    display_cols = [c for c in display_cols if c in cleaned.columns]
    focus = active.loc[active["注力案件"] == 1, display_cols].sort_values(["Close Date", "商談MRR"], ascending=[True, False])
    order = {"期限超過": 0, "未設定": 1, "今週対応": 2, "今週以降": 3}
    action = active[display_cols].assign(_sort=active["アクション状態"].map(order)).sort_values(["_sort", "次回アクション日", "商談MRR"], ascending=[True, True, False]).drop(columns="_sort")
    risks = active.loc[active["リスク理由"] != "通常", display_cols].sort_values(["商談MRR"], ascending=False)
    return ReportResult(raw, cleaned, monthly, focus, action, risks, _answer_cards(cleaned, today), warnings)


def prepare_scenario_deals(cleaned: pd.DataFrame) -> pd.DataFrame:
    """案件単位で人が見込みを判断するための編集用テーブルを作る。"""
    cols = [
        "商談名", "商談MRR", "Close Date", "注力案件", "フェーズ",
        "初回商談日", "次回アクション日", "次のステップ & 状況", "リスク理由",
    ]
    cols = [c for c in cols if c in cleaned.columns]
    # 初回商談を終えていない案件は着地シナリオに混ぜず、Pipelineとして別集計する。
    eligible = cleaned["初回商談日"].notna() & ~cleaned["失注"] & (cleaned["商談MRR"] > 0)
    deals = cleaned.loc[eligible, cols].copy().sort_values(["Close Date", "商談MRR"], ascending=[True, False])
    deals.insert(0, "案件ID", deals.index.astype(str))
    deals.insert(1, "見込み区分", deals["注力案件"].map({1: "min", 0: "max"}).fillna("max"))
    deals.insert(2, "判断メモ", "")
    return deals.reset_index(drop=True)


def calculate_pipeline(cleaned: pd.DataFrame, report_date: date, quarter: Optional[str] = None) -> dict[str, float | int]:
    """初回商談済みかつ失注していない案件をPipelineとして集計する。"""
    pipeline = cleaned[cleaned["初回商談日"].notna() & ~cleaned["失注"] & (cleaned["商談MRR"] > 0)].copy()
    close_period = pipeline["Close Date"].dt.to_period("M")
    current_period = pd.Timestamp(report_date).to_period("M")
    target_quarter = pd.Period(quarter, freq="Q") if quarter else current_period.asfreq("Q")
    in_quarter = close_period.map(lambda p: p.asfreq("Q") if pd.notna(p) else pd.NaT) == target_quarter
    return {
        "件数": int(len(pipeline)),
        "全体MRR": float(pipeline["商談MRR"].sum()),
        "今月MRR": float(pipeline.loc[close_period == current_period, "商談MRR"].sum()),
        "四半期MRR": float(pipeline.loc[in_quarter, "商談MRR"].sum()),
    }


def calculate_pipeline_months(cleaned: pd.DataFrame, report_date: date, quarter: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    pipeline = cleaned[cleaned["初回商談日"].notna() & ~cleaned["失注"] & (cleaned["商談MRR"] > 0)].copy()
    pipeline["月"] = pipeline["Close Date"].dt.to_period("M").astype("string")
    current = pd.Timestamp(report_date).to_period("M")
    labels = [("今月", current), ("来月", current + 1), ("再来月", current + 2)]
    rows = []
    for label, period in labels:
        subset = pipeline[pipeline["月"] == str(period)]
        target = float(MONTHLY_TARGETS.get(str(period), 0))
        value = float(subset["商談MRR"].sum())
        rows.append({"期間": label, "月": str(period), "MRR": value, "目標": target, "達成率": value / target * 100 if target else None, "件数": int(len(subset))})
    target_q = pd.Period(quarter, freq="Q")
    q_mask = pipeline["Close Date"].dt.to_period("Q") == target_q
    q_subset = pipeline[q_mask]
    q_target = float(sum(value for month, value in MONTHLY_TARGETS.items() if pd.Period(month, freq="M").asfreq("Q") == target_q))
    q_value = float(q_subset["商談MRR"].sum())
    rows.append({"期間": "クオーター", "月": str(target_q), "MRR": q_value, "目標": q_target, "達成率": q_value / q_target * 100 if q_target else None, "件数": int(len(q_subset))})
    return pd.DataFrame(rows), pipeline


def calculate_meeting_activity(cleaned: pd.DataFrame, report_date: date) -> dict[str, float | int]:
    month = pd.Timestamp(report_date).to_period("M")
    meeting_month = cleaned["初回商談日"].dt.to_period("M")
    meetings = cleaned[meeting_month == month]
    valid = meetings[~meetings["失注"]]
    total = int(len(meetings))
    valid_count = int(len(valid))
    return {
        "商談件数": total,
        "有効商談数": valid_count,
        "有効商談割合": valid_count / total * 100 if total else None,
    }


def performance_rating(achievement: float | None) -> str:
    if achievement is None or pd.isna(achievement):
        return "－"
    if achievement >= 150:
        return "5"
    if achievement >= 120:
        return "4"
    if achievement >= 100:
        return "3"
    if achievement >= 65:
        return "2"
    return "1候補"


def calculate_scenario_forecast(
    deals: pd.DataFrame,
    monthly_targets: Optional[dict[str, float]] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """案件の見込み区分から月次・四半期のmin/conservative/maxを算出する。"""
    targets = monthly_targets or MONTHLY_TARGETS
    work = deals.copy()
    work["商談MRR"] = pd.to_numeric(work["商談MRR"], errors="coerce").fillna(0).clip(lower=0)
    work["Close Date"] = pd.to_datetime(work["Close Date"], errors="coerce")
    work["Close Month"] = work["Close Date"].dt.to_period("M").astype("string")
    work["見込み区分"] = work["見込み区分"].where(work["見込み区分"].isin(SCENARIO_ORDER), "max")

    rows = []
    months = sorted(set(work["Close Month"].dropna().tolist()) | set(targets))
    for month in months:
        month_deals = work[work["Close Month"] == month]
        min_value = month_deals.loc[month_deals["見込み区分"] == "min", "商談MRR"].sum()
        conservative_value = month_deals.loc[month_deals["見込み区分"].isin(["min", "conservative"]), "商談MRR"].sum()
        max_value = month_deals.loc[month_deals["見込み区分"].isin(["min", "conservative", "max"]), "商談MRR"].sum()
        target = float(targets.get(month, 0))
        achievement = conservative_value / target * 100 if target else None
        rows.append({
            "月": month,
            "min": float(min_value),
            "conservative": float(conservative_value),
            "max": float(max_value),
            "目標": target,
            "目標対比(conservative)": achievement,
            "評価": performance_rating(achievement),
        })
    monthly = pd.DataFrame(rows)
    if monthly.empty:
        return monthly, pd.DataFrame(columns=["四半期", "min", "conservative", "max", "目標", "目標対比(conservative)", "評価"])
    monthly["四半期"] = pd.PeriodIndex(monthly["月"], freq="M").asfreq("Q").astype(str)
    quarterly = monthly.groupby("四半期", as_index=False).agg({"min": "sum", "conservative": "sum", "max": "sum", "目標": "sum"})
    quarterly["目標対比(conservative)"] = quarterly.apply(
        lambda r: r["conservative"] / r["目標"] * 100 if r["目標"] else None, axis=1
    )
    quarterly["評価"] = quarterly["目標対比(conservative)"].map(performance_rating)
    return monthly, quarterly


def _safe_excel_width(series: pd.Series, column_name: object) -> int:
    """空列・NaN・pandasの版差があっても必ず有効なExcel列幅を返す。"""
    minimum = max(len(str(column_name)) + 4, 12)
    if series.empty:
        return min(minimum, 42)
    lengths = series.map(lambda value: len(str(value)) if pd.notna(value) else 0)
    quantile = pd.to_numeric(lengths, errors="coerce").dropna().quantile(0.9)
    content_width = int(quantile) + 2 if pd.notna(quantile) else minimum
    return min(max(minimum, content_width), 42)


def export_excel(
    result: ReportResult,
    output: str | Path | BinaryIO,
    scenario_deals: Optional[pd.DataFrame] = None,
    monthly_scenarios: Optional[pd.DataFrame] = None,
    quarterly_scenarios: Optional[pd.DataFrame] = None,
) -> str | Path | BinaryIO:
    with pd.ExcelWriter(output, engine="xlsxwriter", datetime_format="yyyy-mm-dd", date_format="yyyy-mm-dd") as writer:
        pd.DataFrame([result.answer_cards]).to_excel(writer, sheet_name="Summary", index=False)
        sheets = [("Monthly Forecast", result.monthly_forecast)]
        if monthly_scenarios is not None:
            sheets.append(("Scenario Monthly", monthly_scenarios))
        if quarterly_scenarios is not None:
            sheets.append(("Scenario Quarterly", quarterly_scenarios))
        if scenario_deals is not None:
            sheets.append(("Deal Judgement", scenario_deals))
        sheets.extend([("Focus Deals", result.focus_deals), ("Next Actions", result.action_list), ("Risks", result.risks), ("Cleaned Data", result.cleaned)])
        for name, df in sheets:
            df.to_excel(writer, sheet_name=name, index=False)
        workbook = writer.book
        header = workbook.add_format({"bold": True, "bg_color": "#16324F", "font_color": "white", "border": 1})
        money = workbook.add_format({"num_format": "¥#,##0", "align": "right"})
        date_fmt = workbook.add_format({"num_format": "yyyy-mm-dd"})
        for name, df in [("Summary", pd.DataFrame([result.answer_cards])), *sheets]:
            ws = writer.sheets[name]
            ws.freeze_panes(1, 1 if name != "Summary" else 0)
            ws.autofilter(0, 0, max(len(df), 1), max(len(df.columns) - 1, 0))
            for i, col in enumerate(df.columns):
                ws.write(0, i, col, header)
                width = _safe_excel_width(df[col], col)
                fmt = money if "MRR" in str(col) else date_fmt if ("Date" in str(col) or str(col).endswith("日")) else None
                ws.set_column(i, i, width, fmt)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Salesforce週次予測レポートを生成")
    parser.add_argument("input", help="Salesforce export .xlsx/.csv")
    parser.add_argument("--output", default="weekly_report.xlsx")
    args = parser.parse_args()
    export_excel(build_report(args.input), args.output)
    print(f"created: {args.output}")


if __name__ == "__main__":
    main()
