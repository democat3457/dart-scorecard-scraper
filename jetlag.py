from __future__ import annotations

from dataclasses import dataclass
import functools
import heapq
import itertools

import pandas as pd
import shapely
from tqdm import tqdm
from gtfslib import GTFS, CoordsUtil, Projections, RouteType
from pathlib import Path
import folium
from datetime import datetime, timedelta, date, time
import time as pytime

import requests
import shutil

START_TIME = datetime(2024, 12, 16, 9, 0, 0)
HIDE_DURATION = timedelta(minutes=90)
START_STOP = 22750 # Akard
WALKING_SPEED = 1.06 # m/s
ALLOWED_TRAVEL_MODES = RouteType.all()
ALLOWED_HIDING_MODES = [ RouteType.LIGHT_RAIL ]

def _dateformat(d):
    return d.strftime('%Y%m%d')

today = _dateformat(START_TIME)
end_time = START_TIME + HIDE_DURATION

data_folder = Path("data")
if not data_folder.exists():
    data_folder.mkdir()
export_folder = Path("export")
if not export_folder.exists():
    export_folder.mkdir()
file = Path(data_folder) / "google_transit.zip"

print("Initializing GTFS...")
_init_time = pytime.time()

download_file = True
if file.exists():
    gtfs = GTFS(file)
    _startdate = datetime.strptime(gtfs.get_description_value("start_date"), '%Y%m%d').date()
    _enddate = datetime.strptime(gtfs.get_description_value("end_date"), '%Y%m%d').date()
    if _startdate <= date.today() <= _enddate:
        download_file = False

if download_file:
    print("Downloading DART GTFS zip file...")
    r = requests.get(
        "https://www.dart.org/transitdata/latest/google_transit.zip", stream=True
    )
    if r.status_code == 200:
        with file.open("wb") as out_file:
            r.raw.decode_content = True
            shutil.copyfileobj(r.raw, out_file)
    gtfs = GTFS(file)

# Access properties to cache elements
gtfs.stop_route_types
gtfs.stop_names

_init_time_stop = pytime.time()
print(f"Finished initialization in {_init_time_stop-_init_time:.2f}s.")

StopId = str | int
TripId = str | int
StopSeq = int
Timeish = time | timedelta

def timedelta_coerce(t: Timeish):
    if isinstance(t, (timedelta, pd.Timedelta)):
        return t
    return datetime.combine(date.min, t) - datetime.combine(date.min, time())

def timeish_hms_colon_str(t: Timeish):
    td_sec = timedelta_coerce(t).seconds
    h,m,s = td_sec // 3600, (td_sec % 3600) // 60, td_sec % 60
    return f'{h:02d}:{m:02d}:{s:02d}'

def timeish_minsec_str(t: Timeish):
    td_sec = timedelta_coerce(t).seconds
    m,s = td_sec // 60, td_sec % 60
    return f'{m}m{s:02d}s'

def dt_minus_date(dt: datetime, d: date):
    return dt - datetime.combine(d, time())

@functools.lru_cache()
def get_stop_timetable(stop: StopId):
    tt = gtfs.build_stop_timetable(str(stop), [today])
    tt["arrival_time"] = pd.to_timedelta(tt["arrival_time"])
    tt["departure_time"] = pd.to_timedelta(tt["departure_time"])
    return tt


def df_time_bound(df: pd.DataFrame, lower: Timeish | None = None, upper: Timeish | None = None):
    mask = pd.notna(df["departure_time"]) & pd.notna(df["arrival_time"])
    if lower:
        mask &= df["departure_time"] >= timedelta_coerce(lower)
    if upper:
        mask &= df["arrival_time"] <= timedelta_coerce(upper)
    return df[mask]

def trips_between_for_stop(stop: StopId, t1: Timeish, t2: Timeish):
    tt = get_stop_timetable(stop)
    return df_time_bound(tt, t1, t2)


stop_times: pd.DataFrame = gtfs.feed.stop_times
stop_times["arrival_time"] = pd.to_timedelta(stop_times["arrival_time"])
stop_times["departure_time"] = pd.to_timedelta(stop_times["departure_time"])
stop_times_by_trip = stop_times.groupby(["trip_id"], sort=False)

