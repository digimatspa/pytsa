"""
Trajectory Splitter.
====================

This module contains the class for splitting trajectories
according to a set of rules.
"""
import numpy as np
import multiprocessing as mp
from copy import deepcopy

from ..logger import logger
from ..tsea.search_agent import Targets
from ..tsea.targetship import TargetShip, AISMessage
from .rules import Recipe

def print_rejection_rate(n_rejected: int, n_total: int) -> None:
    logger.info(
        f"Filtered {n_total} trajectories. "
        f"{(n_rejected)/n_total*100:.2f}% rejected."
    )

class Inspector:
    """
    The Inspector takes a dictionary of rules 
    and applies them to the trajectories of 
    a `Targets` type dictionary passed to it.
    
    The dict passed to the inspector
    is expected to have the following structure:
        dict[MMSI,TargetVessel]
    and will most likely be the output of the
    :meth:`SearchAgent.get_all_ships()` method.
    """
    def __init__(self, data: Targets, recipe: Recipe) -> None:
        self.data = data
        self.recipe = recipe.cook()
        self.rejected: Targets = {}
        self.accepted: Targets = {}
    
    def inspect(self, njobs: int = 4) -> tuple[Targets,Targets]:
        """
        Inspects TargetShips in `data` and returns two dictionaries:
        - Accepted: Trajectories evalutating to False for the recipe
        - Rejected: Trajectories evalutating to True for the recipe
        
        The accepted and rejected dictionaries can contain the same MMSIs, 
        if the target ship has multiple tracks, and only some of them
        meet the criteria.
        
        """
        if njobs == 1:
            a,r,_n = self._inspect_impl(self.data)
            # Number of target ships after filtering
            n_rejected = sum(len(r.tracks) for r in r.values())
            print_rejection_rate(n_rejected,_n)
            return a,r
        # Split the target ships into `njobs` chunks
        items = list(self.data.items())
        mmsis, target_ships = zip(*items)
        mmsi_chunks = np.array_split(mmsis,njobs)
        target_ship_chunks = np.array_split(target_ships,njobs)
        chunks = []
        for mmsi_chunk, target_ship_chunk in zip(mmsi_chunks,target_ship_chunks):
            chunks.append(dict(zip(mmsi_chunk,target_ship_chunk)))
        
        with mp.Pool(njobs) as pool:
            results = pool.map(self._inspect_impl,chunks)
        accepted, rejected, _n = zip(*results)
        a_out, r_out = {}, {}
        for a,r in zip(accepted,rejected):
            a_out.update(a)
            r_out.update(r)
            
        # Number of target ships after filtering
        n_rejected = sum(len(r.tracks) for r in r_out.values())
        print_rejection_rate(n_rejected,sum(_n))
        
        return a_out, r_out
    
    def _inspect_impl(self, targets: Targets) -> tuple[Targets,Targets,int]:
        """
        Inspector implementation.
        """
        nships = len(targets)
        _n = 0 # Number of trajectories before split
        for i, (_,target_ship) in enumerate(targets.items()):
            logger.info(f"Filtering target ship {i+1}/{nships}")
            for track in target_ship.tracks:
                _n += 1
                if self.recipe(track):
                    self.reject_track(target_ship,track)
                else:
                    self.accept_track(target_ship,track)
        return self.accepted, self.rejected, _n
    
    def reject_track(self,
                     vessel: TargetShip,
                     track: list[AISMessage]) -> None:
        """
        Reject a track.
        """
        self._copy_track(vessel,self.rejected,track)
        
    def accept_track(self,
                     vessel: TargetShip,
                     track: list[AISMessage]) -> None:
        """
        Accept a track.
        """
        self._copy_track(vessel,self.accepted,track)        
    
    def _copy_track(self,
                    vessel: TargetShip, 
                    target: Targets,
                    track: list[AISMessage]) -> None:
        """
        Copy a track from one TargetVessel object to another,
        and delete it from the original.
        """
        if vessel.mmsi not in target:
            target[vessel.mmsi] = deepcopy(vessel)
            target[vessel.mmsi].tracks = []
        target[vessel.mmsi].tracks.append(track)
