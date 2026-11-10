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
- **② 曜日**:各日付が正しい曜日の列にあるか、抜けや前月・翌月の日付がないか
- **③ 旗日の文字**:フェア名・プレミアム・スペシャル(と中華などの表記)、日付の黒地、贅沢版の混ぜご飯・ふりかけ
- **④ イベントWeek**:表記と期間

店休日(グレーの日付)は店舗ごとに違うためチェックしません。店休日のマスは③も対象外です。
"""
    )


def load_menu(data: bytes) -> MenuMaster:
    # 中面は1ページだけで読み取りが速いため、キャッシュしない
    # (キャッシュすると、ツールを更新しても古い読み取り結果が残ってしまう)
    return parse_menu(io.BytesIO(data))


def load_fax(data: bytes, name: str):
    """読み取り結果はセッション内に保存し、同じファイルは読み直さない"""
    import hashlib
    import proof_core
    code = open(proof_core.__file__, "rb").read()  # ツールが更新されたら読み直す
    key = hashlib.md5(data + code).hexdigest()
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
    rows.append({"日": d, "曜日": wd, "旗日の文字": info.flag, "その他の表記": info.label,
                 "贅沢のご飯": info.rice, "店休日": info.closed})
edited = st.data_editor(
    pd.DataFrame(rows), hide_index=True, use_container_width=True, height=300,
    disabled=["日", "曜日"],
    column_config={
        "旗日の文字": st.column_config.TextColumn(help="日付が黒地になる日。例: スペシャル / プレミアム / スタミナフェア"),
        "その他の表記": st.column_config.TextColumn(help="日付は白地のまま入る表記。例: 中華"),
        "贅沢のご飯": st.column_config.SelectboxColumn(options=["", "混ぜご飯", "ふりかけ"],
                                                    help="贅沢版に入る「混ぜご飯」「ふりかけ」"),
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
                                      rice=(r["贅沢のご飯"] or "").strip(),
                                      label=(r["その他の表記"] or "").strip(),
                                      closed=bool(r["店休日"]))
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

def describe(i) -> str:
    """指摘を1行の文章にする"""
    item = i.item[1:] if i.item[:1] in "①②③④" else i.item
    if i.item == "①年月日" and "/" in i.expected:
        item = "期間"
    elif i.item == "①年月日" and "月" in i.expected:
        item = "月"
    elif i.item == "①年月日":
        item = "年"
    head = f"【{item}】" + (f"{i.day}日 " if i.day and i.day.isdigit() else (f"{i.day} " if i.day else ""))
    if i.item == "読み取り":
        return head + i.actual
    if i.item == "②曜日" and i.actual.endswith(("列にあります", "表示されています", "見つかりません")):
        return head + f"{i.actual}(正: {i.expected})"
    return head + f"「{i.actual}」→ 正しくは「{i.expected}」"


# 校正した項目(ページ一覧の列)と、対応する指摘
CHECK_COLS = {
    "①年月日": ["①年月日"],
    "②曜日": ["②曜日", "読み取り"],
    "③旗日": ["③旗日"],
    "③混ぜご飯": ["③混ぜご飯"],
    "④イベント": ["④イベント"],
}

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
        bad = {i.item for i in page_issues}
        marks = {col: ("❌" if any(it in bad for it in items) else "✅")
                 for col, items in CHECK_COLS.items()}
        if not fp.zeitaku:
            marks["③混ぜご飯"] = "✅(なし)" if marks["③混ぜご飯"] == "✅" else "❌"
        if fixed.event_range is None and marks["④イベント"] == "✅":
            marks["④イベント"] = "✅(なし)"
        summary.append({
            "判定": "❌ エラー" if n_err else "✅ OK",
            "ページ": fp.page_no, "店舗": fp.label, "種類": fp.variant,
            **marks,
            "エラーの内容": " ／ ".join(describe(i) for i in page_issues),
            "ファイル": f.name, "エラー": n_err,
        })

# ------------------------------------------------------------ 3. 結果
st.header("3. 結果")

# 校正した項目と、照合に使った正しい内容
flags = [f"{d}日 {v.flag}" for d, v in sorted(fixed.days.items()) if v.flag]
flags += [f"{d}日 {v.label}(白地)" for d, v in sorted(fixed.days.items()) if v.label]
rice = [f"{d}日 {v.rice}" for d, v in sorted(fixed.days.items()) if v.rice]
first_wd = "月火水木金土日"[calendar.weekday(int(year), int(month), 1)]
checked = pd.DataFrame([
    {"項目": "①年月日", "校正した内容": "左上の年・月・期間",
     "正しい内容": f"{year}年 {month}月 ／ 期間 {month}/1〜{month}/{last}"},
    {"項目": "②曜日", "校正した内容": "日付が正しい曜日の列にあるか、抜け・前月翌月の日付がないか",
     "正しい内容": f"{month}月1日は{first_wd}曜、{last}日まで"},
    {"項目": "③旗日", "校正した内容": "旗日の文字と、日付の黒地",
     "正しい内容": "、".join(flags) or "なし"},
    {"項目": "③混ぜご飯", "校正した内容": "贅沢版の混ぜご飯・ふりかけの表記(通常版は表記がないこと)",
     "正しい内容": "、".join(rice) or "なし"},
    {"項目": "④イベント", "校正した内容": "イベントWeekの表記と期間",
     "正しい内容": (f"{event_name + ' ' if event_name else ''}{fixed.event_range[0]}日〜{fixed.event_range[1]}日"
                if fixed.event_range else "なし")},
])
st.subheader("校正した項目")
st.dataframe(checked, hide_index=True, use_container_width=True)
st.caption("店休日(グレーの日付)は店舗ごとに違うためチェックしていません。店休日のマスは③も対象外です。")

sdf = pd.DataFrame(summary)
idf = pd.DataFrame(issues)
c1, c2, c3 = st.columns(3)
c1.metric("ページ数", len(sdf))
c2.metric("✅ OK", int((sdf["判定"] == "✅ OK").sum()))
c3.metric("❌ エラーのあるページ", int((sdf["エラー"] > 0).sum()))

# 全ページ共通の指摘(テンプレート由来のミス)
n_pages = len(sdf)
if not idf.empty and n_pages > 1:
    cnt = {}
    for i in issues:
        k = (i["level"], i["item"], i["day"], i["expected"], i["actual"])
        cnt[k] = cnt.get(k, 0) + 1
    from proof_core import Issue
    common = [Issue(0, "", k[1], k[2], k[3], k[4], k[0]) for k, v in cnt.items() if v == n_pages]
    if common:
        st.error("**全ページ共通のエラー**(元のデータ自体のミスの可能性があります)\n\n"
                 + "\n".join(f"- {describe(i)}" for i in common))

st.subheader("ページ一覧")
show = st.radio("表示", ["すべて", "❌ エラーのみ", "✅ OKのみ"], horizontal=True)
view = sdf
if show == "❌ エラーのみ":
    view = sdf[sdf["エラー"] > 0]
elif show == "✅ OKのみ":
    view = sdf[sdf["判定"] == "✅ OK"]
cols = ["判定", "ページ", "店舗", "種類", *CHECK_COLS.keys(), "エラーの内容"]
if len(fax_files) > 1:
    cols.append("ファイル")
st.dataframe(
    view[cols], hide_index=True, use_container_width=True,
    height=min(600, 38 + 35 * max(1, len(view))),
    column_config={
        "ページ": st.column_config.NumberColumn(width="small"),
        "エラーの内容": st.column_config.TextColumn(width="large"),
    },
)
st.caption("セルにマウスを乗せると全文が見られます。")

buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as xw:
    checked.to_excel(xw, sheet_name="校正した項目", index=False)
    sdf.drop(columns=["エラー"]).to_excel(xw, sheet_name="ページ一覧", index=False)
    if not idf.empty:
        idf.rename(columns={"file": "ファイル", "page": "ページ", "store": "店舗", "item": "項目",
                            "day": "日", "expected": "正しい内容", "actual": "注文書の内容", "level": "区分"}
                   ).drop(columns=["区分"]).to_excel(xw, sheet_name="指摘一覧", index=False)
    from openpyxl.styles import Alignment
    widths = {"校正した項目": [12, 50, 70], "ページ一覧": [10, 6, 36, 18, 10, 10, 10, 12, 10, 70, 30]}
    for name, ws_widths in widths.items():
        ws = xw.sheets[name]
        for n, w in enumerate(ws_widths):
            ws.column_dimensions[chr(65 + n)].width = w
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
st.download_button("結果をExcelでダウンロード", buf.getvalue(),
                   file_name=f"校正結果_{year}年{month}月.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
