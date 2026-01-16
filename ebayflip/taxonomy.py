from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from ebayflip import get_logger
from ebayflip.db import get_connection

LOGGER = get_logger()

TAXONOMY_BASE_URL = "https://api.ebay.com/commerce/taxonomy/v1"
TAXONOMY_MARKETPLACE = "EBAY_GB"
FALLBACK_PATH = Path(__file__).resolve().parent / "data" / "ebay_categories_fallback.json"


@dataclass(frozen=True, slots=True)
class Category:
    category_id: str
    name: str
    parent_id: Optional[str]
    level: int


_CATEGORY_CACHE: dict[str, Category] = {}
_CHILDREN_CACHE: dict[Optional[str], list[Category]] = {}
_CATEGORIES_LOADED = False


def ensure_categories_loaded(db_path: str) -> bool:
    global _CATEGORIES_LOADED
    if _CATEGORIES_LOADED and _CATEGORY_CACHE:
        return True

    try:
        categories = _load_categories_from_db(db_path)
        if not categories:
            categories = _load_categories_from_api()
        if not categories:
            categories = _load_categories_from_fallback()
        if not categories:
            LOGGER.warning("No category data available.")
            return False
        if not _has_categories_in_db(db_path):
            _store_categories(db_path, categories)
        _build_category_cache(categories)
        _CATEGORIES_LOADED = True
        return True
    except Exception as exc:
        LOGGER.warning("Category loading failed: %s", exc)
        return False


def get_top_categories() -> list[Category]:
    if not _CATEGORIES_LOADED:
        return []
    return list(_CHILDREN_CACHE.get(None, []))


def get_child_categories(parent_id: str) -> list[Category]:
    if not _CATEGORIES_LOADED:
        return []
    return list(_CHILDREN_CACHE.get(parent_id, []))


def get_category_path(category_id: str) -> list[Category]:
    if not _CATEGORIES_LOADED or not category_id:
        return []
    path: list[Category] = []
    current_id: Optional[str] = category_id
    visited = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        category = _CATEGORY_CACHE.get(current_id)
        if not category:
            break
        path.append(category)
        current_id = category.parent_id
    return list(reversed(path))


def _has_categories_in_db(db_path: str) -> bool:
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT 1 FROM categories LIMIT 1").fetchone()
    return row is not None


def _load_categories_from_db(db_path: str) -> list[Category]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT category_id, name, parent_id, level FROM categories"
        ).fetchall()
    return [
        Category(
            category_id=row["category_id"],
            name=row["name"],
            parent_id=row["parent_id"],
            level=int(row["level"]),
        )
        for row in rows
    ]


def _store_categories(db_path: str, categories: list[Category]) -> None:
    with get_connection(db_path) as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO categories (category_id, name, parent_id, level)
            VALUES (?, ?, ?, ?)
            """,
            [
                (category.category_id, category.name, category.parent_id, category.level)
                for category in categories
            ],
        )


def _build_category_cache(categories: list[Category]) -> None:
    _CATEGORY_CACHE.clear()
    _CHILDREN_CACHE.clear()
    for category in categories:
        _CATEGORY_CACHE[category.category_id] = category
        _CHILDREN_CACHE.setdefault(category.parent_id, []).append(category)
    for siblings in _CHILDREN_CACHE.values():
        siblings.sort(key=lambda item: item.name)


def _load_categories_from_api() -> list[Category]:
    token = os.getenv("EBAY_OAUTH_TOKEN")
    if not token:
        return []
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        tree_id = _get_default_tree_id(headers)
        if not tree_id:
            return []
        response = requests.get(
            f"{TAXONOMY_BASE_URL}/category_tree/{tree_id}",
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        LOGGER.warning("Failed to fetch taxonomy API data: %s", exc)
        return []
    root_node = payload.get("rootCategoryNode")
    if not root_node:
        return []
    categories: list[Category] = []
    child_nodes = root_node.get("childCategoryTreeNodes", [])
    _walk_taxonomy_nodes(categories, child_nodes, parent_id=None, level=0)
    return categories


def _get_default_tree_id(headers: dict[str, str]) -> Optional[str]:
    response = requests.get(
        f"{TAXONOMY_BASE_URL}/get_default_category_tree_id",
        headers=headers,
        params={"marketplace_id": TAXONOMY_MARKETPLACE},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return data.get("categoryTreeId")


def _walk_taxonomy_nodes(
    categories: list[Category],
    nodes: list[dict],
    parent_id: Optional[str],
    level: int,
) -> None:
    if level > 2:
        return
    for node in nodes:
        category_info = node.get("category", {})
        category_id = category_info.get("categoryId")
        name = category_info.get("categoryName")
        if not category_id or not name:
            continue
        categories.append(
            Category(
                category_id=str(category_id),
                name=str(name),
                parent_id=parent_id,
                level=level,
            )
        )
        children = node.get("childCategoryTreeNodes") or []
        if children and level < 2:
            _walk_taxonomy_nodes(categories, children, parent_id=str(category_id), level=level + 1)


def _load_categories_from_fallback() -> list[Category]:
    try:
        payload = json.loads(FALLBACK_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        LOGGER.warning("Fallback category file missing: %s", FALLBACK_PATH)
        return []
    except json.JSONDecodeError as exc:
        LOGGER.warning("Fallback category JSON invalid: %s", exc)
        return []
    categories: list[Category] = []
    for entry in payload:
        category_id = entry.get("category_id")
        name = entry.get("name")
        if not category_id or not name:
            continue
        categories.append(
            Category(
                category_id=str(category_id),
                name=str(name),
                parent_id=entry.get("parent_id"),
                level=int(entry.get("level", 0)),
            )
        )
    return categories
