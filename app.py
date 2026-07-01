from datetime import date
from html import escape
from io import BytesIO
import importlib

import altair as alt
import pandas as pd
import streamlit as st

import pipeline_report as _pipeline_report

# Streamlit Cloud の常駐プロセスが古いモジュールを保持していても、最新版を読み直す。
_pipeline_report = importlib.reload(_pipeline_report)
build_report = _pipeline_report.build_report
calculate_meeting_activity = _pipeline_report.calculate_meeting_activity
calculate_pipeline_months = _pipeline_report.calculate_pipeline_months
calculate_scenario_forecast = _pipeline_report.calculate_scenario_forecast
export_excel = _pipeline_report.export_excel
prepare_scenario_deals = _pipeline_report.prepare_scenario_deals


st.set_page_config(page_title="Weekly Forecast", page_icon="◼", layout="wide")
st.markdown("""
<style>
  .block-container {padding-top:1.5rem; max-width:1500px;}
  .card {border:1px solid #dfe3e8; border-radius:12px; padding:17px 19px; background:#fff; min-height:145px;}
  .card-title {font-size:.85rem; color:#52606d; font-weight:800; margin-bottom:10px;}
  .period {font-size:.76rem; color:#8491a3; margin-left:6px; font-weight:500;}
  .big {font-size:2rem; font-weight:850; line-height:1.15; color:#101828;}
  .sub {font-size:.83rem; color:#667085; margin-top:6px;}
  .range-line {display:flex; justify-content:space-between; align-items:baseline; padding:5px 0; border-bottom:1px solid #f1f3f5;}
  .range-line:last-child {border-bottom:none;}
  .range-label,.range-value {font-size:.86rem; font-weight:800;}
  .min {color:#138a5b;} .conservative {color:#1769e0;} .max {color:#8b5cf6;} .pipeline {color:#d97706;}
  .not-achieved {color:#111827;} .target {font-size:.76rem; color:#667085; font-weight:500;}
  .good {color:#1769e0;} .mid {color:#d97706;} .bad {color:#d92d20;}
  .max-notice {border-left:5px solid #8b5cf6; background:#f7f3ff; border-radius:7px; padding:10px 14px; color:#5b35b5; font-weight:750; margin:.4rem 0 .8rem;}
</style>
""", unsafe_allow_html=True)

st.title("Weekly Forecast")
st.caption("Salesforceの事実 × 案件ごとの判断で、活動・Pipeline・着地を更新")

with st.sidebar:
    st.header("レポート設定")
    uploaded = st.file_uploader("Salesforceエクスポート", type=["xlsx", "xls", "csv"])
    report_date = st.date_input("基準日", value=date.today())
    st.divider()
    st.markdown("🟢 **min**：最低限\n\n🔵 **conservative**：現実的\n\n🟣 **max**：最大値\n\n🟠 **Pipeline**：商談済み・失注除外")

if not uploaded:
    st.info("左側からSalesforceのXLSXまたはCSVをアップロードしてください。")
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

top = st.container()

st.subheader("案件別の着地判断")
view_choice = st.radio("表示する見込み", ["max", "min", "conservative", "すべて"], horizontal=True, label_visibility="collapsed")
full_deals = st.session_state["deal_judgements"].copy()
if view_choice == "すべて":
    view = full_deals.sort_values(["見込み区分", "商談MRR"], ascending=[True, True])
else:
    view = full_deals[full_deals["見込み区分"] == view_choice].sort_values("商談MRR", ascending=True)
if view_choice == "max":
    st.markdown('<div class="max-notice">🟣 MAX案件｜上振れ候補をMRRの昇順で表示</div>', unsafe_allow_html=True)
elif view_choice == "min":
    st.markdown('<div class="max-notice" style="border-color:#138a5b;background:#effaf5;color:#08764b">🟢 MIN案件｜最低着地をMRRの昇順で表示</div>', unsafe_allow_html=True)

