# DynamoDB Data Modeling — Self-Test

5 questions. Answer from memory first, then check. No peeking. 👀

---

## 1. Why does this guide insist on listing access patterns *before* designing the table?

<details>
<summary>Answer</summary>

Because DynamoDB stores data the way you **read** it, not the way the world is shaped. Every access pattern becomes a key design (`PK` + `SK`); the table is just all those key designs merged into one. Skip this step and you'll model a relational schema, then reach for `Scan()` on every page load — reading (and paying for) the entire table.

</details>

---

## 2. A user's profile, their orders, and each order's line items all share the partition key `USER#u-42`. What does that buy you — and what is the danger?

<details>
<summary>Answer</summary>

**Buys:** data locality. One partition holds everything a request needs, so "get user + orders + line items" is a small number of single-partition `Query()` calls with single-digit-millisecond latency — no application-side joins across tables.

**Danger:** hot partitions. If one user (or one key prefix) absorbs a wildly disproportionate share of traffic, that partition throttles while provisioned capacity sits idle elsewhere. Mitigate with date buckets (`ORDER#2026-10#...`), key sharding, and caching in front of the hottest keys.

</details>

---

## 3. Your product page needs the user profile, their last 5 orders, and each order's line items. On this guide's design, how many DynamoDB queries is that — and which key expressions?

<details>
<summary>Answer</summary>

**Three queries, all on one partition (`PK = USER#u-42`):**

1. `PK = USER#u-42, SK = PROFILE` → the profile.
2. `PK = USER#u-42, SK BEGINS_WITH ORDER#`, `Limit = 5`, `ScanIndexForward = False` → the 5 newest orders.
3. For each of the 5 orders: `PK = USER#u-42, SK BEGINS_WITH ORDER#<month>#<order-id>#ITEM#` → its line items. (That's 5 small queries, or fetch items together with the order via one `BEGINS_WITH ORDER#<month>#<order-id>` per order.)

No joins, no scans, no second table — adjacency in the sort key *is* the join.

</details>

---

## 4. "All pending orders across all users" can't be answered with the base table's keys. What did the guide add, and why is it "sparse"?

<details>
<summary>Answer</summary>

**GSI1**, an inverted index with `GSI1PK = STATUS#<state>` and `GSI1SK = created_at`. The query becomes `GSI1PK = STATUS#PENDING` — one index query instead of a full-table scan.

It's **sparse** because only order items carry the `GSI1PK`/`GSI1SK` attributes. Profiles, line items, and invoices don't have them, so they never appear in the index — the index stays small and cheap, containing exactly the items the query cares about. Bonus: sorting by `GSI1SK` gives newest-first pending orders for free.

</details>

---

## 5. Which two of these are genuine DynamoDB anti-patterns, and why?

a) Using `Scan()` with a `FilterExpression` "because the table is still small"
b) Putting the entity type in the sort key, e.g. `ORDER#2026-10#ord-123`
c) Creating a GSI for every access pattern, including rare admin queries
d) Setting a `ttl` attribute on session and cart items

<details>
<summary>Answer</summary>

**a) and c).**

a) Tables stop being small. `Scan()` reads *every* item and bills you for all of it, even the ones the filter discards. Design keys so the pattern is a `Query()` from day one.

c) Every GSI multiplies your write cost on indexed items and adds eventually-consistent read lag to reason about. Index the patterns that matter; for rare admin queries, a targeted scan of a single partition (or an export to S3 + Athena) is cheaper than a permanent GSI.

b) is the *recommended* pattern (hierarchical, prefixed sort keys enable `BEGINS_WITH`/`BETWEEN` queries). d) is the *recommended* pattern (DynamoDB deletes expired items for free — sessions and carts shouldn't live forever on your bill).

</details>

---

Scored 5/5? You're thinking in queries now. 3–4? Re-read the access-pattern table in the README. Below 3? Start over at "Design workflow" — it's the whole game.
