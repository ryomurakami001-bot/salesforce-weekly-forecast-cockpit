from datetime import date
from html import escape
from io import BytesIO

import altair as alt
import pandas as pd
import streamlit as st

from pipeline_report import (
    build_report,
    calculate_pipeline,
    calculate_scenario_forecast,
    export_excel,
    prepare_scenario_deals,
)


st.set_page_config(page_title="Weekly Forecast", page_icon="◼", layout="wide")
st.markdown("""
<style>
  .block-container {padding-top: 1.6rem; max-width: 1500px;}
  .range-card {border:1px solid #dfe3e8; border-radius:12px; padding:18px 20px; background:white; min-height:178px;}
  .range-title {font-size:.92rem; color:#52606d; font-weight:700; margin-bottom:11px;}
  .range-period {font-size:.78rem; color:#7b8794; margin-left:6px; font-weight:400;}
  .range-line {display:flex; justify-content:space-between; align-items:baseline; padding:6px 0; border-bottom:1px solid #f1f3f5;}
  .range-line:last-child {border-bottom:none;}
  .range-label {font-size:.86rem; font-weight:700;}
  .range-value {font-size:1.02rem; font-weight:800;}
  .min {color:#138a5b;} .conservative {color:#1769e0;} .max {color:#8b5cf6;} .pipeline {color:#d97706;}
  .target {font-size:.8rem; color:#667085; font-weight:500;}
  .achievement {border-radius:10px; padding:18px 20px; min-height:178px; border:1px solid #dfe3e8; background:#f8fafc;}
  .achievement-value {font-size:2.2rem; font-weight:850; line-height:1.1; margin:18px 0 8px;}
  .good {color:#1769e0;} .warn {color:#d97706;} .bad {color:#d92d20;}
  .section-note {color:#667085; font-size:.88rem;}
</style>
""", unsafe_allow_html=True)

st.title("Weekly Forecast")
st.caption("Salesforceの事実 × 案件ごとの判断で、着地レンジと目標対比を更新")

with st.sidebar:
    st.header("レポート設定")
    uploaded = st.file_uploader("Salesforceエクスポート", type=["xlsx", "xls", "csv"])
    report_date = st.date_input("基準日", value=date.today())
    st.divider()
    st.caption("見込み区分")
    st.markdown("🟢 **min**：最低限見込む\n\n🔵 **conservative**：現実的な着地\n\n🟣 **max**：上振れを含む最大値\n\n🟠 **Pipeline**：初回商談前")

if not uploaded:
    st.info("左側からSalesforceのXLSXまたはCSVをアップロードしてください。元ファイルは変更しません。")
    st.stop()

try:
    file_bytes = uploaded.getvalue()
    source = BytesIO(file_bytes)
    source.name = uploaded.name
    result = build_report(source, today=report_date)
except Exception as exc:
    st.error(f"取込に失敗しました: {exc}")
    st.stop()

for warning in result.warnings:
    st.warning(warning)

file_key = f"{uploaded.name}:{len(file_bytes)}"
if st.session_state.get("forecast_file_key") != file_key:
    st.session_state["forecast_file_key"] = file_key
    st.session_state["deal_judgements"] = prepare_scenario_deals(result.cleaned)

# 中身は編集後に描画するが、画面上は常に最上段に置く。
top_range = st.container()

st.subheader("案件別の着地判断")
st.caption("初回商談済みの案件だけを着地シナリオに表示します。見込み区分の変更は上の着地レンジへ即時反映されます。")
edited = st.data_editor(
    st.session_state["deal_judgements"], key="deal_editor", use_container_width=True, hide_index=True,
    height=min(560, 84 + len(st.session_state["deal_judgements"]) * 35),
    disabled=["商談名", "商談MRR", "注力案件", "フェーズ", "初回商談日", "次回アクション日", "次のステップ & 状況", "リスク理由"],
    column_order=["見込み区分", "商談名", "商談MRR", "Close Date", "フェーズ", "次回アクション日", "次のステップ & 状況", "判断メモ"],
    column_config={
        "見込み区分": st.column_config.SelectboxColumn("見込み", options=["min", "conservative", "max", "除外"], required=True, width="small"),
        "商談名": st.column_config.TextColumn("案件名", width="medium"),
        "商談MRR": st.column_config.NumberColumn("MRR", format="¥%d", width="small"),
        "Close Date": st.column_config.DateColumn("着地予定", format="YYYY-MM-DD"),
        "次のステップ & 状況": st.column_config.TextColumn("次のステップ & 状況", width="large"),
        "判断メモ": st.column_config.TextColumn("村上判断メモ", width="medium"),
    },
)
st.session_state["deal_judgements"] = edited
monthly, quarterly = calculate_scenario_forecast(edited)
pipeline = calculate_pipeline(result.cleaned, report_date)

