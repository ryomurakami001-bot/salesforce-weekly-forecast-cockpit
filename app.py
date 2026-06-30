from datetime import date
from io import BytesIO

import pandas as pd
import streamlit as st

from pipeline_report import build_report, export_excel


st.set_page_config(page_title="Weekly Forecast Cockpit", page_icon="🎯", layout="wide")
st.title("Weekly Forecast Cockpit")
st.caption("Salesforceの週次スナップショットから「いつ・いくら・どの程度確からしいか」を即答")

with st.sidebar:
    st.header("レポート設定")
    uploaded = st.file_uploader("Salesforceエクスポート", type=["xlsx", "xls", "csv"])
    report_date = st.date_input("基準日", value=date.today())

if not uploaded:
    st.info("左側からSalesforceのXLSXまたはCSVをアップロードしてください。元ファイルは変更しません。")
    st.stop()

try:
    result = build_report(uploaded, today=report_date)
except Exception as exc:
    st.error(f"取込に失敗しました: {exc}")
    st.stop()

for warning in result.warnings:
    st.warning(warning)

cards = result.answer_cards
row1 = st.columns(4)
row1[0].metric("今月の見込み", f"¥{cards['今月MRR']:,.0f}")
row1[1].metric("来月の見込み", f"¥{cards['来月MRR']:,.0f}")
row1[2].metric("今四半期の見込み", f"¥{cards['今四半期MRR']:,.0f}")
row1[3].metric("今四半期・加重", f"¥{cards['今四半期加重MRR']:,.0f}")
row2 = st.columns(3)
row2[0].metric("全案件MRR", f"¥{cards['総MRR']:,.0f}")
row2[1].metric("注力案件MRR", f"¥{cards['注力案件MRR']:,.0f}")
row2[2].metric("要フォロー", f"{cards['要フォロー件数']}件")

tab_month, tab_focus, tab_action, tab_risk, tab_raw = st.tabs(["月別見込み", "注力案件", "次回アクション", "リスク / 停滞", "Cleaned Data"])
with tab_month:
    st.dataframe(result.monthly_forecast, use_container_width=True, hide_index=True)
    if not result.monthly_forecast.empty:
        chart = result.monthly_forecast.set_index("Close Month")[["MRR合計", "注力案件MRR", "加重MRR"]]
        st.bar_chart(chart)
with tab_focus:
    st.dataframe(result.focus_deals, use_container_width=True, hide_index=True)
with tab_action:
    status = st.multiselect("状態", ["期限超過", "未設定", "今週対応", "今週以降"], default=["期限超過", "未設定", "今週対応"])
    view = result.action_list[result.action_list["アクション状態"].isin(status)] if status else result.action_list
    st.dataframe(view, use_container_width=True, hide_index=True)
with tab_risk:
    st.dataframe(result.risks, use_container_width=True, hide_index=True)
with tab_raw:
    st.dataframe(result.cleaned, use_container_width=True, hide_index=True)

buffer = BytesIO()
export_excel(result, buffer)
st.download_button("Excelレポートをダウンロード", buffer.getvalue(), file_name=f"weekly_forecast_{report_date:%Y%m%d}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
