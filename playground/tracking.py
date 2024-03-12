import os
import pickle
from typing import Optional

from cut_detector.data.tools import get_data_path
from cut_detector.utils.trackmate_track import TrackMateTrack
from cut_detector.utils.trackmate_spot import TrackMateSpot

import matplotlib.pyplot as plt
import numpy as np

def load_tracks_and_spots(
    trackmate_tracks_path: str, spots_path: str
) -> tuple[list[TrackMateTrack], list[TrackMateSpot]]:
    """
    Load saved spots and tracks generated from Trackmate xml file.
    """
    trackmate_tracks: list[TrackMateTrack] = []
    for track_file in os.listdir(trackmate_tracks_path):
        with open(os.path.join(trackmate_tracks_path, track_file), "rb") as f:
            trackmate_tracks.append(pickle.load(f))

    spots: list[TrackMateSpot] = []
    for spot_file in os.listdir(spots_path):
        with open(os.path.join(spots_path, spot_file), "rb") as f:
            spots.append(pickle.load(f))

    return trackmate_tracks, spots


def main(
    segmentation_results_path: Optional[str] = os.path.join(
        get_data_path("segmentation_results"), "example_video.bin"
    ),
    trackmate_tracks_path: Optional[str] = os.path.join(
        get_data_path("tracks"), "example_video"
    ),
    spots_path: Optional[str] = os.path.join(
        get_data_path("spots"), "example_video"
    ),
):
    # Load Cellpose results
    with open(segmentation_results_path, "rb") as f:
        cellpose_results = pickle.load(f)

    # TODO: create spots from Cellpose results
    # TODO: perform tracking using laptrack

    # Load TrackMate results to compare... make sure they match!
    trackmate_tracks, trackmate_spots = load_tracks_and_spots(
        trackmate_tracks_path, spots_path
    )
    
    # Frame of interest
    frame = 0

    # Plot cellpose_results
    print(cellpose_results[frame])
    plt.figure()
    plt.imshow(cellpose_results[frame])
    plt.show()
    plt.close()

    # Plot trackmate_spots of frame number "frame"
    y = []
    x = []
    
    for s in trackmate_spots:
        if s.frame == frame:
            list = s.spot_points
            for i in range(len(list)):
                x.append(list[i][0])
                y.append(600 - list[i][1])
    plt.scatter(x,y)
    plt.show()

    # Finding barycenters of each cell
    for i in range(1,2):
        indices = np.where(cellpose_results[frame]==i)
        #print(indices)


if __name__ == "__main__":
    main()

