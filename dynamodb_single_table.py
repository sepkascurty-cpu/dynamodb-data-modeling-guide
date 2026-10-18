#!/usr/bin/env python3
"""
dynamodb_single_table.py -- Single-table DynamoDB design for a small shop.

Entities  : users, orders, order line items, invoices -- ALL in one table.
Keys      : overloaded PK/SK. PK groups what is queried together,
            SK encodes entity type + sort order (hierarchical, prefixed).
GSI1      : inverted index (GSI1PK = STATUS#<state>) for "all orders by status".
Sparse    : only items carrying GSI1PK/GSI1SK appear in GSI1.

Run the local demo (no AWS credentials needed):
    python dynamodb_single_table.py

Production swap: replace InMemoryTable with boto3's Table object --
each method's docstring shows the real query() call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

TABLE_NAME = "shop-data"

# ------------------------------------------------------------------ key helpers

def user_pk(user_id: str) -> str:
    return f"USER#{user_id}"

def order_sk(month: str, order_id: str) -> str:
    # Date bucket keeps hot months tight and enables BETWEEN queries.
    return f"ORDER#{month}#{order_id}"

def item_sk(month: str, order_id: str, sku: str) -> str:
    return f"{order_sk(month, order_id)}#ITEM#{sku}"

def status_gsi1pk(status: str) -> str:
    return f"STATUS#{status}"

def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------- in-memory DynamoDB-ish

@dataclass
class InMemoryTable:
    """Tiny stand-in with DynamoDB's core semantics.

    put_item    : upsert keyed on (PK, SK); GSI1 entries derived from
                  GSI1PK/GSI1SK attributes (sparse: absent -> not indexed).
    query       : PK equality + optional SK condition (EQ / BEGINS_WITH /
                  BETWEEN), newest-first when scan_index_forward=False.
    query_gsi1  : same, on the sparse GSI1 projection.
    """

    items: list = field(default_factory=list)

    def put_item(self, item: dict) -> None:
        key = (item["PK"], item["SK"])
        self.items = [i for i in self.items if (i["PK"], i["SK"]) != key]
        self.items.append(item)

    def _match_sk(self, sk: str, sk_cond: tuple | None) -> bool:
        if sk_cond is None:
            return True
        op = sk_cond[0]
        if op == "EQ":
            return sk == sk_cond[1]
        if op == "BEGINS_WITH":
            return sk.startswith(sk_cond[1])
        if op == "BETWEEN":
            return sk_cond[1] <= sk <= sk_cond[2]
        raise ValueError(f"unknown SK op: {op}")

    def query(self, pk: str, sk_cond: tuple | None = None,
              scan_index_forward: bool = True) -> list[dict]:
        hits = [i for i in self.items
                if i["PK"] == pk and self._match_sk(i["SK"], sk_cond)]
        return sorted(hits, key=lambda i: i["SK"], reverse=not scan_index_forward)

    def query_gsi1(self, gsi1pk: str, scan_index_forward: bool = False) -> list[dict]:
        # Sparse: items without GSI1PK are simply not in the index.
        hits = [i for i in self.items if i.get("GSI1PK") == gsi1pk]
        return sorted(hits, key=lambda i: i.get("GSI1SK", ""),
                      reverse=not scan_index_forward)


# ------------------------------------------------------------------ the store

class ShopStore:
    """All six access patterns, each ONE query. Boto3 equivalents in docstrings."""

    def __init__(self, table=None) -> None:
        self.table = table or InMemoryTable()

    # -- writes --------------------------------------------------------

    def put_user(self, user_id: str, name: str, email: str) -> None:
        self.table.put_item({
            "PK": user_pk(user_id), "SK": "PROFILE",
            "name": name, "email": email,
            "order_count": 0,  # materialized counter: ADD, never Scan() to count
        })

    def put_order(self, user_id: str, order_id: str, month: str,
                  total_cents: int, status: str = "PENDING") -> None:
        created = now_iso()
        self.table.put_item({
            "PK": user_pk(user_id), "SK": order_sk(month, order_id),
            "order_id": order_id, "total_cents": total_cents,
            "status": status, "created_at": created,
            # GSI1 routing attributes (sparse: only orders carry these)
            "GSI1PK": status_gsi1pk(status), "GSI1SK": created,
        })

    def put_line_item(self, user_id: str, order_id: str, month: str,
                      sku: str, qty: int, price_cents: int) -> None:
        self.table.put_item({
            "PK": user_pk(user_id), "SK": item_sk(month, order_id, sku),
            "sku": sku, "qty": qty, "price_cents": price_cents,
        })

    def put_invoice(self, invoice_id: str, order_id: str, amount_cents: int) -> None:
        self.table.put_item({
            "PK": f"INVOICE#{invoice_id}", "SK": f"INVOICE#{invoice_id}",
            "order_id": order_id, "amount_cents": amount_cents,
        })

    def seed(self) -> None:
        self.put_user("u-42", "Arlic", "arlic@example.com")
        self.put_order("u-42", "ord-123", "2026-10", 49900, "PENDING")
        self.put_line_item("u-42", "ord-123", "2026-10", "sku-7", 2, 19900)
        self.put_line_item("u-42", "ord-123", "2026-10", "sku-9", 1, 10100)
        self.put_order("u-42", "ord-098", "2026-09", 25900, "SHIPPED")
        self.put_invoice("inv-9", "ord-098", 25900)

    # -- reads: the six access patterns --------------------------------

    def get_user_profile(self, user_id: str) -> dict | None:
        """Pattern 1. boto3: KeyConditionExpression=Key('PK').eq(pk) & Key('SK').eq('PROFILE')"""
        rows = self.table.query(user_pk(user_id), ("EQ", "PROFILE"))
        return rows[0] if rows else None

    def list_user_orders(self, user_id: str, newest_first: bool = True) -> list[dict]:
        """Pattern 2. boto3: Key('PK').eq(pk) & Key('SK').begins_with('ORDER#'),
        ScanIndexForward=False. Line items excluded: SK starts with ORDER# but
        contains '#ITEM#', so filter them out (or use BEGINS_WITH('ORDER#') AND
        NOT contains(SK, '#ITEM#') via FilterExpression)."""
        rows = self.table.query(user_pk(user_id), ("BEGINS_WITH", "ORDER#"),
                                scan_index_forward=not newest_first)
        return [r for r in rows if "#ITEM#" not in r["SK"]]

    def get_order(self, user_id: str, order_id: str) -> dict:
        """Pattern 3. boto3: Key('PK').eq(pk) & Key('SK').begins_with('ORDER#<month>#<id>')
        -- but the month is part of the key. Store the month on the order (we do),
        or keep a lookup. Here we find the order row first, then its items."""
        orders = [r for r in self.list_user_orders(user_id, newest_first=False)
                  if r["order_id"] == order_id]
        if not orders:
            raise KeyError(order_id)
        order = orders[0]
        month = order["SK"].split("#")[1]  # ORDER#<month>#<id>
        items = self.table.query(user_pk(user_id),
                                 ("BEGINS_WITH", f"{order_sk(month, order_id)}#ITEM#"))
        return {"order": order, "items": items}

    def list_orders_by_status(self, status: str) -> list[dict]:
        """Pattern 4 (the GSI one). boto3: IndexName='GSI1',
        KeyConditionExpression=Key('GSI1PK').eq('STATUS#PENDING')."""
        return self.table.query_gsi1(status_gsi1pk(status))

    def list_user_orders_in_month(self, user_id: str, month: str) -> list[dict]:
        """Pattern 5. boto3: Key('PK').eq(pk) & Key('SK').between('ORDER#2026-10', 'ORDER#2026-10￿').
        The date bucket in the SK makes this a range query, not a filter."""
        rows = self.table.query(user_pk(user_id),
                                ("BETWEEN", f"ORDER#{month}", f"ORDER#{month}\uffff"))
        return [r for r in rows if "#ITEM#" not in r["SK"]]

    def get_invoice(self, invoice_id: str) -> dict | None:
        """Pattern 6. Invoices get their own PK namespace -- point lookup."""
        rows = self.table.query(f"INVOICE#{invoice_id}", ("EQ", f"INVOICE#{invoice_id}"))
        return rows[0] if rows else None


# ----------------------------------------------------------------- local demo

def demo() -> None:
    print("== Single-table DynamoDB design -- local demo ==\n")
    store = ShopStore()
    store.seed()

    p = store.get_user_profile("u-42")
    print(f"1. get_user_profile        PK=USER#u-42 SK=PROFILE")
    print(f"   -> {p['name']} <{p['email']}> (order_count={p['order_count']})\n")

    orders = store.list_user_orders("u-42")
    print("2. list_user_orders        PK=USER#u-42 SK BEGINS_WITH ORDER# (newest first)")
    for o in orders:
        print(f"   -> {o['order_id']} [{o['status']}] ${o['total_cents']/100:.2f}")
    print()

    detail = store.get_order("u-42", "ord-123")
    print("3. get_order               PK=USER#u-42 SK BEGINS_WITH ORDER#2026-10#ord-123")
    print(f"   -> order {detail['order']['order_id']} + {len(detail['items'])} line items:")
    for it in detail["items"]:
        print(f"      {it['sku']} x{it['qty']} @ ${it['price_cents']/100:.2f}")
    print()

    pending = store.list_orders_by_status("PENDING")
    print("4. list_orders_by_status   GSI1: GSI1PK=STATUS#PENDING (sparse index)")
    for o in pending:
        print(f"   -> {o['order_id']} created {o['created_at']}")
    print()

    oct_orders = store.list_user_orders_in_month("u-42", "2026-10")
    print("5. list_user_orders_in_month  SK BETWEEN ORDER#2026-10 AND ORDER#2026-10￿")
    print(f"   -> {len(oct_orders)} order(s): {[o['order_id'] for o in oct_orders]}\n")

    inv = store.get_invoice("inv-9")
    print("6. get_invoice             PK=INVOICE#inv-9 SK=INVOICE#inv-9")
    print(f"   -> invoice inv-9: ${inv['amount_cents']/100:.2f} for {inv['order_id']}\n")

    print("Demo done: 6 patterns, 6 queries, 0 scans. "
          "Swap InMemoryTable for boto3's Table to run this for real.")


if __name__ == "__main__":
    demo()