edited_view = st.data_editor(
    view, key=f"deal_editor_{view_choice}", use_container_width=True, hide_index=True,
    height=min(510, 84 + max(len(view), 1) * 35),
    disabled=["案件ID", "商談名", "商談MRR", "注力案件", "フェーズ", "初回商談日", "次回アクション日", "次のステップ & 状況", "リスク理由"],
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
if not edited_view.empty:
    updated = full_deals.set_index("案件ID")
    changed = edited_view.set_index("案件ID")
    updated.update(changed)
    full_deals = updated.reset_index()
st.session_state["deal_judgements"] = full_deals

monthly, quarterly = calculate_scenario_forecast(full_deals)
activity = calculate_meeting_activity(result.cleaned, report_date)
current_period = pd.Timestamp(report_date).to_period("M")
current_q = current_period.asfreq("Q")
target_qs = quarterly[quarterly["目標"] > 0]
display_q = str(target_qs.iloc[0]["四半期"]) if not target_qs.empty and quarterly.loc[quarterly["四半期"] == str(current_q), "目標"].sum() == 0 else str(current_q)
pipeline_summary, pipeline_deals = calculate_pipeline_months(result.cleaned, report_date, display_q)

def attainment_class(ratio):
    if ratio is None or pd.isna(ratio): return "not-achieved"
    if ratio >= 80: return "good"
    if ratio <= 60: return "bad"
    return "mid"

def scenario_card(title, period, row):
    target = float(row.get("目標", 0))
    target_text = f" / ¥{target:,.0f}" if target else " / 目標未設定"
    lines = []
    for key, label in [("min", "MIN"), ("conservative", "CONSERVATIVE"), ("max", "MAX")]:
        value = float(row.get(key, 0))
        value_class = key if target > 0 and value >= target else "not-achieved"
        lines.append(f'<div class="range-line"><span class="range-label {key}">{label}</span><span class="range-value {value_class}">¥{value:,.0f}<span class="target">{target_text}</span></span></div>')
    return f'<div class="card"><div class="card-title">{escape(title)}<span class="period">{escape(period)}</span></div>{"".join(lines)}</div>'

def scenario_row(month_or_q, quarterly_row=False):
    source_df = quarterly if quarterly_row else monthly
    key = "四半期" if quarterly_row else "月"
    match = source_df[source_df[key] == str(month_or_q)]
    return match.iloc[0] if not match.empty else pd.Series({"min":0,"conservative":0,"max":0,"目標":0})

period_specs = [("今月", current_period, False), ("来月", current_period + 1, False), ("再来月", current_period + 2, False), ("クオーター", display_q, True)]

with top:
    st.subheader("今月の商談活動")
    valid_ratio = activity["有効商談割合"]
    r_text = "－" if valid_ratio is None else f"{valid_ratio:.0f}%"
    r_class = attainment_class(valid_ratio)
    x1, x2 = st.columns(2)
    x1.markdown(f'<div class="card"><div class="card-title">今月の商談件数<span class="period">{current_period}</span></div><div class="big">{activity["商談件数"]}件</div><div class="sub">初回商談を実施した企業数（失注を含む）</div></div>', unsafe_allow_html=True)
    x2.markdown(f'<div class="card"><div class="card-title">有効商談</div><div class="big {r_class}">{activity["有効商談数"]}件 <span style="font-size:1.25rem">（{r_text}）</span></div><div class="sub">商談実施済みのうち失注していない企業</div></div>', unsafe_allow_html=True)

    st.subheader("Pipeline")
    pcols = st.columns(4)
    for col, (_, row) in zip(pcols, pipeline_summary.iterrows()):
        ratio = row["達成率"]
        ratio_text = "－" if pd.isna(ratio) else f"{ratio:.0f}%"
        col.markdown(f'<div class="card"><div class="card-title">{row["期間"]}<span class="period">{row["月"]}</span></div><div class="big pipeline">¥{row["MRR"]:,.0f}</div><div class="sub">{row["件数"]}件　目標 ¥{row["目標"]:,.0f}（<b class="{attainment_class(ratio)}">{ratio_text}</b>）</div></div>', unsafe_allow_html=True)

    st.subheader("着地レンジ")
    rcols = st.columns(4)
    for col, (label, period, is_q) in zip(rcols, period_specs):
        col.markdown(scenario_card(label, str(period), scenario_row(period, is_q)), unsafe_allow_html=True)

st.markdown("#### Pipeline案件")
pipeline_choice = st.radio("Pipeline期間", pipeline_summary["期間"].tolist(), horizontal=True, label_visibility="collapsed")
selected_pipeline = pipeline_summary[pipeline_summary["期間"] == pipeline_choice].iloc[0]
if pipeline_choice == "クオーター":
    mask = pipeline_deals["Close Date"].dt.to_period("Q").astype(str) == display_q
else:
    mask = pipeline_deals["Close Date"].dt.to_period("M").astype(str) == selected_pipeline["月"]
pview = pipeline_deals.loc[mask, [c for c in ["商談名", "商談MRR", "Close Date", "フェーズ", "次のステップ & 状況"] if c in pipeline_deals.columns]].sort_values("商談MRR", ascending=True)
st.dataframe(pview, use_container_width=True, hide_index=True)

st.subheader("月次：目標達成率と着地レンジ")
st.caption("縦棒は目標達成率（150%で上限表示）。金額差が大きくても、達成度を同じ尺度で比較できます。月をクリックすると案件を絞り込みます。")
long = monthly.melt(id_vars=["月", "目標"], value_vars=["min", "conservative", "max"], var_name="シナリオ", value_name="MRR")
long["達成率"] = long.apply(lambda r: r["MRR"] / r["目標"] * 100 if r["目標"] else 0, axis=1)
long["表示達成率"] = long["達成率"].clip(upper=150)
select_month = alt.selection_point(fields=["月"], name="month_select", on="click", clear="dblclick")
colors = alt.Scale(domain=["min", "conservative", "max"], range=["#17a673", "#2478e5", "#8b5cf6"])
bars = alt.Chart(long).mark_bar(size=28, cornerRadiusTopLeft=5, cornerRadiusTopRight=5).encode(
    x=alt.X("月:N", sort=list(monthly["月"]), title=None, axis=alt.Axis(labelAngle=0, labelFontSize=13, labelFontWeight="bold")),
    xOffset=alt.XOffset("シナリオ:N", sort=["min", "conservative", "max"]),
    y=alt.Y("表示達成率:Q", title="目標達成率", scale=alt.Scale(domain=[0, 150]), axis=alt.Axis(labelExpr="datum.value + '%'", gridColor="#e9edf2")),
    color=alt.Color("シナリオ:N", scale=colors, legend=alt.Legend(orient="top", title=None)),
    opacity=alt.condition(select_month, alt.value(1), alt.value(.86)),
    tooltip=["月:N", "シナリオ:N", alt.Tooltip("MRR:Q", format=",.0f"), alt.Tooltip("達成率:Q", format=".0f")],
).add_params(select_month)
target_rule = alt.Chart(pd.DataFrame({"y":[100]})).mark_rule(color="#172b4d", strokeWidth=2, strokeDash=[6,4]).encode(y="y:Q")
chart = (bars + target_rule).properties(height=460)
event = st.altair_chart(chart, use_container_width=True, on_select="rerun", selection_mode="month_select", key="monthly_vertical_chart")
selected_month = None
try:
    chosen = event.selection.get("month_select", [])
    if chosen: selected_month = chosen[0].get("月")
except (AttributeError, KeyError, IndexError, TypeError):
    pass
if not selected_month:
    active_months = monthly.loc[monthly["max"] > 0, "月"].tolist()
    selected_month = active_months[0] if active_months else str(current_period)
st.markdown(f"#### {selected_month} の着地案件")
selected_deals = full_deals[pd.to_datetime(full_deals["Close Date"], errors="coerce").dt.strftime("%Y-%m") == selected_month]
st.dataframe(selected_deals[[c for c in ["見込み区分", "商談名", "商談MRR", "Close Date", "フェーズ", "次のステップ & 状況", "判断メモ"] if c in selected_deals.columns]], use_container_width=True, hide_index=True)

with st.expander("月次・四半期サマリ"):
    st.dataframe(monthly, use_container_width=True, hide_index=True)
    st.dataframe(quarterly, use_container_width=True, hide_index=True)

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
export_excel(result, buffer, scenario_deals=full_deals, monthly_scenarios=monthly, quarterly_scenarios=quarterly)
st.download_button("判断込みExcelレポートをダウンロード", buffer.getvalue(), file_name=f"weekly_forecast_{report_date:%Y%m%d}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