current_month = pd.Timestamp(report_date).strftime("%Y-%m")
current_quarter = str(pd.Timestamp(report_date).to_period("Q"))
month_row = monthly[monthly["月"] == current_month]
month_row = month_row.iloc[0] if not month_row.empty else pd.Series({"min": 0, "conservative": 0, "max": 0, "目標": 0})
quarter_row = quarterly[quarterly["四半期"] == current_quarter]
target_quarters = quarterly[quarterly["目標"] > 0]
if quarter_row.empty and not target_quarters.empty:
    quarter_row = target_quarters.iloc[[0]]
quarter_row = quarter_row.iloc[0] if not quarter_row.empty else pd.Series({"四半期": current_quarter, "min": 0, "conservative": 0, "max": 0, "目標": 0, "目標対比(conservative)": None, "評価": "－"})

def range_card(title, period, row):
    target = float(row.get("目標", 0))
    target_text = f" / ¥{target:,.0f}" if target else " / 目標未設定"
    lines = "".join(
        f'<div class="range-line"><span class="range-label {key}">{label}</span><span class="range-value {key}">¥{float(row.get(key, 0)):,.0f}<span class="target">{target_text}</span></span></div>'
        for key, label in [("min", "MIN"), ("conservative", "CONSERVATIVE"), ("max", "MAX")]
    )
    return f'<div class="range-card"><div class="range-title">{escape(title)}<span class="range-period">{escape(period)}</span></div>{lines}</div>'

ratio = quarter_row.get("目標対比(conservative)")
ratio_value = None if pd.isna(ratio) else float(ratio)
ratio_class = "good" if ratio_value is not None and ratio_value >= 80 else "bad" if ratio_value is not None and ratio_value <= 60 else "warn"
ratio_text = "－" if ratio_value is None else f"{ratio_value:.0f}%"

with top_range:
    st.subheader("着地レンジ")
    a, b, c, d = st.columns([1.25, 1.25, 1, .8])
    a.markdown(range_card("今月", current_month, month_row), unsafe_allow_html=True)
    b.markdown(range_card("四半期", str(quarter_row.get("四半期", current_quarter)), quarter_row), unsafe_allow_html=True)
    c.markdown(f'''<div class="range-card"><div class="range-title">PIPELINE<span class="range-period">初回商談前</span></div>
        <div class="range-line"><span class="range-label pipeline">今月</span><span class="range-value pipeline">¥{pipeline['今月MRR']:,.0f}</span></div>
        <div class="range-line"><span class="range-label pipeline">四半期</span><span class="range-value pipeline">¥{pipeline['四半期MRR']:,.0f}</span></div>
        <div class="range-line"><span class="range-label pipeline">全体</span><span class="range-value pipeline">¥{pipeline['全体MRR']:,.0f}<span class="target"> / {pipeline['件数']}件</span></span></div></div>''', unsafe_allow_html=True)
    d.markdown(f'''<div class="achievement"><div class="range-title">Q目標対比<span class="range-period">conservative</span></div>
        <div class="achievement-value {ratio_class}">{ratio_text}</div><div class="range-label {ratio_class}">評価 {quarter_row.get('評価', '－')}</div></div>''', unsafe_allow_html=True)
    st.caption("目標対比：80%以上は青、60%以下は赤、その間はオレンジ。着地数字 / 目標数字で表示しています。")

