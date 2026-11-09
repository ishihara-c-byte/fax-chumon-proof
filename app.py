import calendar
import datetime as dt
import io
from dataclasses import asdict

import pandas as pd
import streamlit as st

from proof_core import DayInfo, MenuMaster, check_page, parse_fax, parse_menu

st.set_page_config(page_title="FAX注文書 校正ツール", page_icon="📠", layout="wide")
st.title("📠 FAX注文書 校正ツール")
st.caption("中面PDFとカレンダーを正として、FAX注文書PDF(全店舗分)を照合します。")

with st.expander("チェックする内容", expanded=False):
    st.markdown(
        """
- **① 年・月・日付**:左上の年・月、期間(例: 12/1〜12/31)
- **② 曜日**:各日付が正しい曜日の列にあるか、抜けや前月・翌月の日付がないか、店休日(グレーの日付)
- **③ 旗日の文字**:フェア名・プレミアム・スペシャル、日付の黒地、贅沢版の混ぜご飯
- **④ イベントWeek**:表記と期間

店休日は店舗ごとに異なることがあるため「要確認」として表示します。
"""
    )


@st.cache_data(show_spinner=False)
def load_menu(data: bytes) -> MenuMaster:
    return parse_menu(io.BytesIO(data))


def load_fax(data: bytes, name: str):
    """読み取り結果はセッション内に保存し、同じファイルは読み直さない"""
    import hashlib
    key = hashlib.md5(data).hexdigest()
    store = st.session_state.setdefault("fax_cache", {})
    if key not in store:
        bar = st.progress(0.0, text=f"{name} を読み取り中…")

        def prog(i, n):
            bar.progress(i / n, text=f"{name} を読み取り中… {i}/{n}ページ")

        store[key] = parse_fax(data, progress=prog)
        bar.empty()
    return store[key]


# ------------------------------------------------------------ 1. 中面
st.header("1. 中面PDF(正しいデータ)")
menu_file = st.file_uploader("メニューパンフレット中面のPDF", type="pdf", key="menu")
if not menu_file:
    st.info("中面PDFをアップロードしてください。")
    st.stop()

master = load_menu(menu_file.getvalue())
for w in master.warnings:
    st.warning(w)

today = dt.date.today()
c1, c2 = st.columns(2)
month = c1.selectbox("月", list(range(1, 13)),
                     index=(master.month or today.month) - 1)
default_year = today.year + (1 if (master.month or today.month) < today.month else 0)
year = c2.number_input("年", min_value=2020, max_value=2100, value=default_year, step=1)
last = calendar.monthrange(int(year), int(month))[1]

st.subheader("中面から読み取った内容(修正できます)")
rows = []
for d in range(1, last + 1):
    info = master.days.get(d, DayInfo(d))
    wd = "月火水木金土日"[calendar.weekday(int(year), int(month), d)]
    rows.append({"日": d, "曜日": wd, "旗日の文字": info.flag,
                 "混ぜご飯": info.mazegohan, "店休日": info.closed})
edited = st.data_editor(
    pd.DataFrame(rows), hide_index=True, use_container_width=True, height=300,
    disabled=["日", "曜日"],
    column_config={
        "旗日の文字": st.column_config.TextColumn(help="例: スペシャル / プレミアム / スタミナフェア"),
    },
)

c1, c2, c3 = st.columns([1, 1, 2])
has_event = c1.checkbox("イベントWeekあり", value=master.event_range is not None)
ev_s, ev_e = master.event_range or (1, 1)
event_name = ""
if has_event:
    ev_s = c2.number_input("開始日", 1, last, ev_s)
    ev_e = c2.number_input("終了日", 1, last, ev_e)
    event_name = c3.text_input("イベント名(入力すると名前もチェック)", value="",
                               placeholder="例: クリスマスWeek")

