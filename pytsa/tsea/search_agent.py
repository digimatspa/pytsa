from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, List, Union
from more_itertools import pairwise
import multiprocessing as mp

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from ..logger import Loader, logger
from ..structs import (
    BoundingBox, TimePosition,
    UNIX_TIMESTAMP
)
from . import split
from ..decode.filedescriptor import (
    BaseColumns, Msg12318Columns, Msg5Columns
)
from ..decode.ais_decoder import decode_from_file
from .targetship import TargetShip, AISMessage, InterpolationError
from ..utils import align_data_files

# Exceptions
class FileLoadingError(Exception):
    pass

# Type aliases
MMSI = int
Targets = dict[MMSI,TargetShip]

def _identity(x):
    return x


class SearchAgent:
    """
    Class searching for target ships
    near a user-provided location.

    To initialize the Agent, you have to specify a 
    datapath, where source AIS Messages are saved as csv files.
    You also have to specify a global frame 
    for the search agent. Outside its borders, no
    search will be commenced.
    """
    
    def __init__(self, 
                 msg12318file: Union[Path,List[Path]],
                 frame: BoundingBox,
                 msg5file: Union[Path,List[Path]],
                 max_tgt_ships: int = 200,
                 preprocessor: Callable[[pd.DataFrame],pd.DataFrame] = _identity,
                 decoded: bool = True,
                 high_accuracy: bool = False) -> None:
       
        """ 
        frame: BoundingBox object setting the search space
                for the taget ship extraction process.
                AIS records outside this BoundingBox are 
                not eligible for TargetShip construction.
        msg12318file: path to a csv file containing AIS messages
                    of type 1,2,3 and 18.
        msg5file: path to a csv file containing AIS messages
                    of type 5.
        max_tgt_ships: maximum number of target ships to retrun

        preproceccor: Preprocessor function for input data. 
                Defaults to the identity.
        decoded: boolean flag indicating whether the input data
                is already decoded or not. If `decoded` is False,
                the input data is decoded on the fly. Please note
                that this process is magnitudes slower than using
                decoded data.
        high_accuracy: boolean flag indicating whether Vincenty's
                formula should be used for distance calculations.
                If `high_accuracy` is False, the Haversine formula
                is used. Defaults to False.
        """

        if not isinstance(msg12318file,list):
            self.msg12318files = [msg12318file]
        else:
            self.msg12318files = msg12318file

        if not isinstance(msg5file,list):
            self.msg5files = [msg5file]
        else:
            self.msg5files = msg5file
        
        # Check if all files are aligned, i.e. if
        # there is a static file for every dynamic file
        # and if the files are in the same order.
        assert align_data_files(self.msg12318files,self.msg5files)

        # Spatial bounding box of current AIS message space
        self.FRAME = frame
        
        self.high_accuracy = high_accuracy
        
        self.spatial_filter = (
            f"{Msg12318Columns.LON.value} > {frame.LONMIN} and "
            f"{Msg12318Columns.LON.value} < {frame.LONMAX} and "
            f"{Msg12318Columns.LAT.value} > {frame.LATMIN} and "
            f"{Msg12318Columns.LAT.value} < {frame.LATMAX}"
        )

        # Maximum number of target ships to extract
        self.max_tgt_ships = max_tgt_ships
        
        # Maximum temporal deviation of target
        # ships from provided time in `init()`.
        # This is used internally to catch
        # all messages around the queried time,
        # for building and interpolating trajectories.
        self.time_delta = 30 # in minutes

        # List of cell-indicies of all 
        # currently buffered cells
        self._buffered_cell_idx = []

        # Custom preprocessor function for input data
        self.preprocessor = preprocessor
        
        # Flag indicating whether the input data
        # is already decoded or not
        self.decoded = decoded
        
        # Length of the original AIS message dataframe
        self._n_original = 0
        # Length of the filtered AIS message dataframe
        self._n_filtered = 0
        
        # Number of times, the _speed_correction function
        # was called
        self._n_speed_correction = 0
        
        # Number of times, the _position_correction function
        # was called
        self._n_position_correction = 0
        
        self._is_initialized = False

    def init(self, tpos: TimePosition)-> None:
        """
        tpos: TimePosition object for which 
                TargetShips shall be extracted from 
                AIS records.
        """
        pos = tpos.position
        if not self._is_initialized:
            # Load AIS Messages for specific cell
            self.dynamic_msgs = self._load_dynamic_messages()
            self.static_msgs = self._load_static_messages()
            self._is_initialized = True
            pos = f"{pos.lat:.3f}N, {pos.lon:.3f}E"
            logger.info(
                "Target Ship search agent initialized at "
                f"{pos}"
            )
        else:
            logger.info("Target Ship search agent already initialized")
            return self 


    def get_ships(self, 
                  tpos: TimePosition, 
                  search_radius: float = 20, # in nautical miles
                  interpolation: str = "linear") -> Targets:
        """
        Returns a list of target ships
        present in the neighborhood of the given position. 
        
        tpos: TimePosition object of agent for which 
                neighbors shall be found
        
        search_radius: Radius around agent in which taget vessel 
                search is rolled out [nm]

        overlap_tpos: boolean flag indicating whether
                we only want to return vessels, whose track overlaps
                with the queried timestamp. If `overlap_tpos` is False,
                all vessels whose track is within the time delta of the
                queried timestamp are returned.
        """
        assert self._is_initialized, \
            "Search agent not initialized. Call `init()` first."
        assert interpolation in ["linear","spline","auto"], \
            "Interpolation method must be either 'linear', 'spline' or 'auto'"
        # Get neighbors
        neigbors = self._get_neighbors(tpos,search_radius)
        tgts = self.construct_target_vessels(neigbors,tpos,True,njobs=1)
        # Contruct Splines for all target ships
        tgts = self._interpolate_trajectories(tgts,mode=interpolation)
        return tgts
    
    def get_all_ships(self,
                      njobs: int = 4, 
                      skip_tsplit: bool = False) -> Targets:
        """
        Returns a dictionary of all target ships in the
        frame of the search agent.
        """
        if not self._is_initialized:
            self.init(TimePosition(self.FRAME.center))
        
        return self.construct_target_vessels(
            self.dynamic_msgs,None,False,njobs,skip_tsplit
        )
    
    def _interpolate_trajectories(self, 
                           tgts: Targets,
                           mode: str = "auto") -> Targets:
        """
        Interpolate all target ships' trajectories.
        """
        for mmsi,tgt in list(tgts.items()):
            try:
                tgt.interpolate(mode) # Construct splines
            except InterpolationError as e:
                logger.warn(e)
                del tgts[mmsi]
        return tgts
    
    def _load_static_messages(self) -> pd.DataFrame:
        """
        Load AIS Messages from a given path or list of paths
        and return only messages that fall inside given `cell`-bounds.
        """
        snippets = []
        for file in self.msg5files:
            logger.info(f"Loading msg5 file '{file}'")
            msg5 = pd.read_csv(
                file,usecols=
                [
                    Msg5Columns.MMSI.value,
                    Msg5Columns.SHIPTYPE.value,
                    Msg5Columns.TO_BOW.value,
                    Msg5Columns.TO_STERN.value
                ]
            )
            snippets.append(msg5)
        msg5 = pd.concat(snippets)
        msg5 = msg5[msg5[Msg5Columns.MMSI.value].isin(self.dynamic_msgs[Msg12318Columns.MMSI.value])]
        return decode_from_file(msg5,None,False) if not self.decoded else msg5

    def _load_dynamic_messages(self) -> pd.DataFrame:
        """
        Load AIS Messages of type 1,2,3,18 from a 
        given path, or list of paths, and return only 
        messages that fall inside ``self.FRAME``.
        """
        snippets = []
        try:
            with Loader(self.FRAME):
                for file in self.msg12318files:
                    msg12318 = pd.read_csv(
                        file,sep=",",
                        usecols=[
                            BaseColumns.TIMESTAMP.value,
                            Msg12318Columns.MMSI.value,
                            Msg12318Columns.LAT.value,
                            Msg12318Columns.LON.value,
                            Msg12318Columns.SPEED.value,
                            Msg12318Columns.COURSE.value
                        ]
                    )
                    self._n_original += len(msg12318)
                    msg12318 = self.preprocessor(msg12318) # Apply custom filter
                    msg12318[BaseColumns.TIMESTAMP.value] = pd.to_datetime(
                        msg12318[BaseColumns.TIMESTAMP.value])
                    msg12318 = msg12318.drop_duplicates(
                        subset=[
                            BaseColumns.TIMESTAMP.value,
                            Msg12318Columns.MMSI.value
                        ], keep="first"
                    )
                    self._n_filtered += len(msg12318)
                    snippets.append(msg12318.query(self.spatial_filter))
        except Exception as e:
            logger.error(f"Error while loading cell data: {e}")
            raise FileLoadingError(e)
        stitched = pd.concat(snippets)
        return decode_from_file(stitched,None,False) if not self.decoded else stitched
            
    def _build_kd_tree(self, data: pd.DataFrame) -> cKDTree:
        """
        Build a kd-tree object from the `Lat` and `Lon` 
        columns of a pandas dataframe.
        """
        assert Msg12318Columns.LAT.value in data and Msg12318Columns.LON.value in data, \
            "Input dataframe has no `lat` or `lon` columns"
        lat=data[Msg12318Columns.LAT.value].values
        lon=data[Msg12318Columns.LON.value].values
        return cKDTree(np.column_stack((lat,lon)))
            
        
    def _get_ship_type(self, 
                       mmsi: int) -> list[int]:
        """
        Return the ship type of a given MMSI number.

        If more than one ship type is found, the first
        one is returned and a warning is logged.
        """
        st = self.static_msgs[self.static_msgs[Msg5Columns.MMSI.value] == mmsi]\
            [Msg5Columns.SHIPTYPE.value].values
        st:np.ndarray = np.unique(st)
        if st.size > 1:
            logger.warning(
                f"More than one ship type found for MMSI {mmsi}. "
                f"Found {st}.")
            return st
        else:
            return list(st)

    def _get_ship_length(self, mmsi: int) -> int:
        """
        Return the ship length of a given MMSI number.

        If more than one ship length is found, the largest
        one is returned and a warning is logged.
        """
        raw = self.static_msgs[self.static_msgs[Msg5Columns.MMSI.value] == mmsi]\
            [[Msg5Columns.TO_BOW.value,Msg5Columns.TO_STERN.value]].values
        sl:np.ndarray = np.sum(raw,axis=1)
        sl = np.unique(sl)
        if sl.size > 1:
            logger.warning(
                f"More than one ship length found for MMSI {mmsi}. "
                f"Found {sl}. Returning {max(sl)}.")
            return max(sl)
        return sl

    def _get_neighbors(self, 
                       tpos: TimePosition, 
                       search_radius: float = 20) -> pd.DataFrame:
        """
        Return all AIS messages that are no more than
        `self.search_radius` [nm] away from the given position.

        Args:
            tpos: TimePosition object of postion and time for which 
                    neighbors shall be found        

        """
        filtered = self._time_filter(
            self.dynamic_msgs,tpos.timestamp,self.time_delta
        )
        # Check if filterd result is empty
        if filtered.empty:
            logger.warning("No AIS messages found in time-filtered cell.")
            return filtered
        tree = self._build_kd_tree(filtered)
        # The conversion to degrees is only accurate at the equator.
        # Everywhere else, the distances get smaller as lines of 
        # Longitude are not parallel. Therefore, 
        # this is a conservative estimate.
        if not search_radius == np.inf:
            sr =  search_radius/60 # Convert to degrees
        else:
            sr = np.inf
        d, indices = tree.query(
            tpos.position.as_list,
            k=self.max_tgt_ships,
            distance_upper_bound=sr
        )
        # Sort out any infinite distances
        res = [indices[i] for i,j in enumerate(d) if j != float("inf")]

        return filtered.iloc[res]
    
    def _distribute(self, df: pd.DataFrame) -> list[pd.DataFrame]:
        """
        Distribute a given dataframe into smaller
        dataframes, each containing only messages
        from a single MMSI.
        """
        mmsis = df[Msg12318Columns.MMSI.value].unique()
        return [df[df[Msg12318Columns.MMSI.value] == mmsi] for mmsi in mmsis]
    
    def _impl_construct_target_vessel(self, 
                                      df: pd.DataFrame, 
                                      skip_tsplit: bool = False) -> Targets:
        """
        Construct a single TargetVessel object from a given dataframe.
        """
        MMSI = df[Msg12318Columns.MMSI.value].unique()
        assert len(MMSI) == 1, \
            "Input dataframe must only contain messages from a single MMSI"
        MMSI = int(MMSI)
        # Initialize TargetVessel object
        tv =  TargetShip(
            ts = None,
            mmsi=MMSI,
            ship_type=self._get_ship_type(MMSI),
            length=self._get_ship_length(MMSI),
            tracks=[[]]
        )
        first = True
            
        df = df.sort_values(by=BaseColumns.TIMESTAMP.value)
        
        for ts,lat,lon,sog,cog in zip(
            df[BaseColumns.TIMESTAMP.value], df[Msg12318Columns.LAT.value],  
            df[Msg12318Columns.LON.value],       df[Msg12318Columns.SPEED.value],
            df[Msg12318Columns.COURSE.value]):
            ts: pd.Timestamp # make type hinting happy
            
            msg = AISMessage(
                sender=MMSI,
                timestamp=int(ts.timestamp()), # Convert to unix
                lat=lat,lon=lon,
                COG=cog,SOG=sog
            )
            if first:
                tv.tracks[-1].append(msg)
                first = False
            else:
                # Split track if change in speed or heading is too large
                if not skip_tsplit:
                    if split.is_split_point(tv.tracks[-1][-1],msg):
                        tv.tracks.append([])
                tv.tracks[-1].append(msg)
        
        # Remove tracks with only one observation
        for track in tv.tracks:
                if len(track) < 2:
                    tv.tracks.remove(track)
          
        tv.find_shell()
        return {MMSI:tv}
    
    def _mp_construct_target_vessels(self, df: pd.DataFrame,
                                     njobs: int = 4,
                                     skip_tsplit: bool = False) -> Targets:
        """
        Adaption from `_construct_target_vessels` with multiprocessing.
        The initial dataframe is split into smaller dataframes, each
        containing only messages from a single MMSI. These dataframes
        are then processed in parallel and the results are merged.
        """
        targets = {}
        single_frames = self._distribute(df)
        with mp.Pool(njobs) as pool:
            results = pool.starmap(
                self._impl_construct_target_vessel,
                [(frame,skip_tsplit) for frame in single_frames]
            )  
        for res in results:
            targets.update(res)
        
        return targets

    def _sp_construct_target_vessels(self, 
                                     df: pd.DataFrame, 
                                     tpos: TimePosition,
                                     overlap: bool) -> Targets:
        """
        Single-process version of `_construct_target_vessels`.
        
        Walk through the rows of `df` and construct a 
        `TargetVessel` object for every unique MMSI. 
        
        The individual AIS Messages are sorted by date
        and are added to the respective TargetVessel's track attribute.
        
        The `overlap` flag indicates whether we only want to return vessels,
        whose track overlaps with the queried timestamp. If `overlap` is False,
        all vessels whose track is within the time delta of the
        queried timestamp are returned.
        
        `max_tgap` is the maximum time gap in seconds between two AIS Messages.
        `max_dgap` is the maximum distance gap in nautical miles between two AIS Messages.
        
        If the temporal or spatial difference between two AIS Messages is too large,
        the track of the target ship is split into two tracks.
        
        """
        df = df.sort_values(by=BaseColumns.TIMESTAMP.value)
        targets: Targets = {}
        
        for mmsi,ts,lat,lon,sog,cog in zip(
            df[Msg12318Columns.MMSI.value], df[BaseColumns.TIMESTAMP.value],
            df[Msg12318Columns.LAT.value],  df[Msg12318Columns.LON.value],
            df[Msg12318Columns.SPEED.value],df[Msg12318Columns.COURSE.value]):
            ts: pd.Timestamp # make type hinting happy
            
            msg = AISMessage(
                sender=mmsi,
                timestamp=ts.to_pydatetime().timestamp(), # Convert to unix
                lat=lat,lon=lon,
                COG=cog,SOG=sog
            )
            
            if mmsi not in targets:
                targets[mmsi] = TargetShip(
                    ts = tpos.timestamp if tpos is not None else None,
                    mmsi=mmsi,
                    ship_type=self._get_ship_type(mmsi),
                    length=self._get_ship_length(mmsi),
                    tracks=[[msg]]
                )
            else:
                # Split track if change in speed or heading is too large
                if split.is_split_point(targets[mmsi].tracks[-1][-1],msg):
                    targets[mmsi].tracks.append([])
                v = targets[mmsi]
                v.tracks[-1].append(msg)

        for tgt in targets.values():
            tgt.find_shell() # Find shell (start/end of traj) of target ship
        
        # Remove target ships with only one observation
        self._remove_single_obs(targets)
        
        if overlap:
            self._filter_overlapping(targets,tpos)
        
        return targets
    
    def construct_target_vessels(self,df: pd.DataFrame,
                                 tpos: TimePosition = None, 
                                 overlap: bool = False, 
                                 njobs: int = 4,
                                 skip_tsplit: bool = False) -> Targets:
        """
        Construct a dictionary of TargetVessel objects
        from a given dataframe.
        """
        if njobs == 1:
            assert tpos is not None, \
                "If `njobs` is 1, `tpos` must not be None"
            return self._sp_construct_target_vessels(df,tpos,overlap)
        else:
            return self._mp_construct_target_vessels(df,njobs,skip_tsplit)
        
    
    def _filter_overlapping(self, 
                            targets: Targets, 
                            tpos: TimePosition) -> None:
        """
        Remove target ships whose track does not overlap
        with the queried timestamp.
        """
        for tgt in targets.values():
            to_keep = []
            for track in tgt.tracks:
                if self._overlaps_search_date(track,tpos):
                    to_keep.append(track)
            tgt.tracks = to_keep

    def _remove_single_obs(self, targets: Targets) -> Targets:
        """
        Remove tracks that only have a single observation.
        """
        for tgt in targets.values():
            to_keep = []
            for track in tgt.tracks:
                if len(track) >= 2:
                    to_keep.append(track)
            tgt.tracks = to_keep
    
    def _corrections(self, targets: Targets) -> Targets:
        """
        Perform corrections on the given targets.
        """
        self._speed_correction(targets)
        self._position_correction(targets)
        return targets
    
    def _break_down_velocity(self,
                             speed: float,
                             course: float) -> tuple[float,float]:
        """
        Break down a given velocity into its
        longitudinal and lateral components.
        """
        return (
            speed * np.cos(np.deg2rad(course)), # Longitudinal component
            speed * np.sin(np.deg2rad(course)) # Lateral component
        )
    
    def _speed_correction(self,
                          targets: Targets) -> Targets:
        """
        Speed correction after 10.1016/j.aap.2011.05.022
        """
        for target in targets.values():
            for msg_t0,msg_t1 in pairwise(target.tracks):
                tms = _time_mean_speed(msg_t0,msg_t1)
                lower_bound = msg_t0.SOG - (msg_t1.SOG - msg_t0.SOG)
                upper_bound = msg_t1.SOG + (msg_t1.SOG - msg_t0.SOG)
                if not lower_bound < tms < upper_bound:
                    self._n_speed_correction += 1
                    logger.warn(
                        f"Speed correction for MMSI {target.mmsi} "
                        f"at {msg_t1.timestamp}"
                    )
                    msg_t1.SOG = tms

    def _position_correction(self,
                             targets: Targets) -> Targets:
        """
        Position correction after 10.1016/j.aap.2011.05.022
        """
        for target in targets.values():
            for msg_t0,msg_t1 in pairwise(target.tracks):
                dt = (msg_t1.timestamp - msg_t0.timestamp)
                sog_lon, sog_lat = self._break_down_velocity(msg_t0.SOG,msg_t0.COG)
                lont1 = msg_t0.lon + (sog_lon *dt)
                latt1 = msg_t0.lat + (sog_lat *dt)
                
                est_pos = np.sqrt(
                    (lont1-msg_t1.lon)**2 + (latt1-msg_t1.lat)**2
                )
                if est_pos <= 0.5*(msg_t1.SOG-msg_t0.SOG)*dt:
                    self._n_position_correction += 1
                    logger.warn(
                        f"Position correction for MMSI {target.mmsi} "
                        f"at {msg_t1.timestamp}"
                    )
                    msg_t1.lon = lont1
                    msg_t1.lat = latt1

    
    def _overlaps_search_date(self, 
                              track: list[AISMessage], 
                              tpos: TimePosition) -> bool:
        """
        Return True if the track of the given vessel
        overlaps with the queried timestamp.
        """
        return (track[0].timestamp < tpos.timestamp < track[-1].timestamp)

    def _time_filter(self, 
                     df: pd.DataFrame, 
                     date: UNIX_TIMESTAMP, 
                     delta: int) -> pd.DataFrame:
        """
        Filter a pandas dataframe to only return 
        rows whose `Timestamp` is not more than 
        `delta` minutes apart from imput `date`.
        """
        assert BaseColumns.TIMESTAMP.value in df, "No `timestamp` column found"
        timezone = df[BaseColumns.TIMESTAMP.value].iloc[0].tz.zone # Get timezone
        date = pd.to_datetime(int(date),unit="s").tz_localize(timezone)
        dt = pd.Timedelta(delta, unit="minutes")
        mask = (
            (df[BaseColumns.TIMESTAMP.value] > (date-dt)) & 
            (df[BaseColumns.TIMESTAMP.value] < (date+dt))
        )
        return df.loc[mask]
    
def _time_mean_speed(msg_t0: AISMessage,msg_t1: AISMessage) -> float:
    """
    Calculate the time-mean speed between two AIS Messages.
    """
    lat_offset = (msg_t1.lat - msg_t0.lat)**2
    lon_offset = (msg_t1.lon - msg_t0.lon)**2
    time_offset = (msg_t1.timestamp - msg_t0.timestamp) # in seconds
    tms = np.sqrt(lat_offset + lon_offset) / (time_offset / 60 / 60) #[deg/h]
    return tms