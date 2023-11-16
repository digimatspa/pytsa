"""
Module for defining trajectory splitting rules.
"""
from typing import Callable
import numpy as np
from inspect import signature
from functools import partial

from ..tsea.structs import AISMessage

class Recipe:
    """
    Rule recipe class.
    =================
    
    This class is used to define a recipe for the TrajectorySplitter class.
    
    """
    Rule = Callable[[list[AISMessage]], bool]
    def __init__(self, *funcs: Rule) -> None:
        self.funcs = funcs
        for func in self.funcs:
            _check_signature(func)
        
    def cook(self) -> Callable[[list[AISMessage]], bool]:
        """
        Cook the recipe into a function that can be passed to the
        TrajectorySplitter class.
        """
        def cooked(track: list[AISMessage]) -> bool:
            return all(func(track) for func in self.funcs)
        
        return cooked
            

# Signature checker-------------------------------------------------------------
def _check_signature(func) -> None:
    """
    Check if the given function has the correct signature.
    """
    if not callable(func):
        raise TypeError(f"Expected a callable, got {type(func)}")
    sig = signature(func)
    if len(sig.parameters) != 1:
        raise TypeError(
            f"Expected a function with exactly one parameter, "
            f"got {len(sig.parameters)} parameters"
        )
    if sig.parameters["track"].annotation != list[AISMessage]:
        raise TypeError(
            f"Expected a function with parameter `track` of type "
            f"list[AISMessage], got {sig.parameters['track'].annotation}"
        )
    if sig.return_annotation != bool:
        raise TypeError(
            f"Expected a function with return type bool, "
            f"got {sig.return_annotation}"
        )

# Rule functions---------------------------------------------------------------
"""
All rule functions must have the following signature:
    def rule_name(track: list[AISMessage], *args, **kwargs) -> bool:
        ...
        
To make a recipe for the TrajectorySplitter class, you are expected to
fix the rule function's arguments, such that only a one-argument function
remains. It is recommended to use the `functools.partial` function for this.
        
Once you have defined a set of rule functions, you can create a recipe
for the TrajectorySplitter class by passing them to the Recipe class.

Example:
    from functools import partial
    from pytsa.trajectories import rules
    
    # Define a recipe
    recipe = rules.Recipe(
        partial(rules.too_few_obs, n=100),
        partial(rules.too_small_span, span=0.1)
    )
    
    # Cook the recipe
    cooked = recipe.cook()
    
The `cooked` function can now be passed to the TrajectorySplitter class
to perform the trajectory splitting.

"""

def too_few_obs(track: list[AISMessage], n: int) -> bool:
    """
    Return True if the length of the track of the given vessel
    is smaller than `n`.
    """
    return len(track) < n

def too_small_spatial_deviation(track: list[AISMessage], sd: float) -> bool:
    """
    Return True if the summed standard deviation of lat/lon 
    of the track of the given vessel is smallerw than `sd`.
    Unit of `sd` is [°].
    """
    sdlon = np.sqrt(np.var([v.lon for v in track]))
    sdlat = np.sqrt(np.var([v.lat for v in track]))
    return (sdlon+sdlat) < sd

def too_small_span(track: list[AISMessage], span: float) -> bool:
    """
    Return True if the lateral and longitudinal span
    of the track of the given vessel is smaller than `span`.
    """
    lat_span = np.ptp([v.lat for v in track])
    lon_span = np.ptp([v.lon for v in track])
    return lat_span > span and lon_span > span

# Example recipe---------------------------------------------------------------
ExampleRecipe = Recipe(
    partial(too_few_obs, n=100),
    partial(too_small_spatial_deviation, sd=0.1)
)