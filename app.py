from datetime import date
from io import BytesIO

import altair as alt
import pandas as pd
import streamlit as st

from pipeline_report import (
    build_report,
    calculate_scenario_forecast,
    export_excel,
    prepare_scenario_deals,
)


st.set_page_config(page_title="Weekly Forecast", page_icon="◼", layout="wide")
st.markdown("""
<style>
  .block-container {padding-top: 2rem; max-width: 1500px;}
  [data-testid="stMetric"] {border-top: 2px solid #202124; padding-top: .8rem;}
  [data-testid="stMetricValue"] {font-size: 1.65rem;}
  .quiet {color:#5f6368; font-size:.9rem;}
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
    st.markdown("**min**：最低限見込む\n\n**conservative**：現実的な着地\n\n**max**：上振れを含む最大値")

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

st.subheader("案件別の着地判断")
st.caption("見込み区分を変更すると、下の月次・四半期サマリが即時に更新されます。初期値は注力案件=min、その他=maxです。")
edited = st.data_editor(
    st.session_state["deal_judgements"],
    key="deal_editor",
    use_container_width=True,
    hide_index=True,
    height=min(560, 84 + len(st.session_state["deal_judgements"]) * 35),
    disabled=["商談名", "商談MRR", "注力案件", "フェーズ", "次回アクション日", "次のステップ & 状況", "リスク理由"],
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

if monthly.empty:
    st.warning("集計できるClose Dateがありません。")
    st.stop()

target_months = monthly[monthly["目標"] > 0]
current = target_months.iloc[0] if not target_months.empty else monthly.iloc[0]
q_current = quarterly[quarterly["目標"] > 0].iloc[0] if (quarterly["目標"] > 0).any() else quarterly.iloc[0]

st.subheader("着地レンジ")
c1, c2, c3, c4 = st.columns(4)
c1.metric("min", f"¥{q_current['min']:,.0f}")
c2.metric("conservative", f"¥{q_current['conservative']:,.0f}")
c3.metric("max", f"¥{q_current['max']:,.0f}")
q_ratio = q_current["目標対比(conservative)"]
c4.metric("四半期目標対比", "－" if pd.isna(q_ratio) else f"{q_ratio:.0f}%", f"評価 {q_current['評価']}")
st.caption(f"{q_current['四半期']}｜目標 ¥{q_current['目標']:,.0f}｜評価はconservativeを基準")

st.subheader("月次：目標と着地レンジ")
long = monthly.melt(id_vars=["月", "目標"], value_vars=["min", "conservative", "max"], var_name="シナリオ", value_name="MRR")
base = alt.Chart(long).encode(
    y=alt.Y("月:N", sort=list(monthly["月"]), title=None),
    x=alt.X("MRR:Q", title="MRR", axis=alt.Axis(format="~s", grid=True, gridColor="#eeeeee")),
    yOffset=alt.YOffset("シナリオ:N", sort=["min", "conservative", "max"]),
    color=alt.Color("シナリオ:N", scale=alt.Scale(domain=["min", "conservative", "max"], range=["#111111", "#666666", "#c4c4c4"]), legend=alt.Legend(orient="top")),
    tooltip=["月:N", "シナリオ:N", alt.Tooltip("MRR:Q", format=",.0f")],
)
bars = base.mark_bar(size=10, cornerRadiusEnd=2)
target_ticks = alt.Chart(monthly).mark_tick(color="#111111", thickness=3, size=30).encode(
    y=alt.Y("月:N", sort=list(monthly["月"])),
    x=alt.X("目標:Q"),
    tooltip=["月:N", alt.Tooltip("目標:Q", format=",.0f")],
)
st.altair_chart((bars + target_ticks).properties(height=max(180, len(monthly) * 74)), use_container_width=True)
st.caption("黒い縦線＝月次目標。色は3段階のグレーに限定しています。")

monthly_view = monthly.copy()
for col in ["min", "conservative", "max", "目標"]:
    monthly_view[col] = monthly_view[col].map(lambda x: f"¥{x:,.0f}")
monthly_view["目標対比(conservative)"] = monthly_view["目標対比(conservative)"].map(lambda x: "－" if pd.isna(x) else f"{x:.0f}%")
st.dataframe(monthly_view.drop(columns="四半期"), use_container_width=True, hide_index=True)

st.subheader("四半期：目標対比")
quarter_view = quarterly.copy()
for col in ["min", "conservative", "max", "目標"]:
    quarter_view[col] = quarter_view[col].map(lambda x: f"¥{x:,.0f}")
quarter_view["目標対比(conservative)"] = quarter_view["目標対比(conservative)"].map(lambda x: "－" if pd.isna(x) else f"{x:.0f}%")
st.dataframe(quarter_view, use_container_width=True, hide_index=True)
with st.expander("評価基準"):
    st.write("5：150%以上｜4：120%以上｜3：100%以上｜2：65%以上100%未満｜1候補：65%未満（合理的な救済理由は判断メモで補足）")

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