def get_future_stops_on_trip(trip: TripId, stop_seq: StopSeq = 0):
    st = stop_times_by_trip.get_group((str(trip),))
    return st[st["stop_sequence"] > int(stop_seq)]


class RouteSegmentCollection:
    @dataclass
    class RouteSegment:
        departure_td: timedelta
        arrival_td: timedelta
        route_name: str
        arrival_stop_id: StopId

    # TODO use special route segment flags rather than checking route names
    STARTING_ROUTE_NAME = "__start__"

    def __init__(self, day: date, *trips: RouteSegment):
        self.day = day
        self.trips = trips

    def append(self, departure_td: timedelta, arrival_td: timedelta, route_name: str, arrival_stop_id: StopId) -> RouteSegmentCollection:
        return self.append_(RouteSegmentCollection.RouteSegment(departure_td, arrival_td, route_name, arrival_stop_id))

    def append_(self, trip: RouteSegment) -> RouteSegmentCollection:
        return RouteSegmentCollection(self.day, *self.trips, trip)

    def get_last_trip(self) -> RouteSegment | None:
        if not len(self.trips):
            return None
        return self.trips[-1]

    def get_arrival_dt(self) -> datetime | None:
        if (last_trip := self.get_last_trip()) is None:
            return None
        return datetime.combine(self.day, time()) + last_trip.arrival_td

    def populate_waiting(self) -> RouteSegmentCollection:
        segments = [self.trips[0]]
        for a, b in itertools.pairwise(self.trips):
            if a.arrival_td != b.departure_td:
                wait_route_name = f'Wait at stop'
                segments.append(RouteSegmentCollection.RouteSegment(a.arrival_td, b.departure_td, wait_route_name, a.arrival_stop_id))
            segments.append(b)
        return RouteSegmentCollection(self.day, *segments)

    def to_str(self, sep: str = "\n") -> list[str]:
        route_text = [
            f'{gtfs.stop_names[str(self.get_last_trip().arrival_stop_id)]}',
            f'Arrival time: {self.get_arrival_dt().strftime("%m/%d %H:%M:%S")}',
            "",
            "Steps:",
        ]
        for segment in self.trips:
            arrival_stop_name = gtfs.stop_names[str(segment.arrival_stop_id)]
            if segment.route_name == self.__class__.STARTING_ROUTE_NAME:
                route_text.append(f"{timeish_hms_colon_str(segment.departure_td)} Start at {arrival_stop_name}")
            else:
                route_str = segment.route_name
                if not (route_str.startswith("Walk ") or route_str.startswith("Wait ")):
                    route_str = "Take " + route_str
                route_text.append(f" - ({timeish_minsec_str(segment.arrival_td - segment.departure_td)}) {route_str}")
                route_text.append(f"{timeish_hms_colon_str(segment.arrival_td)} {arrival_stop_name}")
        return sep.join(route_text)

    @classmethod
    def starting_collection(cls, start_dt: datetime, start_stop_id: StopId):
        td = timedelta_coerce(start_dt.time())
        return cls(start_dt.date()).append(td, td, cls.STARTING_ROUTE_NAME, start_stop_id)

    def __str__(self):
        return str(self.trips)

    def __iter__(self):
        return iter(self.trips)

    def __len__(self):
        return len(self.trips)

    def __get_cmp_key(self):
        if not len(self.trips):
            raise ValueError("route collection needs a segment to compare against")
        return (self.get_last_trip().arrival_td, len(self.trips))

    def __lt__(self, other):
        if not isinstance(other, RouteSegmentCollection):
            raise TypeError
        return self.__get_cmp_key() < other.__get_cmp_key()

    def __gt__(self, other):
        if not isinstance(other, RouteSegmentCollection):
            raise TypeError
        return self.__get_cmp_key() > other.__get_cmp_key()

    def __hash__(self):
        return hash(self.trips)

    def __eq__(self, other):
        return isinstance(other, RouteSegmentCollection) and self.trips == other.trips


visited_stops: dict[str, RouteSegmentCollection] = dict() # stop_id : fastest route combo
visited_trips: set[str] = set()

added_stops: dict[str, timedelta] = dict() # temp dict to stop adding to queue

