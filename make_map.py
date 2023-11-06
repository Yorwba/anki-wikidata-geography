import matplotlib.pyplot as plt

import cartopy.crs as ccrs
import cartopy.feature as cfeature

from country_bounding_boxes import country_subunits_by_iso_code


# chatgpt made this and promised it worked across the date line. 
# after some prompting, it seems to?
def bounding_box_for_points(*points):
    """
    Find the bounding box for a series of lat/lon points, correctly handling the case
    where the bounding box must cross the International Date Line.

    :param points: Any number of tuples of (latitude, longitude)
    :return: Tuple representing the bounding box covering all provided points,
             structured as (min_lat, min_lon, max_lat, max_lon)
    """
    # Initialize min and max latitude to extreme values
    min_lat = min(point[0] for point in points)
    max_lat = max(point[0] for point in points)

    # Normalize longitudes in the range [-180, 180]
    longitudes = [point[1] if point[1] <= 180 else point[1] - 360 for point in points]

    # Sort the normalized longitudes
    sorted_longs = sorted(longitudes)

    # Check if boxes cross the Date Line
    cross_date_line = sorted_longs[-1] - sorted_longs[0] > 180

    if cross_date_line:
        # When crossing the Date Line, we find the segment with the max difference
        # which will indicate the range that is NOT part of the bounding box
        max_gap = max((sorted_longs[i] - sorted_longs[i - 1], i) for i in range(1, len(sorted_longs)))
        gap_index = max_gap[1]

        # Longitudes to the right of the gap
        right_of_gap = sorted_longs[gap_index:]

        # Longitudes to the left of the gap
        left_of_gap = sorted_longs[:gap_index]

        # The bounding box is from the end of the left segment to the start of the right segment
        min_lon = min(right_of_gap)
        max_lon = max(left_of_gap)
    else:
        # No Date Line crossing, just take min and max
        min_lon = min(longitudes)
        max_lon = max(longitudes)

    return min_lat, min_lon, max_lat, max_lon


def make_map(iso_code, lat=None, lon=None, filename='map.png'):
    fig = plt.figure()

    points_to_include = []
    if lat and lon:
        points_to_include.append((lat, lon))

    boxes = [
        c for c in country_subunits_by_iso_code(iso_code)
        # main stuff only
        if c.homepart == 1
    ]
    for box in boxes:
        lon1, lat1, lon2, lat2 = box.bbox
        points_to_include.append((lat1, lon1))
        points_to_include.append((lat2, lon2))

    big_box = bounding_box_for_points(*points_to_include)
    bigger_box = (big_box[0] - 1, big_box[1] - 1, big_box[2] + 1, big_box[3] + 1)

    # if we cross the date line, we need to adjust the central longitude
    central_longitude = 0
    if bigger_box[1] > bigger_box[3]:
        central_longitude = 180

    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree(central_longitude=central_longitude))

    # this wants: (x0, x1, y0, y1)
    ax.set_extent([bigger_box[1], bigger_box[3], bigger_box[0], bigger_box[2]])

    if lat and lon:
        # longitude, latitude...
        ax.plot(lon, lat, 'o', markersize=5, color='red', transform=ccrs.Geodetic())

    ax.add_feature(cfeature.LAND)
    ax.add_feature(cfeature.OCEAN)
    ax.add_feature(cfeature.COASTLINE)
    ax.add_feature(cfeature.BORDERS)
    ax.add_feature(cfeature.LAKES, alpha=0.5)
    ax.add_feature(cfeature.RIVERS)

    plt.savefig(filename, dpi=100, bbox_inches='tight')


if __name__ == '__main__':
    make_map('NZ')
    import os
    os.system('open map.png')