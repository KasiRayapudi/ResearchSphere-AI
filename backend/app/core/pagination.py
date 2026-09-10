"""
Pagination, sorting, filtering and search for list endpoints.

Every list endpoint used to return its whole table for a workspace. That is
fine for a demo and untenable for a real one: a workspace with ten thousand
documents produced a response nobody could use, over a query nothing could
index its way out of.

Two strategies, because they answer different questions:

**Offset** (``?page=3&page_size=25``) can say "page 3 of 47", which is what a
numbered pager needs. Its cost grows with the offset: the database walks and
discards every skipped row, so page 4000 is slow no matter how it is indexed.

**Keyset** (``?cursor=...``) asks for "the next N after this row" and stays
flat however deep it goes, because it seeks rather than counts. It cannot
produce a page number or jump to an arbitrary page, and it needs a stable,
unique sort. It is the right default for feeds that only ever move forwards.

Both are offered. Endpoints whose data grows without bound accept a cursor;
everything else uses offset, where a total is worth the count.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Generic, TypeVar

from fastapi import HTTPException, Query
from sqlalchemy import String, and_, cast, or_
from sqlalchemy.orm import Query as OrmQuery

T = TypeVar("T")

#: Chosen to fill a screen without inviting a client to ask for everything.
DEFAULT_PAGE_SIZE = 25
#: A hard ceiling, applied server-side. A client asking for 10_000 gets 100:
#: the point of paginating is that one request cannot be made unbounded.
MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class PageParams:
    """Validated list parameters."""

    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    sort: str | None = None
    order: str = "desc"
    search: str | None = None
    cursor: str | None = None

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def descending(self) -> bool:
        return self.order.lower() != "asc"


def page_params(
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page, at most {MAX_PAGE_SIZE}.",
    ),
    sort: str | None = Query(None, description="Field to sort by."),
    order: str = Query("desc", pattern="^(asc|desc)$", description="Sort direction."),
    search: str | None = Query(None, max_length=200, description="Free-text filter."),
    cursor: str | None = Query(
        None,
        max_length=512,
        description="Keyset cursor from a previous response. Overrides `page`.",
    ),
) -> PageParams:
    """FastAPI dependency producing validated parameters.

    Bounds are declared here rather than clamped afterwards, so an out-of-range
    request is a 422 naming the field rather than a silently different result.
    """
    return PageParams(
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
        search=(search or "").strip() or None,
        cursor=cursor,
    )


@dataclass
class Page(Generic[T]):
    """One page of results plus what a client needs to ask for the next."""

    items: list[T]
    page: int
    page_size: int
    #: None for keyset pages: counting the whole set would throw away the
    #: reason for using a cursor in the first place.
    total: int | None
    pages: int | None
    has_next: bool
    has_previous: bool
    next_cursor: str | None = None

    def envelope(self, items: list[Any] | None = None) -> dict:
        """The JSON shape returned by every paginated endpoint."""
        return {
            "items": self.items if items is None else items,
            "page": self.page,
            "pageSize": self.page_size,
            "total": self.total,
            "pages": self.pages,
            "hasNext": self.has_next,
            "hasPrevious": self.has_previous,
            "nextCursor": self.next_cursor,
        }


# --------------------------------------------------------------- cursors --
def encode_cursor(sort_value: Any, row_id: str) -> str:
    """Opaque cursor for a (sort value, id) pair.

    Base64 of JSON: opaque enough that clients treat it as a token rather
    than something to construct, and cheap to decode. It carries no
    authority -- the query it is applied to is already scoped to the caller,
    so a forged cursor can only reposition within data they may already see.
    """
    if isinstance(sort_value, datetime | date):
        encoded_value = sort_value.isoformat()
    else:
        encoded_value = sort_value
    payload = json.dumps({"v": encoded_value, "i": row_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[Any, str]:
    """Decode a cursor, or 400 if it is not one.

    A malformed cursor is a client error worth naming: silently falling back
    to the first page would look like data loss to whoever is paging.
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        return payload["v"], payload["i"]
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="That pagination cursor is not valid.") from exc


def _coerce(value: Any, column) -> Any:
    """Turn a decoded cursor value back into something comparable.

    Datetimes cross the wire as ISO strings; comparing those to a DateTime
    column works on PostgreSQL and silently misbehaves elsewhere.
    """
    python_type = getattr(column.type, "python_type", None)
    if python_type in (datetime, date) and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.date() if python_type is date else parsed
        except ValueError:
            return value
    return value


