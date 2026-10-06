"""Paging through an ArcGIS feature layer — the form county and city GIS
inventories, and one state corporate registry, are published in."""

from __future__ import annotations

import time

import requests

DEFAULT_PAGE_SIZE = 1000


def query_url(layer_url: str) -> str:
    """The layer's /query endpoint, whether or not the configured URL names it."""
    layer_url = layer_url.rstrip("/")
    return layer_url if layer_url.endswith("/query") else layer_url + "/query"


def query_features(session: requests.Session, layer_url: str, where: str = "1=1", out_fields: str = "*",
                   order_by: str | None = "OBJECTID", page_size: int = DEFAULT_PAGE_SIZE,
                   pace: float = 0.5, timeout: float = 180) -> list[dict]:
    """The attributes of every feature matching `where`, read page by page
    through `resultOffset` until a short page arrives and the server says
    nothing was left out."""
    out: list[dict] = []
    offset = 0
    while True:
        params = {"where": where, "outFields": out_fields, "returnGeometry": "false",
                  "resultOffset": offset, "resultRecordCount": page_size, "f": "json"}
        if order_by:
            params["orderByFields"] = order_by
        response = session.get(query_url(layer_url), params=params, timeout=timeout)
        response.raise_for_status()
        page = response.json()
        if "error" in page:
            raise RuntimeError(f"arcgis error: {page['error']}")
        features = [feature.get("attributes", feature) for feature in page.get("features", [])]
        out.extend(features)
        offset += len(features)
        if not features or (len(features) < page_size and not page.get("exceededTransferLimit")):
            return out
        time.sleep(pace)
