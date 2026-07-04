"""
Leaflet map builder — render scored + enriched homes to a self-contained ``map.html``
(data embedded, no server needed). Pins are colored by solar grade; click for
address, grade, production, and potential. Defaults to residential leads only.
"""

from __future__ import annotations

import json
import warnings

import duckdb

from .config import Config

warnings.filterwarnings("ignore")

GRADES_SHOWN = ("A+", "A", "B+", "B", "C", "D")
GRADE_COLORS = {
    "A+": "#1a9850", "A": "#66bd63", "B+": "#a6d96a",
    "B": "#fee08b", "C": "#fdae61", "D": "#d73027",
}


def _load_features(cfg: Config, include_non_residential: bool):
    con = duckdb.connect(cfg.db_path, read_only=True)
    cols = {c[0] for c in con.execute("DESCRIBE homes").fetchall()}
    if "is_residential" not in cols:
        con.close()
        raise SystemExit("No address/residential columns — run enrichment first.")

    where = ["solar_grade IN ({})".format(",".join(f"'{g}'" for g in GRADES_SHOWN))]
    if not include_non_residential:
        where.append("is_residential")
    rows = con.execute(f"""
        SELECT lat, lon, solar_grade, solar_score,
               ROUND(res_annual_kwh) AS res_kwh, ROUND(res_system_kw, 1) AS res_kw,
               potential_grade, ROUND(max_system_kw, 1) AS max_kw,
               ROUND(shade_loss_pct) AS shade,
               COALESCE(full_address, '(no address)') AS addr, COALESCE(city, '') AS city
        FROM homes WHERE {' AND '.join(where)}
    """).fetchdf()
    con.close()

    features = []
    for r in rows.itertuples(index=False):
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r.lon, r.lat]},
            "properties": {
                "grade": r.solar_grade, "score": int(r.solar_score),
                "res_kwh": None if r.res_kwh != r.res_kwh else int(r.res_kwh),
                "res_kw": r.res_kw, "pot": r.potential_grade, "max_kw": r.max_kw,
                "shade": None if r.shade != r.shade else int(r.shade),
                "addr": r.addr, "city": r.city,
            },
        })
    return rows, features


def _build_html(cfg: Config, rows, features) -> str:
    center_lat, center_lon = float(rows["lat"].mean()), float(rows["lon"].mean())
    geojson = json.dumps({"type": "FeatureCollection", "features": features})
    colors = json.dumps(GRADE_COLORS)
    legend = "".join(f'<div><span style="background:{c}"></span>{g}</div>'
                     for g, c in GRADE_COLORS.items())

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>Solar Grader — Leads</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body,#map {{ height:100%; margin:0; }}
  .legend {{ background:#fff; padding:8px 10px; border-radius:6px; font:13px sans-serif;
             box-shadow:0 1px 5px rgba(0,0,0,.3); line-height:1.6; }}
  .legend div span {{ display:inline-block; width:12px; height:12px; margin-right:6px;
                      border-radius:50%; }}
  .count {{ font-weight:bold; margin-bottom:4px; }}
</style></head>
<body><div id="map"></div>
<script>
const DATA = {geojson};
const COLORS = {colors};
const map = L.map('map').setView([{center_lat}, {center_lon}], 15);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  maxZoom: 19, attribution: '{cfg.attribution}'
}}).addTo(map);
L.geoJSON(DATA, {{
  pointToLayer: (f, latlng) => L.circleMarker(latlng, {{
    radius: 5, weight: 1, color: '#333', fillOpacity: 0.85,
    fillColor: COLORS[f.properties.grade] || '#888'
  }}),
  onEachFeature: (f, layer) => {{
    const p = f.properties;
    layer.bindPopup(
      `<b>${{p.addr}}</b><br>${{p.city}}<br>` +
      `<b>Grade ${{p.grade}}</b> (score ${{p.score}})<br>` +
      `Residential: ${{p.res_kw}} kW · ${{p.res_kwh}} kWh/yr<br>` +
      `Potential: ${{p.pot}} · up to ${{p.max_kw}} kW<br>` +
      `Shading loss: ${{p.shade}}%`
    );
  }}
}}).addTo(map);
const legend = L.control({{position:'bottomright'}});
legend.onAdd = () => {{
  const d = L.DomUtil.create('div','legend');
  d.innerHTML = '<div class="count">{len(features)} leads</div>' + `{legend}`;
  return d;
}};
legend.addTo(map);
</script></body></html>"""


def build_map(cfg: Config, out_html: str = "map.html",
              include_non_residential: bool = False) -> None:
    rows, features = _load_features(cfg, include_non_residential)
    if not features:
        print("No homes to map — run the pipeline and enrichment first.")
        return
    with open(out_html, "w") as f:
        f.write(_build_html(cfg, rows, features))
    kind = "all" if include_non_residential else "residential"
    print(f"Wrote {out_html} with {len(features)} {kind} leads.  Open it with:  open {out_html}")
