import numpy as np
import xarray as xr

from src.parameters import Parameters


# TODO: should this be saved to the database?


class Histogram:
    """
    A wrapper around an xarray.Dataset to store histograms.
    This object can also load and save the underlying data into a netCDF file.

    The reason to keep track of this data is to be able to set
    thresholds for detection, based on the background distribution.
    Another reason is to know the amount of time or number of observations
    that did not have a detection, which can be used to calculate
    the upper limit (in case of no detections) or to calculate the
    physical rates (in case of detections).

    Histograms can also be added with each other (using the + operator).
    This simply adds together the counts in each bin,
    and expands the bins if they have different limits.

    Coordinates: the values are saved as an xarray Dataset,
    where the dataset values are always the counts of how many
    epochs/data points fall into each bin.
    The bins are along coordinates in either
    score, source, or observation (obs) space.
    The score coordinates are for the measured values,
    like Signal to Noise Ratio (snr), or Delta Magnitude (dmag).
    The source coordinates depend on the source,
    and are given by the catalog row for that source (e.g., magnitude).
    The observation coordinates are the values given by
    the specific observation (e.g., exposure time, filter).

    Choose coordinates with names that match the catalog values
    (for source coordinates), or the columns of the lightcurve dataframe
    for the observation coordinates.

    Data quality cuts: The score coordinates can be used also
    to track the results for various quality cuts on the data.
    Make sure the names of the score_coords are the same as the
    names of the columns added by the quality cuts.

    DataArrays: the datasets should include one DataArray for each
    of the score coordinates, sharing all the source- and obs-coordinates.

    """

    def __init__(self):
        self.pars = Parameters()
        self.data = None

        # parameter definitions
        self.pars.dtype = "uint32"
        self.pars.score_coords = {
            "snr": (-10, 10, 0.1),
            "dmag": (-3, 3, 0.1),
        }  # other options: any of the quality cuts
        self.pars.source_coords = {
            "mag": (15, 21, 0.5),
        }  # other options: color, ecl lat, mass, radius
        self.pars.obs_coords = {
            "exptime": (30, 1),
            "filt": (),
        }  # other options: airmass, zp, magerr

    def initialize_data(self):
        self.pars.verify()
        if self.pars.dtype not in ("uint16", "uint32"):
            raise ValueError(
                f"Unsupported dtype: {self.pars.dtype}, " f"must be uint16 or uint32."
            )

        # create the coordinates
        coords = {}
        for input_ in ("score", "source", "obs"):
            for k, v in getattr(self.pars, f"{input_}_coords").items():
                coords[k] = self.create_coordinate(k, v)
                coords[k].attrs["input"] = input_

        data_shape = tuple(
            len(v) for v in coords.values() if v.attrs["input"] != "score"
        )

        # these coordinates are shared by all the DataArrays
        common_coord_names = [
            k for k, v in coords.items() if v.attrs["input"] != "score"
        ]

        data_vars = {}
        for k, v in coords.items():
            if v.attrs["input"] == "score":
                data_vars[k + "_counts"] = (
                    common_coord_names + [k],
                    np.zeros(data_shape + (len(v),), dtype=self.pars.dtype),
                )

        self.data = xr.Dataset(data_vars, coords=coords)

    def create_coordinate(self, name, specs):
        """
        Create a coordinate axis with a preset range
        or make a dynamic axis.
        This axis will be used in defining the xarray
        for the histogram.

        Parameters
        ----------
        name: str
            The name of the coordinate.
        specs:
            A tuple of (start, stop, step) for a fixed range,
            or an empty tuple for a dynamic range.

        Returns
        -------
        xarray.DataArray
            The coordinate axis.
        """

        if not isinstance(specs, tuple):
            raise ValueError(f"Coordinate specs must be a tuple: {specs}")

        if len(specs) and isinstance(specs[-1], str):
            units = specs[-1]
            specs = specs[:-1]
        else:
            units = ""

        if len(specs) == 0:
            # dynamic range
            ax = xr.DataArray([], dims=[name])
            ax.attrs["type"] = "dynamic"
        elif len(specs) == 2:
            # dynamic range with a fixed step
            ax = xr.DataArray(np.array([specs[0]]), dims=[name])
            ax.attrs["step"] = specs[-1]
            ax.attrs["type"] = "dynamic"
        elif len(specs) == 3:
            # fixed range
            start, stop, step = specs
            ax = xr.DataArray(
                np.arange(start, stop + step, step),
                dims=[name],
            )
            ax.attrs["step"] = specs[-1]
            ax.attrs["type"] = "fixed"
        else:
            raise ValueError(
                f"Coordinate specs must be a tuple of length 0 or 3: {specs}"
            )

        ax.attrs["long_name"] = self.get_coordinate_name(name)
        ax.attrs["units"] = units

        return ax

    @staticmethod
    def get_coordinate_name(name):
        """
        Get the long name of a coordinate.
        If the name is not recognized,
        return the name itself.

        Parameters
        ----------
        name:
            Short name given to the coordinate,
            which also matches the column name in the catalog
            or in the lightcurve data.

        Returns
        -------
        str
            Long name of the coordinate
        """
        return {
            "mag": "Magnitude",
            "dmag": "Delta Magnitude",
            "snr": "Signal to Noise Ratio",
            "exptime": "Exposure Time",
            "filt": "Filter",
        }.get(name, name)

    def get_size(self, units="mb"):
        """
        Get the size of the histogram in memory.
        If some of the axes are empty (e.g., dynamic axes
        where data has not yet been added), the size would
        be zero. In that case, use get_size_estimate() instead.

        Parameters
        ----------
        units: str
            Can be 'kb', 'mb', or 'gb'.
            Otherwise, assume 'bytes' are returned.

        Returns
        -------
        float
            The size of the histogram in memory,
            in whatever units were requested.
        """
        total_size = 0
        for d in self.data.data_vars.values():
            total_size += d.size * d.dtype.itemsize

        return total_size / self.unit_convert_bytes(units)

    def get_size_estimate(self, units="mb", dyn_coord_size=3, dyn_score_size=100):
        """
        Get an estimate for the size of the histogram in memory.
        If any of the (dynamic) axis are not yet filled, will use
        estimates for the final size of these axes when estimating
        memory footprint.
        The assumption is that dynamic coordinate axes (like filter)
        will have a small number of unique values (like 3), and that the
        dynamic score axes (like SNR) will have a large number of
        unique values (like 100).

        Parameters
        ----------
        units: str
            Can be 'kb', 'mb', or 'gb'.
            Otherwise, assume 'bytes' are returned.
        dyn_coord_size: int
            The number of unique values to assume for dynamic coordinate axes.
            Default is 3, which is appropriate for having a few values.
        dyn_score_size: int
            The number of unique values to assume for dynamic score axes.
            Default is 100, which is appropriate for having a lot of values,
            spanning the dynamic range of possible scores.

        Returns
        -------
        float
            The size of the histogram in memory,
            in whatever units were requested.
        """

        common_size = 1
        for d in self.data.coords.values():
            # only count non-source coordinates (shared by all DataArrays)
            if d.attrs["input"] != "score":
                if len(d) <= 1:
                    common_size *= dyn_coord_size
                else:
                    common_size *= len(d)

        score_size = 0
        for d in self.data.coords.values():
            # only count non-source coordinates (shared by all DataArrays)
            if d.attrs["input"] == "score":
                if len(d) <= 1:
                    score_size += dyn_score_size
                else:
                    score_size += len(d)

        total_size = common_size * score_size

        array_names = list(self.data.keys())
        total_size *= self.data[array_names[0]].dtype.itemsize

        return total_size / self.unit_convert_bytes(units)

    @staticmethod
    def unit_convert_bytes(units):
        if units.endswith("s"):
            units = units[:-1]

        return {
            "kb": 1024,
            "mb": 1024**2,
            "gb": 1024**3,
        }.get(units.lower(), 1)

    def add_data(self, data, score):
        pass


if __name__ == "__main__":
    h = Histogram()
    h.initialize_data()