st.subheader("月次：目標と着地レンジ")
st.caption("各月をクリックすると、その月の案件一覧を下に表示します。黒い縦線は月次目標です。")
long = monthly.melt(id_vars=["月", "目標"], value_vars=["min", "conservative", "max"], var_name="シナリオ", value_name="MRR")
month_select = alt.selection_point(fields=["月"], name="month_select", on="click", clear="dblclick")
color_scale = alt.Scale(domain=["min", "conservative", "max"], range=["#17a673", "#2478e5", "#8b5cf6"])
bars = alt.Chart(long).mark_bar(size=30, cornerRadiusEnd=5).encode(
    y=alt.Y("シナリオ:N", sort=["min", "conservative", "max"], title=None, axis=alt.Axis(labelFontSize=13, labelFontWeight="bold")),
    x=alt.X("MRR:Q", title="MRR", axis=alt.Axis(format="~s", grid=True, gridColor="#e8edf3", labelFontSize=12)),
    color=alt.Color("シナリオ:N", scale=color_scale, legend=None),
    opacity=alt.condition(month_select, alt.value(1), alt.value(.78)),
    tooltip=["月:N", "シナリオ:N", alt.Tooltip("MRR:Q", format=",.0f"), alt.Tooltip("目標:Q", format=",.0f")],
).add_params(month_select)
labels = alt.Chart(long).mark_text(align="left", dx=8, fontSize=13, fontWeight="bold").encode(
    y=alt.Y("シナリオ:N", sort=["min", "conservative", "max"]), x="MRR:Q", text=alt.Text("MRR:Q", format=",.0f"), color=alt.Color("シナリオ:N", scale=color_scale, legend=None)
)
target_line = alt.Chart(long).mark_rule(color="#172b4d", strokeWidth=3, strokeDash=[6, 4]).encode(x=alt.X("mean(目標):Q"))
chart = (bars + labels + target_line).properties(width=900, height=165).facet(
    row=alt.Row("月:N", sort=list(monthly["月"]), title=None, header=alt.Header(labelFontSize=17, labelFontWeight="bold", labelColor="#172b4d", labelPadding=12))
).resolve_scale(x="shared")
event = st.altair_chart(chart, use_container_width=True, on_select="rerun", selection_mode="month_select", key="monthly_range_chart")

selected_month = None
try:
    selection = event.selection.get("month_select", [])
    if selection:
        selected_month = selection[0].get("月")
except (AttributeError, KeyError, IndexError, TypeError):
    selected_month = None
if not selected_month:
    active_months = monthly.loc[monthly["max"] > 0, "月"].tolist()
    selected_month = active_months[0] if active_months else monthly.iloc[0]["月"]

st.markdown(f"#### {selected_month} の案件")
selected_deals = edited[pd.to_datetime(edited["Close Date"], errors="coerce").dt.strftime("%Y-%m") == selected_month]
st.dataframe(selected_deals[[c for c in ["見込み区分", "商談名", "商談MRR", "Close Date", "フェーズ", "次のステップ & 状況", "判断メモ"] if c in selected_deals.columns]], use_container_width=True, hide_index=True)

monthly_view = monthly.copy()
for col in ["min", "conservative", "max", "目標"]:
    monthly_view[col] = monthly_view[col].map(lambda x: f"¥{x:,.0f}")
monthly_view["目標対比(conservative)"] = monthly_view["目標対比(conservative)"].map(lambda x: "－" if pd.isna(x) else f"{x:.0f}%")
with st.expander("月次サマリ表"):
    st.dataframe(monthly_view.drop(columns="四半期"), use_container_width=True, hide_index=True)

with st.expander("四半期サマリ・評価基準"):
    quarter_view = quarterly.copy()
    for col in ["min", "conservative", "max", "目標"]:
        quarter_view[col] = quarter_view[col].map(lambda x: f"¥{x:,.0f}")
    quarter_view["目標対比(conservative)"] = quarter_view["目標対比(conservative)"].map(lambda x: "－" if pd.isna(x) else f"{x:.0f}%")
    st.dataframe(quarter_view, use_container_width=True, hide_index=True)
    st.write("5：150%以上｜4：120%以上｜3：100%以上｜2：65%以上100%未満｜1候補：65%未満")

tab_action, tab_risk, tab_raw = st.tabs(["次回アクション", "リスク / 停滞", "Cleaned Data"])
with tab_action:
    status = st.multiselect("状態", ["期限超過", "未設定", "今週対応", "今週以降"], default=["期限超過", "未設定", "今週対応"])
    view = result.action_list[result.action_list["アクション状態"].isin(status)] if status else result.action_list
    st.dataframe(view, use_container_width=True, hide_index=True)
with tab_risk:
    st.dataframe(result.risks, use_container_width=True, hide_index=True)
with tab_raw:
    st.dataframe(result.cleaned, use_container_width=True, hide_index=True)

buffer = BytesIO()
export_excel(result, buffer, scenario_deals=edited, monthly_scenarios=monthly, quarterly_scenarios=quarterly)
st.download_button("判断込みExcelレポートをダウンロード", buffer.getvalue(), file_name=f"weekly_forecast_{report_date:%Y%m%d}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