end_timedelta = dt_minus_date(end_time, START_TIME.date())


queue = [RouteSegmentCollection.starting_collection(START_TIME, str(START_STOP))]
heapq.heapify(queue)

def push_to_queue(route_collection: RouteSegmentCollection):
    stop_id, arrival_time = route_collection.get_last_trip().arrival_stop_id, route_collection.get_last_trip().arrival_td
    if stop_id in added_stops:
        if arrival_time > added_stops[stop_id]:
            # if stop has already been added and the tentative time is later than the already queued time, skip
            return
    heapq.heappush(queue, route_collection)
    added_stops[stop_id] = arrival_time

t = tqdm()
while len(queue):
    t.set_description(str(len(queue)), refresh=False)
    t.update()
    route_collection = heapq.heappop(queue)
    td, stop_id = route_collection.get_last_trip().arrival_td, route_collection.get_last_trip().arrival_stop_id
    if stop_id in visited_stops:
        continue
    if td > end_timedelta:
        continue
    visited_stops[stop_id] = route_collection
    stop_timetable = trips_between_for_stop(stop_id, td, end_timedelta)
    first_available_routes = stop_timetable.drop_duplicates('route_id', keep='first')
    # print(stop_timetable)
    for _, row_gdf in first_available_routes.iterrows():
        trip_id, stop_seq, trip_name = row_gdf["trip_id"], row_gdf["stop_sequence"], row_gdf["trip_headsign"]
        departure_time = timedelta_coerce(row_gdf["departure_time"])

        # only travel in allowed route types
        if gtfs.trip_route_types[trip_id] not in ALLOWED_TRAVEL_MODES:
            continue

        if trip_id in visited_trips:
            continue
        visited_trips.add(trip_id)

        for _, future_stop in get_future_stops_on_trip(trip_id, stop_seq).iterrows():
            arrival_time = timedelta_coerce(future_stop["arrival_time"])
            if arrival_time > end_timedelta:
                continue
            future_stop_id = future_stop["stop_id"]
            push_to_queue(route_collection.append(departure_time, arrival_time, trip_name, future_stop_id))

    # if we had just walked, walking again is not going to provide new stations
    if route_collection.get_last_trip().route_name.startswith("Walk "):
        continue

    # walking calculation
    if WALKING_SPEED <= 0:
        continue
    remaining_time = end_timedelta - td
    walking_distance = WALKING_SPEED * remaining_time.seconds
    stop_df = gtfs.get_stop(stop_id)
    buffered_area = CoordsUtil.buffer_points(walking_distance, stop_df)
    stop_geometry = stop_df.iloc[0]["geometry"]
    stops_in_area = gtfs.get_stops_in_area(buffered_area)
    for _, row in stops_in_area.iterrows():
        distance_to_stop = shapely.distance(stop_geometry, row["geometry"])
        arrival_time = td + (distance_to_stop / WALKING_SPEED * timedelta(seconds=1))
        future_stop_id = row["stop_id"]
        push_to_queue(route_collection.append(td, arrival_time, f"Walk {round(distance_to_stop)} meters", future_stop_id))

t.close()

print(f'Evaluated {len(visited_trips)} trips and found {len(visited_stops)} reachable stops.')

# import pprint
# pprint.pprint(visited_stops)

m = folium.Map(location=[32.7769, -96.7972], zoom_start=10)

for stop_id, route_collection in visited_stops.items():
    stop = gtfs.get_stop(stop_id).to_crs(Projections.WGS84).iloc[0]
    name, point = stop["stop_name"], stop.geometry
    lon, lat = point.x, point.y
    is_valid_hiding_spot = any(
        rtype in ALLOWED_HIDING_MODES
        for rtype in gtfs.stop_route_types[stop_id]
    )

    popup = folium.Popup(
        route_collection.populate_waiting().to_str(sep='<br>'),
        max_width=300
    )

    folium.Circle(
        location=[lat, lon],
        tooltip=name,
        popup=popup,
        fill_color="#00f" if is_valid_hiding_spot else "#f00",
        fill_opacity=0.2,
        color="black",
        weight=1,
        radius=804.672 if is_valid_hiding_spot else 20,
    ).add_to(m)

m.save("jetlag.html")
