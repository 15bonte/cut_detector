import os
from random import shuffle
from typing import Literal, Optional, Callable
from math import sqrt
import numpy as np
from bigfish import stack, detection
from skimage.morphology import extrema, opening
from skimage.feature import blob_log, blob_dog, blob_doh
from scipy import ndimage
from scipy.optimize import linear_sum_assignment
from scipy.spatial import distance
import matplotlib as mpl
import matplotlib.pyplot as plt
import pandas as pd
from laptrack import LapTrack

from cnn_framework.utils.display_tools import display_progress

from ..constants.tracking import CYTOKINESIS_DURATION
from ..utils.mid_body_track import MidBodyTrack
from ..utils.image_tools import smart_cropping
from ..utils.mid_body_spot import MidBodySpot
from ..utils.mitosis_track import MitosisTrack
from ..utils.trackmate_track import TrackMateTrack
from ..utils.tools import plot_detection

from ..mb_tracking import SpatialLapTrack
from .mb_support import detection


class MidBodyDetectionFactory:
    """
    Class to perform mid-body detection, tracking and filtering.

    Args:
        weight_mklp_intensity_factor (float): Weight of intensity in spot dist calculation
            (cf TrackMate).
        weight_sir_intensity_factor (float): Weight of sir intensity in spot distance calculation.
        mid_body_linking_max_distance (int): Maximum distance between two mid-bodies to link them.

        h_maxima_threshold (float): Threshold for h_maxima detection (default).

        sigma (float): Sigma for bigfish detection (unused).
        threshold (float): Threshold for bigfish detection (unused).

        cytokinesis_duration (int): Number of frames to look for mid-body in between cells.
        minimum_mid_body_track_length (int): Minimum spots in mid-body track to consider it.
    """

    def __init__(
        self,
        weight_mklp_intensity_factor=5.0,
        weight_sir_intensity_factor=1.50,
        # weight_sir_intensity_factor=15,
        mid_body_linking_max_distance=175,
        # mid_body_linking_max_distance=100,
        # mid_body_linking_max_distance=1000,
        h_maxima_threshold=5.0,
        sigma=2.0,
        threshold=1.0,
        cytokinesis_duration=CYTOKINESIS_DURATION,
        minimum_mid_body_track_length=10,
    ) -> None:
        self.weight_mklp_intensity_factor = weight_mklp_intensity_factor
        self.weight_sir_intensity_factor = weight_sir_intensity_factor
        self.mid_body_linking_max_distance = mid_body_linking_max_distance
        self.h_maxima_threshold = h_maxima_threshold
        self.sigma = sigma
        self.threshold = threshold
        self.cytokinesis_duration = cytokinesis_duration
        self.minimum_mid_body_track_length = minimum_mid_body_track_length

    SPOT_DETECTION_MODE = Literal[
        "bigfish", 
        "h_maxima", 
        "cur_log",
        "lapgau",
        "log2_wider",
        "shifted_centered_log"
        "cur_dog",
        "diffgau", 
        "cur_doh"
        "hessian", 
    ]

    def update_mid_body_spots(
        self,
        mitosis_track: MitosisTrack,
        mitosis_movie: np.array,
        mask_movie: np.array,
        tracks: list[TrackMateTrack],
        mb_detect_method: SPOT_DETECTION_MODE | Callable[[np.ndarray], np.ndarray] = "lapgau",
        mb_tracking_method: Literal["laptrack", "spatial_laptrack"] = "laptrack",
    ) -> None:
        """
        Get spots of best mitosis track.

        Parameters
        ----------
        mitosis_movie: TYXC
        mask_movie: TYX

        """

        spots_candidates = self.detect_mid_body_spots(
            mitosis_movie, mask_movie=mask_movie,
            mode=mb_detect_method
        )
        mid_body_tracks = self.generate_tracks_from_spots(
            spots_candidates,
            tracking_method=mb_tracking_method
        )
        kept_track = self._select_best_track(
            mitosis_track, mid_body_tracks, tracks, mitosis_movie
        )

        if kept_track is None:
            return

        # Keep only spots of best mitosis track
        for rel_frame, spot in kept_track.spots.items():
            frame = rel_frame + mitosis_track.min_frame
            mitosis_track.mid_body_spots[frame] = spot

    def detect_mid_body_spots(
        self,
        mitosis_movie: np.array,
        mask_movie: Optional[np.array] = None,
        mid_body_channel=1,
        sir_channel=0,
        mode: SPOT_DETECTION_MODE | Callable[[np.ndarray], np.ndarray] = "diffgau",
    ) -> dict[int, list[MidBodySpot]]:
        """
        Parameters
        ----------
        mitosis_movie: TYXC
        mask_movie: TYX

        Returns
        ----------
        spots_dictionary: dict[int, list[MidBodySpot]]
        """

        # Default mask is all ones
        if mask_movie is None:
            mask_movie = np.ones(mitosis_movie.shape[:-1])

        # Detect spots in each frame
        spots_dictionary = {}
        nb_frames = mitosis_movie.shape[0]
        for frame in range(nb_frames):
            display_progress(
                "Detect mid-body spots...",
                frame + 1,
                nb_frames,
                additional_message=f"Frame {frame + 1}/{nb_frames}",
            )

            mitosis_frame = mitosis_movie[frame, :, :, :].squeeze()  # YXC
            mask_frame = mask_movie[frame, :, :].squeeze()  # YX
            spots = self._spot_detection(
                mitosis_frame,
                mask_frame,
                mid_body_channel,
                sir_channel,
                mode=mode,
                frame=frame,
            )

            # Update dictionary
            spots_dictionary[frame] = spots

        return spots_dictionary


    def _spot_detection(
        self,
        image: np.array,
        mask: np.array,
        mid_body_channel: int,
        sir_channel: int,
        mode: SPOT_DETECTION_MODE | Callable[[np.ndarray], np.ndarray],
        frame=-1,
        log_blob_spot: bool = True,
    ) -> list[MidBodySpot]:
        """
        Mode 'bigfish'
            threshold_1: sigma for log filter
            threshold_2: threshold for spots detection

        Mode 'h_maxima'
            threshold_1: threshold for h_maxima
            threshold_2: unused
        """

        image_sir = image[:, :, sir_channel]
        image_mklp = image[:, :, mid_body_channel]  #

        if callable(mode):
            # directly passsing a blob-like function
            spots = [
                (int(spot[0]), int(spot[1], int(spot[2])))
                for spot in mode(image_mklp)
            ]
            if log_blob_spot:
                for s in spots:
                    print(f"found x:{s[1]}  y:{s[0]}  s:{s[2]}")

        elif mode in [
                "cur_log", "lapgau", "log2_wider", "shifted_centered_log",
                "cur_dog", "diffgau"
                "cur_doh", "hessian"
                ]:
            # blob-like function called referenced by name
            
            mapping = {
                "cur_log": detection.current_log,
                "cur_dog": detection.current_dog,
                "cur_doh": detection.current_doh,

                "lapgau": detection.lapgau,
                "log2_wider": detection.log2_wider,
                "rshit_log": detection.rshift_log,
                
                "diffgau": detection.diffgau,

                "hessian": detection.hessian
            }

            spots = [
                (int(spot[0]), int(spot[1], int(spot[2])))
                for spot in mapping[mode](image_mklp)
            ]

            if log_blob_spot:
                for s in spots:
                    print(f"found x:{s[1]}  y:{s[0]}  s:{s[2]}")

        elif mode == "bigfish":
            # Spots detection with bigfish functions
            filtered_image = stack.log_filter(image_mklp, sigma=self.sigma)
            # Filter out spots which are not maximal or outside convex hull
            spots_mask = (filtered_image > 0) * mask
            # If mask is empty, skip frame
            if np.sum(spots_mask) == 0:
                spots = np.array([], dtype=np.int64).reshape(
                    (0, filtered_image.ndim)
                )
            else:
                spots, _ = detection.spots_thresholding(
                    filtered_image,
                    spots_mask.astype(bool),
                    threshold=self.threshold,
                )

        elif mode == "h_maxima":
            # Perform opening followed by closing to remove small spots
            filtered_image = opening(image_mklp, footprint=np.ones((3, 3)))
            # Get local maxima using h_maxima
            local_maxima = extrema.h_maxima(
                filtered_image, self.h_maxima_threshold
            )
            # Label spot regions
            labeled_local_maxima, nb_labels = ndimage.label(
                local_maxima, structure=np.ones((3, 3))
            )
            # Remove inconsistent labels
            # Threshold is computed as 99th percentile of image
            filtering_threshold = np.quantile(image_mklp.flatten(), 0.99)
            for label in range(1, nb_labels + 1):
                # Labels intensity in original image has to be higher than threshold
                if (
                    image_mklp[np.where(labeled_local_maxima == label)].mean()
                    < filtering_threshold
                ):
                    labeled_local_maxima[labeled_local_maxima == label] = 0
            # Re-label accordingly
            labeled_local_maxima, nb_labels = ndimage.label(
                labeled_local_maxima > 0, structure=np.ones((3, 3))
            )
            # Get center of mass to locate spots
            spots = ndimage.center_of_mass(
                local_maxima, labeled_local_maxima, range(1, nb_labels + 1)
            )
            spots = np.asarray(spots, dtype=np.int64)

            # Here, do something to retrieve mid_body area and/or circularity...

            if len(spots) == 0:
                spots = np.array([], dtype=np.int64).reshape(
                    (0, filtered_image.ndim)
                )

        # elif mode == "lapgau":
        #     # raise "Laplacian of Gaussian not implemtented yet"
        #     spots = [
        #         (int(spot[0]), int(spot[1]))
        #         for spot in self._compute_laplacian_of_gaussian(image_mklp)
        #     ]

        # elif mode == "log2_wider":
        #     spots = [
        #         (int(spot[0]), int(spot[1]))
        #         for spot in self._compute_any_laplacian_of_gaussian(
        #             image_mklp,
        #             min_sigma=2,
        #             max_sigma=8,
        #             num_sigma=4,
        #             threshold=0.1
        #         )
        #     ]

        # elif mode == "off_centered_log":
        #     spots = [
        #         (int(spot[0]), int(spot[1]))
        #         for spot in self._compute_any_laplacian_of_gaussian(
        #             image_mklp,
        #             min_sigma=3,
        #             max_sigma=11,
        #             num_sigma=5,
        #             threshold=0.1
        #         )
        #     ]

        # elif mode == "diffgau":
        #     spots = [
        #         (int(spot[0]), int(spot[1]))
        #         for spot in self._compute_diff_of_gaussian(image_mklp)
        #     ]

        # elif mode == "hessian":
        #     spots = [
        #         (int(spot[0]), int(spot[1]))
        #         for spot in self._compute_det_of_hessian(image_mklp)
        #     ]

        else:
            raise ValueError(f"Unknown mode: {mode}")


        # WARNING:
        # spots can be a list of Tuple with 2 or 3 values:
        # 2 values: (y, x) if h_maxima or fish_eye used
        # 3 values: (y, x, sigma) if any blob-based method used
        mid_body_spots = [
            MidBodySpot(
                frame,
                # Convert spots to MidBodySpot objects (switch (y, x) to (x, y))
                x=position[1],
                y=position[0],
                intensity=self._get_average_intensity(position, image_mklp),
                sir_intensity=self._get_average_intensity(position, image_sir),
            )
            for position in spots
        ]

        return mid_body_spots
    


    # @staticmethod
    # def _compute_laplacian_of_gaussian(
    #     midbody_gs_img: np.array,
    # ) -> np.array:  # 2 dimensions, blob and Y X R
    #     # midbody_gs_img = midbody_gs_img / np.max(midbody_gs_img)
    #     midbody_gs_img = (midbody_gs_img - np.min(midbody_gs_img)) / (
    #         np.max(midbody_gs_img) - np.min(midbody_gs_img)
    #     )
    #     blobs_log = blob_log(
    #         midbody_gs_img,
    #         min_sigma=5,
    #         max_sigma=10,
    #         num_sigma=5,
    #         threshold=0.1,
    #     )
    #     print("found blobs (y/x/s):", blobs_log, sep="\n")

    #     # Compute radii in the 3rd column, since 3 column is sigma
    #     # and radius can be approximated by sigma * sqrt(2) according to doc
    #     blobs_log[:, 2] = blobs_log[:, 2] * sqrt(2)
    #     return blobs_log
    
    # @staticmethod
    # def _compute_any_laplacian_of_gaussian(
    #     mklp_img: np.array,
    #     min_sigma: int,
    #     max_sigma: int,
    #     num_sigma: int,
    #     threshold: float
    #     ) -> np.array:
    #     """ Computes a MinMax Normalization followed by a laplacian
    #     of gaussian with the given parameters
    #     """
    #     min = np.min(mklp_img)
    #     max = np.max(mklp_img)
    #     mklp_img = (mklp_img-min) / (max-min)
    #     blobs = blob_log(
    #         image=mklp_img,
    #         min_sigma=min_sigma,
    #         max_sigma=max_sigma,
    #         num_sigma=num_sigma,
    #         threshold=threshold
    #     )
    #     print("found blobs (y/x/s):", blobs, sep="\n")
    #     blobs[:, 2] = blobs[:, 2] * sqrt(2)
    #     return blobs
    
    # @staticmethod
    # def _compute_diff_of_gaussian(
    #     midbody_gs_img: np.array,
    # ) -> np.array:
    #     min = np.min(midbody_gs_img)
    #     max = np.max(midbody_gs_img)
    #     midbody_gs_img = (midbody_gs_img - min) / (max - min)
    #     blobs = blob_dog(
    #         image=midbody_gs_img,
    #         min_sigma=2,
    #         max_sigma=5,
    #         sigma_ratio=1.2,
    #         threshold=0.1,
    #     )
    #     print("found blobs (y/x/s):", blobs, sep="\n")
    #     blobs[:, 2] = blobs[:, 2] * sqrt(2)
    #     return blobs
    
    # @staticmethod
    # def _compute_det_of_hessian(
    #     midbody_gs_img: np.array
    # ) -> np.array:
    #     min = np.min(midbody_gs_img)
    #     max = np.max(midbody_gs_img)
    #     midbody_gs_img = (midbody_gs_img - min) / (max - min)
    #     blobs = blob_doh(
    #         midbody_gs_img,
    #         min_sigma=5,
    #         max_sigma=10,
    #         num_sigma=5,
    #         threshold=0.0040,
    #     )
    #     print("found blobs (y/x/s):", blobs, sep="\n")
    #     blobs[:, 2] = blobs[:, 2] * sqrt(2)
    #     return blobs

    @staticmethod
    def _get_average_intensity(
        position: tuple[int], image: np.array, margin=1
    ) -> int:
        """
        Parameters
        ----------
        position: (y, x)
        image: YX
        margin: int

        Returns
        ----------
        average_intensity: int
        """
        # Get associated crop
        crop = smart_cropping(
            image,
            margin,
            position[1],
            position[0],
            position[1] + 1,
            position[0] + 1,
        )

        # Return average intensity
        return int(np.mean(crop))

    def _update_spots_hereditary(
        self, spots1: list[MidBodySpot], spots2: list[MidBodySpot]
    ) -> None:
        """
        Link spots together using Hungarian algorithm.
        """
        # Ignore empty spots list
        if len(spots1) == 0 or len(spots2) == 0:
            return

        # Create cost matrix
        # https://imagej.net/plugins/trackmate/algorithms
        cost_matrix = np.zeros(
            (len(spots1) + len(spots2), len(spots1) + len(spots2))
        )
        max_cost = 0
        for i, spot1 in enumerate(spots1):
            for j, spot2 in enumerate(spots2):
                intensity_penalty = (
                    3
                    * self.weight_mklp_intensity_factor
                    * np.abs(spot1.intensity - spot2.intensity)
                    / (spot1.intensity + spot2.intensity)
                )
                sir_intensity_penalty = (
                    3
                    * self.weight_sir_intensity_factor
                    * np.abs(spot1.sir_intensity - spot2.sir_intensity)
                    / (spot1.sir_intensity + spot2.sir_intensity)
                )
                penalty = 1 + intensity_penalty + sir_intensity_penalty
                distance = spot1.distance_to(spot2)
                if distance > self.mid_body_linking_max_distance:
                    cost_matrix[i, j] = np.inf
                else:
                    # Compared to original TrackMate algorithm, remove square to penalize no attribution to the closest spot
                    cost_matrix[i, j] = (penalty * distance) ** 1
                    max_cost = max(max_cost, cost_matrix[i, j])

        min_cost = (
            0
            if np.max(cost_matrix) == 0
            else np.min(cost_matrix[np.nonzero(cost_matrix)])
        )

        cost_matrix[len(spots1) :, : len(spots2)] = (
            max_cost * 1.05
        )  # bottom left
        cost_matrix[: len(spots1), len(spots2) :] = (
            max_cost * 1.05
        )  # top right
        cost_matrix[len(spots1) :, len(spots2) :] = min_cost  # bottom right

        # Hungarian algorithm
        row_ind, col_ind = linear_sum_assignment(cost_matrix)

        # Update parent and child spots
        for i, j in zip(row_ind, col_ind):
            if i < len(spots1) and j < len(spots2):
                spots1[i].child_spot = spots2[j]
                spots2[j].parent_spot = spots1[i]

    TRACKING_MODE = Literal["laptrack", "spatial_laptrack"]
    
    def generate_tracks_from_spots(
        self, 
        spots_candidates: dict[int, list[MidBodySpot]],
        tracking_method: TRACKING_MODE = "laptrack",
        show_tracking: bool = False,
        use_custom_laptrack: LapTrack | None = None
    ) -> list[MidBodyTrack]:
        """
        Use spots linked together to generate tracks.

        Parameters
        ----------
        spots_candidates : {frame: [MidBodySpot]}

        Returns
        ----------
        tracks: [MidBodyTrack]

        """

        return self._gen_laptrack_tracking(
            spots_candidates, 
            tracking_method, 
            show_tracking,
            use_custom_laptrack
        )

    def _gen_laptrack_tracking(
        self, 
        spots_candidates: dict[int, list[MidBodySpot]],
        tracking_method: TRACKING_MODE = "laptrack",
        show_tracking: bool = False,
        use_custom_laptrack: LapTrack | None = None
    ) -> list[MidBodyTrack]:
        
        spots_df = pd.DataFrame(
            {
                "frame": [],
                "x": [],
                "y": [],
                "mlkp_intensity": [],
                "sir_intensity": [],
            }
        )
        for frame, mb_spots in spots_candidates.items():
            if len(mb_spots) == 0:
                spots_df.loc[len(spots_df.index)] = [
                    frame,
                    None,
                    None,
                    None,
                    None,
                ]
            else:
                for mb_spot in mb_spots:
                    spots_df.loc[len(spots_df.index)] = [
                        frame,
                        mb_spot.x,
                        mb_spot.y,
                        mb_spot.intensity,
                        mb_spot.sir_intensity,
                    ]
        print("spots_df:", spots_df, sep="\n")

        # distance function:
        def dist_metric(c1, c2):
            """Modified version of sqeuclidian distance

            Square Euclidian distance is applied to spatial coordinates
            x and y.
            while an 'intensity' distance is computed with MLKP and
            SIR intensities

            Finally values are combined by weighted addition
            """

            # unwrapping
            (x1, y1, mlkp1, sir1), (x2, y2, mlkp2, sir2) = c1, c2

            # In case we have a None None point:
            # if x1 is None or x2 is None:
            #     return self.mid_body_linking_max_distance*2 # connection is invalid
            if np.isnan([x1, y1, x2, y2]).any():
                return self.mid_body_linking_max_distance*2 # connection is invalidated

            # spatial coordinates: euclidean
            spatial_e = distance.euclidean([x1, y1], [x2, y2])

            mkpl_penalty = (
                3
                * self.weight_mklp_intensity_factor
                * np.abs(mlkp1 - mlkp2) / (mlkp1 + mlkp2)
            )

            sir_penalty = (
                3
                * self.weight_sir_intensity_factor
                * np.abs(sir1 - sir2) / (sir1 + sir2)
            )

            penalty = (
                1
                + sir_penalty
                + mkpl_penalty
            )
            # penalty = 1 + mkpl_penalty + sir_penalty

            return (spatial_e * penalty)**2 
            # return penalty**2 

        
        max_distance = self.mid_body_linking_max_distance

        print("tracking method:", tracking_method)
        if tracking_method == "laptrack":
            lt = LapTrack(
                track_dist_metric=dist_metric,
                track_cost_cutoff=max_distance**2,
                gap_closing_dist_metric=dist_metric,
                gap_closing_cost_cutoff=max_distance**2,
                gap_closing_max_frame_count=2,
                splitting_cost_cutoff=False,
                merging_cost_cutoff=False,
                # alternative_cost_percentile=90,  # default value
                alternative_cost_percentile=0.01,
            )
        elif tracking_method == "spatial_laptrack":
            # print("spatial laptrack")
            lt = SpatialLapTrack(
                spatial_coord_slice=slice(0,2),
                spatial_metric="euclidean",
                track_dist_metric=dist_metric,
                track_cost_cutoff=max_distance,
                gap_closing_dist_metric=dist_metric,
                gap_closing_cost_cutoff=max_distance,
                gap_closing_max_frame_count=3,
                splitting_cost_cutoff=False,
                merging_cost_cutoff=False,
                # alternative_cost_percentile=1,
                alternative_cost_percentile=100,  # modified value
                # alternative_cost_percentile=90, # default value
            )
        else:
            raise RuntimeError(f"Invalid tracking method '{tracking_method}'")
        
        if use_custom_laptrack is not None:
            print("=== WARNING: overriding LapTrack with a custom LapTrack ===")
            lt = use_custom_laptrack

        track_df, split_df, merge_df = lt.predict_dataframe(
            spots_df,
            ["x", "y", "mlkp_intensity", "sir_intensity"],
            only_coordinate_cols=True,
        )
        # track_df.reset_index()

        print("Tracking result:", track_df, sep="\n")

        ####################################################################
        ########################### Visualization ##########################
        if show_tracking:
            def get_track_end(track_df, keys, track_id, first=True):
                df = track_df[track_df["track_id"] == track_id].sort_index(
                    level="frame"
                )
                return df.iloc[0 if first else -1][keys]

            keys = ["position_x", "position_y", "track_id", "tree_id"]

            plt.figure(figsize=(3, 3))
            frames = track_df.index.get_level_values("frame")
            frame_range = [frames.min(), frames.max()]
            # k1, k2 = "position_y", "position_x"
            k1, k2 = "y", "x"
            keys = [k1, k2]

            for track_id, grp in track_df.groupby("track_id"):
                df = grp.reset_index().sort_values("frame")
                plt.scatter(
                    df[k1],
                    df[k2],
                    c=df["frame"],
                    vmin=frame_range[0],
                    vmax=frame_range[1],
                )
                for i in range(len(df) - 1):
                    pos1 = df.iloc[i][keys]
                    pos2 = df.iloc[i + 1][keys]
                    plt.plot([pos1[0], pos2[0]], [pos1[1], pos2[1]], "-k")
                for _, row in list(split_df.iterrows()) + list(
                    merge_df.iterrows()
                ):
                    # pos1 = self.get_track_end(row["parent_track_id"], first=False)
                    # pos2 = self.get_track_end(row["child_track_id"], first=True)
                    pos1 = get_track_end(row["parent_track_id"], first=False)
                    pos2 = get_track_end(row["child_track_id"], first=True)
                    plt.plot([pos1[0], pos2[0]], [pos1[1], pos2[1]], "-k")

            plt.show()
        ####################################################################
        ####################################################################

        # output data conversion
        track_df.reset_index(inplace=True)
        track_df.dropna(inplace=True)
        print("filtered tracking result", track_df, sep="\n")
        print("results columns:[", track_df.columns, "]")
        print("results rows:[", track_df.index, "]")
        track_id_to_mb_track = {}
        spot_to_track_id = {}
        for idx, row in track_df.iterrows():
            track_id = row["track_id"]
            x = row["x"]
            y = row["y"]
            frame = row["frame"]
            if track_id_to_mb_track.get(track_id) is None:
                track_id_to_mb_track[track_id] = MidBodyTrack(int(track_id))
            spot_to_track_id[(frame, x, y)] = track_id

        for frame, mb_spots in spots_candidates.items():
            for mb_spot in mb_spots:
                track_id = spot_to_track_id[
                    (frame, int(mb_spot.x), int(mb_spot.y))
                ]
                track_id_to_mb_track[track_id].add_spot(mb_spot)

        return list(track_id_to_mb_track.values())

    def _select_best_track(
        self,
        mitosis_track: MitosisTrack,
        mid_body_tracks: list[MidBodyTrack],
        trackmate_tracks: list[TrackMateTrack],
        mitosis_movie: np.array,
        sir_channel=0,
    ) -> MidBodyTrack:
        """
        Select best track from mid-body tracks.

        Parameters
        ----------
        mitosis_movie: TYXC
        """
        (
            mother_track,
            daughter_tracks,
        ) = mitosis_track.get_mother_daughters_tracks(trackmate_tracks)
        # NB: only first daughter is considered
        daughter_track = daughter_tracks[0]

        expected_positions = {}
        for frame in range(
            daughter_track.start,
            daughter_track.start + self.cytokinesis_duration,
        ):
            # If one cell does not exist anymore, stop
            if (
                frame not in daughter_track.spots
                or frame not in mother_track.spots
            ):
                continue
            # Compute mid-body expected relative position at current frame
            closest_points = []
            min_distance = np.inf
            for mother_point in mother_track.spots[frame].spot_points:
                position_mother = [
                    int(mother_point[0]) - mitosis_track.position.min_x,
                    int(mother_point[1]) - mitosis_track.position.min_y,
                ]
                for daughter_point in daughter_track.spots[frame].spot_points:
                    position_daughter = [
                        int(daughter_point[0]) - mitosis_track.position.min_x,
                        int(daughter_point[1]) - mitosis_track.position.min_y,
                    ]
                    distance = np.linalg.norm(
                        [
                            a - b
                            for a, b in zip(position_mother, position_daughter)
                        ]
                    )
                    if distance < min_distance:
                        min_distance = distance
                        closest_points = [(position_mother, position_daughter)]
                    if distance == min_distance:
                        closest_points.append(
                            (position_mother, position_daughter)
                        )

            mid_body_position = np.mean(closest_points, axis=0)
            mid_body_position = np.mean(mid_body_position, axis=0)
            expected_positions[frame - mitosis_track.min_frame] = (
                mid_body_position
            )

        # Remove wrong tracks by keeping only tracks with at least minimum_track_length points
        mid_body_tracks = [
            track
            for track in mid_body_tracks
            if track.length > self.minimum_mid_body_track_length
        ]

        # Compute mean intensity on sir-tubulin channel for each track
        image_sir = mitosis_movie[..., sir_channel]  # TYX
        sir_intensity_track = [0 for _ in mid_body_tracks]
        for idx, track in enumerate(mid_body_tracks):
            abs_track_frames = [
                frame + mitosis_track.min_frame
                for frame in list(track.spots.keys())
            ]
            abs_min_frame = mitosis_track.key_events_frame[
                "cytokinesis"
            ]  # Cytokinesis start
            abs_max_frame = abs_min_frame + int(self.cytokinesis_duration / 2)
            if (
                abs_min_frame > abs_track_frames[-1]
                or abs_max_frame < abs_track_frames[0]
            ):
                sir_intensity_track[idx] = -np.inf
            frame_count = 0
            for frame in range(abs_min_frame, abs_max_frame + 1):
                if frame not in abs_track_frames:
                    continue
                frame_count += 1
                track_spot = track.spots[frame - mitosis_track.min_frame]
                sir_intensity_track[idx] += image_sir[
                    frame - mitosis_track.min_frame,
                    track_spot.y,
                    track_spot.x,
                ]

            if frame_count < (abs_max_frame - abs_min_frame + 1) / 2:
                sir_intensity_track[idx] = -np.inf
            else:
                sir_intensity_track[idx] /= frame_count

        # Get list of expected distances
        expected_distances = []
        for track in mid_body_tracks:
            val = track.get_expected_distance(
                expected_positions, self.mid_body_linking_max_distance
            )
            expected_distances.append(val)

        # Assert lists have same length for next function
        assert len(expected_distances) == len(mid_body_tracks)
        assert len(sir_intensity_track) == len(mid_body_tracks)

        # function to sort tracks by expected distance and intensity
        def func_sir_intensity(track):
            a = expected_distances[mid_body_tracks.index(track)]
            b = sir_intensity_track[mid_body_tracks.index(track)]
            return a - 0.5 * b

        # Remove tracks with infinite func value
        fun_values = []
        final_tracks = []
        for track in mid_body_tracks:
            fun_values.append(func_sir_intensity(track))
            if func_sir_intensity(track) != np.inf:
                final_tracks.append(track)

        # Sort tracks by func value
        sorted_tracks = sorted(final_tracks, key=func_sir_intensity)
        return sorted_tracks[0] if len(sorted_tracks) > 0 else None


    class MbTrackColorManager:
        def __init__(self):
            self.index = 0
            self.color_list = [
                mpl.colormaps["tab10"](i)[:3] for i in range(10)
            ]
            self.id2color = {}

        def get_color_for_track(self, id: int):
            color = self.id2color.get(id)
            if color is None:
                new_color = self.color_list[self.index]
                self.id2color[id] = new_color
                color = new_color
                self.inc_index()
            return color
        
        def inc_index(self):
            self.index += 1
            if self.index >= len(self.color_list):
                self.index = 0

    def save_mid_body_tracking(
        self,
        spots_candidates,
        mitosis_movie: np.ndarray,
        path_output: str,
        mid_body_channel=1,
    ):
        """
        Plot spots detection & tracking.
        """
        # Check if directory exists
        if not os.path.exists(path_output):
            os.makedirs(path_output)

        # matplotlib_colors = [
        #     mpl.colormaps["hsv"](i)[:3] for i in np.linspace(0, 0.9, 100)
        # ]
        # shuffle(matplotlib_colors)
        # matplotlib_colors = [
        #     mpl.colormaps["tab10"](i)[:3] for i in range(20)
        # ]
        color_lib = MidBodyDetectionFactory.MbTrackColorManager()

        # Detect spots in each frame
        nb_frames = mitosis_movie.shape[0]
        for frame in range(nb_frames):
            print(f"generating and saving frame ({frame+1}/{nb_frames})")
            image = mitosis_movie[frame, :, :, mid_body_channel].squeeze()

            # Bigfish spots
            frame_spots = [
                [spot.y, spot.x] for spot in spots_candidates[frame]
            ]
            colors = [
                (
                    # matplotlib_colors[spot.track_id % len(matplotlib_colors)]
                    color_lib.get_color_for_track(spot.track_id)
                    if spot.track_id is not None
                    else (0, 0, 0)
                )
                for spot in spots_candidates[frame]
            ]

            plot_detection(
                image,
                frame_spots,
                color=colors,
                contrast=True,
                path_output=os.path.join(
                    path_output, f"spot_detection_{frame}.png"
                ),
                show=False,
                title=f"Python frame {frame} - Fiji frame {frame + 1}",
                fill=True,
            )
