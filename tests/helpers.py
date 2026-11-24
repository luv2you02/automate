"""테스트용 엑셀 생성기. 샘플 파일과 같은 컬럼 구성(제품 시트 + 경매 시트)으로 만든다.

제품은 톤 단위로 적고(ton=13), 엑셀에는 샘플처럼 kg 로 들어간다.
orders 를 주지 않으면 (경매번호, 고객) 그룹마다 '낙찰확정' 주문 1건을 자동으로 만든다.
"""
from __future__ import annotations

import io
from collections import OrderedDict

import pandas as pd

_seq = [0]


# 기본 판: 두께 100mm × 폭 2500mm → 13t ≈ 6.6m (장척 아님). 장척을 원하면 t 를 얇게 하거나 length 지정.
def P(pno: str, ton: float, *, auc="2026112001", cust="C1", cname=None, dlv=None, dname=None, route="R1",
      t=100.0, w=2500.0, length=None, shipped=None, spec="SS275", date="20261120") -> dict:
    return {"pno": pno, "kg": round(ton * 1000), "auc": auc, "cust": cust, "cname": cname or f"고객{cust}",
            "dlv": dlv or f"DL{cust}", "dname": dname or f"{cname or '고객' + cust} 본사", "route": route,
            "t": t, "w": w, "length": length, "shipped": shipped, "spec": spec, "date": date}


def O(auc: str, cust: str, status: str, qty: int, kg: float, order_no: str | None = None, route="R1",
      dlv=None, plan="20261125") -> dict:
    return {"auc": auc, "cust": cust, "status": status, "qty": qty, "kg": kg, "order_no": order_no,
            "route": route, "dlv": dlv or f"DL{cust}", "plan": plan}


def auto_orders(products: list[dict], status: str = "낙찰확정") -> list[dict]:
    groups: "OrderedDict[tuple, list[dict]]" = OrderedDict()
    for p in products:
        groups.setdefault((p["auc"], p["cust"]), []).append(p)
    out = []
    for (auc, cust), ps in groups.items():
        _seq[0] += 1
        out.append(O(auc, cust, status, len(ps), sum(p["kg"] for p in ps),
                     order_no=f"D{261100000 + _seq[0]:09d}" if status == "낙찰확정" else None,
                     route=ps[0]["route"], dlv=ps[0]["dlv"]))
    return out


def make_xlsx(products: list[dict], orders: list[dict] | None = None, *, with_length=False, with_shipped=False,
              product_sheet="Sheet1", auction_sheet="Sheet1 (2)", include_auction=True,
              include_products=True) -> bytes:
    if orders is None:
        orders = auto_orders(products)
    prod_rows = []
    for p in products:
        row = {
            "경매번호": p["auc"], "경매일자": p["date"], "업체코드": p["cust"], "업체명": p["cname"],
            "플랜트": "당진 후판", "품종": "PCJ", "제품번호": p["pno"], "인도처코드": p["dlv"], "인도처명": p["dname"],
            "규격약호": p["spec"], "두께 (mm)": p["t"], "폭 (mm)": p["w"], "중량": p["kg"],
        }
        if with_length:
            row["길이"] = p["length"]
        if with_shipped:
            row["출하여부"] = p["shipped"]
        prod_rows.append(row)
    cname = {p["cust"]: p["cname"] for p in products}
    dname = {p["dlv"]: p["dname"] for p in products}
    ord_rows = [{
        "경매일자": "20261120", "경매번호": o["auc"], "고객코드": o["cust"], "고객명": cname.get(o["cust"], o["cust"]),
        "경매타입": "일반경매", "진행상태": o["status"], "수량": o["qty"], "중량": o["kg"], "주문번호": o["order_no"],
        "운송경로": o["route"], "인도처코드": o["dlv"], "인도처명": dname.get(o["dlv"], o["dlv"]),
        "출하예정일": o["plan"] if o["status"] == "낙찰확정" else None,
    } for o in orders]

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        if include_auction:
            pd.DataFrame(ord_rows).to_excel(xw, sheet_name=auction_sheet, index=False)
        if include_products:
            pd.DataFrame(prod_rows).to_excel(xw, sheet_name=product_sheet, index=False)
    return buf.getvalue()


def make_places_xlsx(rows: list[tuple[str, str]]) -> bytes:
    buf = io.BytesIO()
    pd.DataFrame([{"인도처코드": c, "주소": a} for c, a in rows]).to_excel(buf, index=False)
    return buf.getvalue()


def upload(client, content: bytes, name="test.xlsx") -> dict:
    r = client.post("/api/upload", files={"files": (name, content)})
    assert r.status_code == 200, r.text
    return r.json()


def dispatch(client, date="2026-11-24") -> dict:
    r = client.post("/api/dispatch/run", json={"date": date})
    assert r.status_code == 200, r.text
    return r.json()


def products(client) -> dict[str, dict]:
    """product_no → row_summary (배차 결과 API 기준)."""
    d = client.get("/api/dispatch?include_shipped=true").json()
    out = {}
    for t in d["trucks"]:
        for p in t["products"]:
            out[p["product_no"]] = p
    for p in d["unassigned"] + d["waiting_payment"]:
        out[p["product_no"]] = p
    return out


def chat(client, q: str) -> dict:
    r = client.post("/api/chat", json={"question": q})
    assert r.status_code == 200, r.text
    return r.json()
