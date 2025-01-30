import pytsa
from pathlib import Path

def preprocess(df):
    return df[df["speed"] > 0]

if __name__ == '__main__':
    # Global geographic search area.
    # Outside these bounds, no search will be commenced
    frame = pytsa.BoundingBox(
        LATMIN=55.196,  # [°N]
        LATMAX=57.064,  # [°N]
        LONMIN=4.994,   # [°E]
        LONMAX=9.432,   # [°E]
    )

    coordinates = {'latitude': 56.7780335777419, 'longitude': 8.53576037485539}

    # File containing AIS messages
    # Replace with the path to your own data ---v
    dynamic_data = Path("../test/dynamic/2025_01_19.csv")
    static_data = Path("../test/static/2025_01_19.csv")

    # Instantiate the search agent with the source file
    # and the search area
    search_agent = pytsa.SearchAgent(
        dynamic_paths=dynamic_data,
        static_paths=static_data,
        frame=frame,
        preprocessor=preprocess
    )

    found = False
    print("Searching", coordinates['latitude'], coordinates['longitude'])

    # Provide a position and time for which the search
    # will be carried out
    tpos = pytsa.TimePosition(
        timestamp="2025-01-19T05:49:16.000Z",
        lat=coordinates['latitude'],
        lon=coordinates['longitude']
    )

    # Search for TargetVessels with
    # default settings:
    #   Linear interpolation,
    #   20 nm search radius
    target_ships = search_agent.freeze(tpos, search_radius=20, alpha=0.02)

    # Extract the current position, speed and
    # course for all found target vessels.
    for ship in target_ships.values():
        print("Found", ship.mmsi, ship.observe())
        found = True

    if not found:
        print("Not found")
