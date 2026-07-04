"""
Solar irradiance source — EU PVGIS typical-meteorological-year (TMY) data.

PVGIS is free and needs no API key, covers the US, and returns hourly GHI/DNI/DHI.
Isolated behind one function so an alternate source (e.g. NREL NSRDB, which would
use ``Config.require_nrel_api_key``) can be dropped in without touching the model.
"""

from __future__ import annotations


def get_tmy(lat: float, lon: float):
    """Fetch a Typical Meteorological Year for a location as a pandas DataFrame with
    pvlib-standard columns (ghi, dni, dhi). One fetch covers all buildings nearby."""
    import pvlib  # lazy

    tmy, _ = pvlib.iotools.get_pvgis_tmy(   # pvlib >=0.11 returns (data, metadata)
        latitude=lat, longitude=lon, outputformat="json",
        usehorizon=True, map_variables=True,
    )
    return tmy