# 編集結果を正データに反映
fixed = MenuMaster(month=int(month))
for r in edited.to_dict("records"):
    flag = (r["旗日の文字"] or "").strip()
    kind = "premium" if flag == "プレミアム" else "special" if flag == "スペシャル" else ("fair" if flag else "")
    fixed.days[int(r["日"])] = DayInfo(int(r["日"]), flag=flag, kind=kind,
                                      mazegohan=bool(r["混ぜご飯"]), closed=bool(r["店休日"]))
fixed.event_range = (int(ev_s), int(ev_e)) if has_event else None

# ------------------------------------------------------------ 2. FAX注文書
st.header("2. FAX注文書PDF(校正対象)")
fax_files = st.file_uploader("FAX注文書のPDF(複数可)", type="pdf", accept_multiple_files=True, key="fax")
if not fax_files:
    st.info("FAX注文書PDFをアップロードしてください。")
    st.stop()

if st.button("校正をはじめる", type="primary"):
    st.session_state["run"] = True
if not st.session_state.get("run"):
    st.stop()

issues, summary = [], []
for f in fax_files:
    pages = load_fax(f.getvalue(), f.name)
    for fp in pages:
        page_issues = check_page(fp, fixed, int(year), int(month), event_name)
        for i in page_issues:
            d = asdict(i)
            d["file"] = f.name
            issues.append(d)
        n_err = sum(i.level == "エラー" for i in page_issues)
        n_chk = sum(i.level == "要確認" for i in page_issues)
        summary.append({
            "ファイル": f.name, "ページ": fp.page_no, "店舗番号": fp.store_no,
            "店舗": fp.store_name, "種類": fp.variant,
            "判定": "❌ エラー" if n_err else ("⚠️ 要確認" if n_chk else "✅ OK"),
            "エラー": n_err, "要確認": n_chk,
        })

# ------------------------------------------------------------ 3. 結果
st.header("3. 結果")
sdf = pd.DataFrame(summary)
idf = pd.DataFrame(issues)
c1, c2, c3, c4 = st.columns(4)
c1.metric("ページ数", len(sdf))
c2.metric("OK", int((sdf["判定"] == "✅ OK").sum()))
c3.metric("エラーのあるページ", int((sdf["エラー"] > 0).sum()))
c4.metric("要確認のあるページ", int(((sdf["エラー"] == 0) & (sdf["要確認"] > 0)).sum()))

if not idf.empty:
    idf = idf.rename(columns={"file": "ファイル", "page": "ページ", "store": "店舗", "item": "項目",
                              "day": "日", "expected": "正しい内容", "actual": "注文書の内容", "level": "区分"})
    idf = idf[["区分", "ファイル", "ページ", "店舗", "項目", "日", "正しい内容", "注文書の内容"]]

    # 全ページ共通のエラー(テンプレート由来)はまとめて表示
    n_pages = len(sdf)
    key = ["区分", "項目", "日", "正しい内容", "注文書の内容"]
    common = idf.groupby(key).size().reset_index(name="ページ数")
    common = common[common["ページ数"] == n_pages]
    if not common.empty and n_pages > 1:
        st.subheader("全ページ共通の指摘")
        st.dataframe(common, hide_index=True, use_container_width=True)
        idf_rest = idf.merge(common[key], on=key, how="left", indicator=True)
        idf_rest = idf_rest[idf_rest["_merge"] == "left_only"].drop(columns="_merge")
    else:
        idf_rest = idf

    st.subheader("ページごとの指摘")
    levels = st.multiselect("表示する区分", ["エラー", "要確認"], default=["エラー", "要確認"])
    st.dataframe(idf_rest[idf_rest["区分"].isin(levels)], hide_index=True, use_container_width=True)
else:
    st.success("指摘はありませんでした 🎉")

st.subheader("ページ一覧")
st.dataframe(sdf, hide_index=True, use_container_width=True)

buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as xw:
    sdf.to_excel(xw, sheet_name="ページ一覧", index=False)
    (idf if not idf.empty else pd.DataFrame()).to_excel(xw, sheet_name="指摘一覧", index=False)
st.download_button("結果をExcelでダウンロード", buf.getvalue(),
                   file_name=f"校正結果_{year}年{month}月.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
