# Salesforce Weekly Forecast Cockpit

Salesforceの週次エクスポートから、「いつ・いくら・どの程度確からしいか」と、今追うべき案件を即答するローカルアプリです。Salesforceを日々の正本とし、このアプリは週次レビュー専用に使います。

## 使い方

Python 3.10以上を想定しています。

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

画面左からSalesforceのCSV/XLSXをアップロードします。Excelレポートも画面からダウンロードできます。

コマンドラインだけでExcelを作る場合:

```bash
python pipeline_report.py salesforce_export.xlsx --output weekly_report.xlsx
```

## 最低限必要な列

- 商談名
- 商談MRR
- Close Date

「注力案件」「次回アクション日」「フェーズ」などがあれば分析が充実します。日本語・一部英語の代表的な別名は自動で吸収します。CSVはUTF-8とShift-JISに対応します。

## 見込みの考え方

- 通常見込み: Close Dateの月に商談MRRを全額計上
- 加重見込み: 売上予測カテゴリがあれば優先し、なければフェーズから確率を設定
- 失注・見送り: フェーズ名から判定して除外
- MRRが0以下: 集計から除外
- 要フォロー: 次回アクション未設定、期限超過、フェーズ14日以上、Close Date未設定

確率は `pipeline_report.py` の `PHASE_RULES` / `FORECAST_WEIGHTS` で自社定義に合わせて変更できます。

## 週次運用のおすすめ

毎週同じ曜日にSalesforceレポートを保存し、ファイル名に日付を入れます。会議では「今月」「来月」「今四半期・加重」「要フォロー」の順に確認し、終了後の更新はSalesforce側へ戻します。