# ------------------------------------------------------------ the engine --
def apply_search(query: OrmQuery, params: PageParams, columns: list) -> OrmQuery:
    """Case-insensitive substring match across ``columns``.

    Deliberately simple. A LIKE scan is the honest tool for filtering a
    workspace's own list; anything that needs real relevance ranking belongs
    in the vector store, which the product already has.
    """
    if not params.search or not columns:
        return query
    pattern = f"%{params.search.lower()}%"
    clauses = [cast(column, String).ilike(pattern) for column in columns]
    return query.filter(or_(*clauses))


def resolve_sort(params: PageParams, sortable: dict, default: str):
    """Pick the sort column, rejecting anything not explicitly allowed.

    An allowlist rather than ``getattr(Model, params.sort)``: the latter lets
    a caller order by any attribute on the model, which leaks its shape and
    can be pointed at an unindexed column to make the query expensive.
    """
    key = params.sort or default
    column = sortable.get(key)
    if column is None:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot sort by {key!r}. Allowed: {', '.join(sorted(sortable))}.",
        )
    return column


def paginate(
    query: OrmQuery,
    params: PageParams,
    *,
    sortable: dict,
    default_sort: str,
    tiebreaker,
    searchable: list | None = None,
) -> Page:
    """Apply search, sort and paging. Uses the cursor when one is supplied.

    ``tiebreaker`` must be a unique column, normally the primary key. Without
    it, rows sharing a sort value have no defined order, so the same row can
    appear on two pages or on none -- the classic pagination bug that only
    shows up once there is enough data to have ties.
    """
    query = apply_search(query, params, searchable or [])
    column = resolve_sort(params, sortable, default_sort)

    if params.cursor:
        return _keyset_page(query, params, column, tiebreaker)
    return _offset_page(query, params, column, tiebreaker)


def _ordered(query: OrmQuery, column, tiebreaker, descending: bool) -> OrmQuery:
    if descending:
        return query.order_by(column.desc(), tiebreaker.desc())
    return query.order_by(column.asc(), tiebreaker.asc())


def _offset_page(query: OrmQuery, params: PageParams, column, tiebreaker) -> Page:
    # Counted before ordering: the ORDER BY cannot change how many rows match
    # and only makes the count slower.
    total = query.order_by(None).count()
    rows = (
        _ordered(query, column, tiebreaker, params.descending)
        .offset(params.offset)
        .limit(params.page_size)
        .all()
    )
    pages = max(1, -(-total // params.page_size))  # ceiling division
    return Page(
        items=rows,
        page=params.page,
        page_size=params.page_size,
        total=total,
        pages=pages,
        has_next=params.page < pages,
        has_previous=params.page > 1,
        next_cursor=_cursor_for(rows, column, tiebreaker),
    )


def _keyset_page(query: OrmQuery, params: PageParams, column, tiebreaker) -> Page:
    last_value, last_id = decode_cursor(params.cursor)
    last_value = _coerce(last_value, column)

    # Strictly after the cursor row in the sort order, with the tiebreaker
    # settling rows that share a sort value. Written as an explicit OR rather
    # than a row-value comparison, which SQLite does not support.
    if params.descending:
        keyset = or_(
            column < last_value,
            and_(column == last_value, tiebreaker < last_id),
        )
    else:
        keyset = or_(
            column > last_value,
            and_(column == last_value, tiebreaker > last_id),
        )

    # One extra row tells us whether another page exists without a count.
    rows = (
        _ordered(query.filter(keyset), column, tiebreaker, params.descending)
        .limit(params.page_size + 1)
        .all()
    )
    has_next = len(rows) > params.page_size
    rows = rows[: params.page_size]

    return Page(
        items=rows,
        page=params.page,
        page_size=params.page_size,
        total=None,
        pages=None,
        has_next=has_next,
        has_previous=True,  # a cursor can only exist after a first page
        next_cursor=_cursor_for(rows, column, tiebreaker) if has_next else None,
    )


def _cursor_for(rows: list, column, tiebreaker) -> str | None:
    """Cursor pointing just past the last row of this page."""
    if not rows:
        return None
    last = rows[-1]
    try:
        return encode_cursor(getattr(last, column.key), getattr(last, tiebreaker.key))
    except AttributeError:
        # A row from a joined query that is not the mapped entity. Offset
        # paging still works; there is simply no cursor to hand out.
        return None
