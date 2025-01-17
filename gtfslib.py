# DART GTFS data: https://www.dart.org/about/about-dart/fixed-route-schedule
# https://www.dart.org/transitdata/latest/google_transit.zip

from collections import defaultdict
from pathlib import Path
import gtfs_kit as gk
import numpy as np
import pandas as pd
from pandas.core.groupby import DataFrameGroupBy
import folium
import geopandas as gpd
from shapely import Point

class Projections:
    WGS84 = 'EPSG:4326'
    GMAPS = 'EPSG:3857'

class CoordsUtil:
    @staticmethod
    def _to_projected_crs(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        if not gdf.crs.is_projected:
            gdf = gdf.to_crs(gdf.estimate_utm_crs())
        return gdf

    @staticmethod
    def buffer_points(distance_meters: float, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        old_crs = None
        gdf = CoordsUtil._to_projected_crs(gdf)
        proj_geoseries: gpd.GeoSeries = gdf.buffer(distance_meters)
        if old_crs:
            proj_geoseries = proj_geoseries.to_crs(old_crs)
        return gpd.GeoDataFrame(geometry=proj_geoseries)

    @staticmethod
    def coord_distance(gdf1: gpd.GeoDataFrame, gdf2: gpd.GeoDataFrame) -> float:
        gdf1 = CoordsUtil._to_projected_crs(gdf1)
        gdf2 = CoordsUtil._to_projected_crs(gdf2)
        return gdf1.distance(gdf2, align=False).iloc[0]

class GTFS:
    def __init__(self, gtfs_file: Path):
        self._feed = gk.read_feed(gtfs_file, dist_units="mi")
        self._feed_description = self.feed.describe().set_index("indicator")["value"].to_dict()
        self._geostops = self.feed.get_stops(as_gdf=True, use_utm=True)
        self._merged_trips_and_stoptimes: DataFrameGroupBy[tuple, True] | None = None
        self._trip_activities_by_dates: dict[tuple[str], pd.DataFrame] = dict()
        self._stops_by_id: gpd.GeoDataFrame = self.stops.set_index("stop_id", drop=False)

    @property
    def feed(self):
        return self._feed

    @property
    def routes(self) -> pd.DataFrame:
        return self.feed.routes

    @property
    def stops(self) -> gpd.GeoDataFrame:
        return self._geostops

    def get_description_value(self, key: str) -> str:
        return self._feed_description[key]

    def get_stop(self, stop_id: str | int) -> gpd.GeoDataFrame:
        df = self._stops_by_id.loc[[str(stop_id)]].copy()
        df.index.set_names('', inplace=True)
        return df

    def get_map(self, route_ids:list[str]=None, color_palette:list[str]=None) -> folium.Map:
        if route_ids is None:
            route_ids = self.routes.route_id.loc[:]
        kwargs = dict()
        if color_palette is not None:
            kwargs["color_palette"] = color_palette
        return self.feed.map_routes(route_ids, show_stops=False, **kwargs)

    def get_stops_in_area(self, area: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """
        Return the subset of ``feed.stops`` that contains all stops that lie
        within the given GeoDataFrame of polygons.
        """
        if self.stops.crs != area.crs:
            area = area.to_crs(self.stops.crs)
        return self.stops.merge(
            gpd.sjoin(self.stops, area)
            .filter(["stop_id"])
        )

    def build_stop_timetable(self, stop_id: str, dates: list[str]) -> pd.DataFrame:
        """
        Return a DataFrame containing the timetable for the given stop ID
        and dates (YYYYMMDD date strings)

        Return a DataFrame whose columns are all those in ``feed.trips`` plus those in
        ``feed.stop_times`` plus ``'date'``, and the stop IDs are restricted to the given
        stop ID.
        The result is sorted by date then departure time.
        
        Adapted from the gtfs_kit.Feed.build_stop_timetable method to use caching of key
        variables and optimize fetching of stops by ID.
        """
        dates = self.feed.subset_dates(dates)
        if not dates:
            return pd.DataFrame()

        if self._merged_trips_and_stoptimes is None:
            merged = pd.merge(
                self.feed.trips, self.feed.stop_times
            )
            self._merged_trips_and_stoptimes = merged.groupby(["stop_id"], sort=False)
        t = self._merged_trips_and_stoptimes.get_group((stop_id,))

        tuple_dates = tuple(dates)
        if tuple_dates not in self._trip_activities_by_dates:
            self._trip_activities_by_dates[tuple_dates] = self.feed.compute_trip_activity(dates)
        a = self._trip_activities_by_dates[tuple_dates]

        frames = []
        for date in dates:
            # Slice to stops active on date
            ids = a.loc[a[date] == 1, "trip_id"]
            f = t[t["trip_id"].isin(ids)].copy()
            f["date"] = date
            frames.append(f)

        f = pd.concat(frames)
        return f.sort_values(["date", "departure_time"])
